"""End-to-end execution engine for ACP Software Factory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any, Callable

from acp.agent import AcpAgent
from acp.protocol import AcpError
from acp.worker import AcpWorker
from factory.config import Settings
from factory.db import Store
from factory.models import AcceptanceFlow, AdjustScope, Plan, Task
from factory.parallel import ParallelBuilder
from factory.products import ensure_repo, head_commit
from factory.testing import Tester, run_product_server
from factory.verifier import Verifier
from factory.artifact import assemble_artifact

try:
    from factory.design import DesignDealer, deal_blocks
except ImportError:
    DesignDealer = None  # type: ignore[assignment, misc]
    deal_blocks = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

PLANNER_PROMPT = """\
You are the lead software architect in an automated software factory.
Given a project brief and profile, decompose the requirements into a coherent, executable Plan.
Requirements:
1. Create atomic, independently verifiable tasks with clear instructions and acceptance criteria.
2. Define explicit task dependencies (depends_on) to form a valid Directed Acyclic Graph (DAG) without cycles.
3. Identify user-facing acceptance flows (kind="api" or kind="browser") with concrete steps and selectors. For API steps, ALWAYS use relative paths (e.g. 'GET /healthz', 'POST /items'), NEVER hardcode ports or localhost URLs.
4. For server/backend profiles (go-htmx, node, zig), the application MUST read the listen port from the `PORT` environment variable (defaulting to 8080 if unset; never hardcode port 8000).
5. Ensure task IDs are lowercase kebab-case (^[a-z][a-z0-9_-]{0,47}$).
"""


class Engine:
    """Coordinates planning, parallel building, verification, and acceptance testing."""

    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        agent: AcpAgent | None = None,
        worker: AcpWorker | None = None,
        verifier: Verifier | None = None,
        tester: Tester | None = None,
    ) -> None:
        self.settings = settings if settings is not None else Settings()
        self.store = store if store is not None else Store(self.settings.data / "factory.db")
        resolved_harness = self.settings.resolve_harness_path()
        model = (
            self.settings.opencode_model
            if self.settings.harness.strip().lower() == "opencode"
            else None
        )
        self.agent = (
            agent
            if agent is not None
            else AcpAgent(agent_name=self.settings.harness, harness_path=resolved_harness, model=model)
        )
        self.worker = (
            worker
            if worker is not None
            else AcpWorker(agent_name=self.settings.harness, harness_path=resolved_harness, model=model)
        )
        self.verifier = verifier if verifier is not None else Verifier()
        self.tester = tester if tester is not None else Tester()
        self.builder = ParallelBuilder(
            settings=self.settings,
            store=self.store,
            worker=self.worker,
            agent=self.agent,
        )

    def plan_brief(
        self,
        project: dict[str, Any] | Any,
        brief: str,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Plan:
        """Generate a structured Plan from a brief using AcpAgent."""
        profile = (
            project.get("profile", "static")
            if isinstance(project, dict)
            else getattr(project, "profile", "static")
        )
        plan: Plan = self.agent.generate(
            role="planner",
            instruction=PLANNER_PROMPT,
            context={"brief": brief, "profile": profile},
            schema=Plan,
            timeout=self.settings.agent_timeout,
            cancelled=cancelled,
        )

        project_id = (
            project.get("id") if isinstance(project, dict) else getattr(project, "id", None)
        )
        if project_id and self.store:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE projects SET plan=? WHERE id=?",
                    (plan.model_dump_json(), project_id),
                )
            self.store.event(project_id, None, "plan.created", {"task_count": len(plan.tasks)})

        return plan

    def _repo_files(self, root: Path, limit: int = 150) -> list[str]:
        """Collect relative file paths in the repository, excluding build/vcs noise."""
        skip = {".git", "vendor", "node_modules", ".venv", "__pycache__", "data"}
        files: list[str] = []
        if not root.is_dir():
            return files
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in skip for part in rel.parts):
                continue
            files.append(str(rel))
            if len(files) >= limit:
                break
        return files

    def preview_adjust_scope(self, project: Any, instruction: str) -> AdjustScope:
        """Identify concise domain hierarchy candidates affected by an adjustment request."""
        project_id = (
            project["id"] if isinstance(project, dict) else getattr(project, "id", str(project))
        )
        profile = (
            project.get("profile", "static")
            if isinstance(project, dict)
            else getattr(project, "profile", "static")
        )
        root = ensure_repo(self.settings.data, project_id)
        repo_files = self._repo_files(root)

        prompt = (
            "You are the Domain Scope Classifier in an automated software factory.\n"
            "Given an existing project's file list and the user's adjustment request, identify 1 to 4 "
            "high-level domain hierarchy paths (e.g. 'User > Form Input Data Detail User' or "
            "'Acara > Halaman Admin Tambah Acara') that match what the user wants to change.\n"
            "If the request could plausibly apply to multiple modules, list each candidate separately "
            "so the user can pick the right one before coding starts. Do NOT generate tasks."
        )
        ctx = {
            "project_id": project_id,
            "profile": profile,
            "adjustment_instruction": instruction,
            "repo_files": repo_files,
        }
        return self.agent.generate(
            role="Domain Scope Classifier",
            instruction=prompt,
            context=ctx,
            schema=AdjustScope,
            timeout=self.settings.agent_timeout,
        )

    def plan_adjust(
        self,
        project: Any,
        instruction: str,
        selected_domains: list[str],
        target_files: list[str] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Plan:
        """Generate an incremental delta Plan merged onto the existing project Plan."""
        project_id = (
            project["id"] if isinstance(project, dict) else getattr(project, "id", str(project))
        )
        profile = (
            project.get("profile", "static")
            if isinstance(project, dict)
            else getattr(project, "profile", "static")
        )
        root = ensure_repo(self.settings.data, project_id)
        repo_files = self._repo_files(root)

        existing_plan: Plan | None = None
        plan_json = (
            project.get("plan") if isinstance(project, dict) else getattr(project, "plan", None)
        )
        if plan_json:
            try:
                existing_plan = Plan.model_validate_json(plan_json)
            except Exception:
                existing_plan = None

        adjust_prompt = (
            f"{PLANNER_PROMPT}\n"
            "INCREMENTAL ADJUSTMENT RULES:\n"
            "- This is an incremental adjustment on an EXISTING codebase.\n"
            "- ONLY plan tasks needed to implement the requested adjustment within the confirmed "
            "domain scope and target files.\n"
            "- Do NOT recreate or delete unrelated existing features.\n"
            "- Keep existing behaviour intact and add regression/acceptance flows for the change."
        )
        ctx: dict[str, Any] = {
            "project_id": project_id,
            "profile": profile,
            "adjustment_instruction": instruction,
            "confirmed_domains": selected_domains,
            "target_files": target_files or [],
            "repo_files": repo_files,
            "existing_summary": existing_plan.summary if existing_plan else "",
        }
        delta = self.agent.generate(
            role="Adjustment Planner",
            instruction=adjust_prompt,
            context=ctx,
            schema=Plan,
            timeout=self.settings.agent_timeout,
            cancelled=cancelled,
        )

        if existing_plan is None:
            return delta

        existing_ids = {t.id for t in existing_plan.tasks}
        id_map: dict[str, str] = {}
        mapped_tasks: list[Task] = []
        for task in delta.tasks:
            tid = task.id
            candidate = tid if tid not in existing_ids and tid not in id_map.values() else f"adj-{tid}"[:48]
            counter = 1
            while candidate in existing_ids or candidate in id_map.values():
                candidate = f"adj-{tid[:36]}-{counter}"[:48]
                counter += 1
            id_map[tid] = candidate

        for task in delta.tasks:
            new_id = id_map[task.id]
            remapped_deps = [id_map.get(d, d) for d in task.depends_on]
            valid_targets = existing_ids | set(id_map.values())
            valid_deps = [d for d in remapped_deps if d in valid_targets and d != new_id]
            mapped_tasks.append(
                Task(
                    id=new_id,
                    title=task.title,
                    instructions=task.instructions,
                    acceptance=task.acceptance,
                    depends_on=valid_deps,
                    skills=task.skills,
                )
            )

        existing_flow_ids = {f.id for f in existing_plan.test_flows}
        mapped_flows: list[AcceptanceFlow] = []
        for flow in delta.test_flows:
            fid = flow.id
            if fid in existing_flow_ids or any(f.id == fid for f in mapped_flows):
                fid = f"adj-{flow.id}"[:48]
                counter = 1
                while fid in existing_flow_ids or any(f.id == fid for f in mapped_flows):
                    fid = f"adj-{flow.id[:36]}-{counter}"[:48]
                    counter += 1
            mapped_flows.append(
                AcceptanceFlow(
                    id=fid,
                    kind=flow.kind,
                    goal=flow.goal,
                    steps=flow.steps,
                    selectors=flow.selectors,
                )
            )

        scope_label = ", ".join(selected_domains) if selected_domains else "custom"
        combined_summary = (
            f"{existing_plan.summary}\n\n[Adjust ({scope_label})]: {instruction}\n{delta.summary}"
        )[:12000]

        return Plan(
            summary=combined_summary,
            assumptions=list(dict.fromkeys(existing_plan.assumptions + delta.assumptions))[:50],
            questions=[],
            tasks=list(existing_plan.tasks) + mapped_tasks,
            pages=list(dict.fromkeys((existing_plan.pages or []) + (delta.pages or [])))[:20],
            test_flows=(list(existing_plan.test_flows) + mapped_flows)[:20],
        )

    def build(
        self,
        project: dict[str, Any] | Any,
        job: dict[str, Any] | Any,
        plan: Plan | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> dict[str, Any]:
        """Execute end-to-end product build: Plan -> Parallel Tasks -> Verify -> Test -> Artifact.

        Args:
            project: Project dictionary or object.
            job: Job dictionary or object.
            plan: Optional pre-existing Plan. If None, generated via plan_brief.
            cancelled: Callback returning True if cancellation is requested.

        Returns:
            Dictionary containing build digest artifact.
        """
        project_id = (
            project["id"] if isinstance(project, dict) else getattr(project, "id", str(project))
        )
        job_id = job["id"] if isinstance(job, dict) else getattr(job, "id", str(job))

        if cancelled():
            self.store.finish(job_id, state="cancelled", error="Cancelled before build started")
            return {"status": "cancelled", "project_id": project_id, "job_id": job_id}

        root = ensure_repo(self.settings.data, project_id)
        usage_before = dict(getattr(self.agent, "total_usage", {}) or {})

        stage = "planner"
        try:
            # 1. Resolve or generate plan
            job_payload: dict[str, Any] = {}
            raw_payload = job.get("payload") if isinstance(job, dict) else getattr(job, "payload", None)
            if isinstance(raw_payload, dict):
                job_payload = raw_payload
            elif isinstance(raw_payload, str) and raw_payload.strip():
                try:
                    parsed_p = json.loads(raw_payload)
                    if isinstance(parsed_p, dict):
                        job_payload = parsed_p
                except Exception:
                    pass

            active_plan = plan
            if active_plan is None and job_payload.get("mode") == "adjust":
                active_plan = self.plan_adjust(
                    project=project,
                    instruction=str(job_payload.get("instruction", "")),
                    selected_domains=list(job_payload.get("selected_domains") or []),
                    target_files=list(job_payload.get("target_files") or []),
                    cancelled=cancelled,
                )

            if active_plan is None:
                plan_json = (
                    project.get("plan")
                    if isinstance(project, dict)
                    else getattr(project, "plan", None)
                )
                if plan_json:
                    try:
                        active_plan = Plan.model_validate_json(plan_json)
                    except Exception:
                        active_plan = None

            if active_plan is None:
                brief = (
                    project.get("brief", "")
                    if isinstance(project, dict)
                    else getattr(project, "brief", "")
                )
                active_plan = self.plan_brief(project, brief, cancelled=cancelled)

            with self.store.tx() as db:
                db.execute(
                    "UPDATE projects SET plan=? WHERE id=?",
                    (active_plan.model_dump_json(), project_id),
                )

            stage = "build"
            # 2. Execute parallel wave builder over worktrees
            dealer = None
            if DesignDealer and hasattr(self.settings, "skills_dir") and self.settings.skills_dir.is_dir():
                try:
                    dealer = DesignDealer(self.settings.skills_dir)
                except Exception as exc:
                    logger.debug("DesignDealer initialization skipped: %s", exc)

            def instructions_for(task: Task, context: dict[str, Any] | None = None) -> str:
                parts = [f"TASK ({task.id}): {task.title}\n{task.instructions}"]
                if task.acceptance:
                    parts.append("\nACCEPTANCE CRITERIA:\n" + "\n".join(f"- {c}" for c in task.acceptance))

                # Deal design block if task has design skills
                if dealer and deal_blocks and any("design" in s or s in ("animate", "apple-design", "break-ui", "emil-design-eng") for s in task.skills):
                    try:
                        deal = dealer.deal(brief, seed=project_id)
                        dealt_text = deal_blocks(deal)
                        if dealt_text:
                            parts.append(f"\nDEALT DESIGN SPECIFICATION:\n{dealt_text}")
                    except Exception as exc:
                        logger.debug("Could not deal design block: %s", exc)

                return "\n".join(parts)

            if self.store:
                self.store.event(
                    project_id,
                    job_id,
                    "build.tasks_started",
                    {"task_count": len(active_plan.tasks)},
                )

            completed_tasks = self.builder.build_tasks(
                project=project,
                job=job,
                plan=active_plan,
                instructions_for=instructions_for,
                cancelled=cancelled,
            )

            # 3. Verification checks on merged root
            if cancelled():
                raise RuntimeError("Build cancelled during verification")

            if self.store:
                self.store.event(project_id, job_id, "verify.started", {})

            verifier_ok, verifier_msg = self.verifier.verify(root)
            if not verifier_ok:
                raise RuntimeError(f"Build verification failed:\n{verifier_msg}")

            if self.store:
                self.store.event(project_id, job_id, "verify.passed", {})

            # 4. Acceptance testing
            if cancelled():
                raise RuntimeError("Build cancelled during testing")

            if active_plan.test_flows:
                if self.store:
                    self.store.event(
                        project_id,
                        job_id,
                        "test.started",
                        {"flow_count": len(active_plan.test_flows)},
                    )
                with run_product_server(root) as base_url:
                    tester_ok, test_results = self.tester.run_flows(
                        active_plan.test_flows, base_url=base_url
                    )
                if not tester_ok:
                    raise RuntimeError(f"Acceptance testing failed:\n{json.dumps(test_results)}")
                if self.store:
                    self.store.event(
                        project_id,
                        job_id,
                        "test.passed",
                        {"flow_count": len(active_plan.test_flows)},
                    )
            else:
                test_results = []

            # 5. Assemble release artifact bundle and mark job succeeded
            if self.store:
                self.store.event(project_id, job_id, "release.packaging", {})

            commit_sha = head_commit(root)
            tarball_path = self.settings.data / "artifacts" / project_id / f"{job_id}.tar.gz"
            profile = (
                project.get("profile", "static")
                if isinstance(project, dict)
                else getattr(project, "profile", "static")
            )
            tar_info = assemble_artifact(
                source_root=root,
                output_tarball=tarball_path,
                project=project,
                plan=active_plan,
                profile=profile,
            )

            usage_after = dict(getattr(self.agent, "total_usage", {}) or {})
            token_usage = {
                k: max(0, int(usage_after.get(k, 0)) - int(usage_before.get(k, 0)))
                for k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
            }

            artifact = {
                "project_id": project_id,
                "job_id": job_id,
                "commit_sha": commit_sha,
                "tasks_completed": completed_tasks,
                "verification": verifier_msg,
                "test_results": test_results,
                "token_usage": token_usage,
                "artifact_path": tar_info["artifact_path"],
                "digest": tar_info["digest"],
                "size_bytes": tar_info["size_bytes"],
                "finished_at": time.time(),
                "status": "succeeded",
            }

            with self.store.tx() as db:
                db.execute(
                    "UPDATE projects SET artifact=?, state='built' WHERE id=?",
                    (json.dumps(artifact), project_id),
                )
            self.store.finish(job_id, state="succeeded", result=artifact)
            self.store.event(project_id, job_id, "build.succeeded", {"commit_sha": commit_sha})
            return artifact

        except Exception as exc:
            err_msg = str(exc)
            logger.error("Build failed for project %s (job %s): %s", project_id, job_id, err_msg)
            if stage == "planner" and isinstance(exc, (AcpError, ValueError)):
                self.store.record_acp_error(project_id, job_id, stage, self.agent, exc)
            self.store.finish(job_id, state="failed", error=err_msg)
            self.store.event(project_id, job_id, "build.failed", {"error": err_msg})
            raise
