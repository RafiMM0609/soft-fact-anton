"""End-to-end execution engine for ACP Software Factory."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from acp.agent import AcpAgent
from acp.worker import AcpWorker
from factory.config import Settings
from factory.db import Store
from factory.models import Plan, Task
from factory.parallel import ParallelBuilder
from factory.products import ensure_repo, head_commit
from factory.testing import Tester
from factory.verifier import Verifier

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
3. Identify user-facing acceptance flows (kind="api" or kind="browser") with concrete steps and selectors.
4. Ensure task IDs are lowercase kebab-case (^[a-z][a-z0-9_-]{0,47}$).
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
        self.agent = (
            agent
            if agent is not None
            else AcpAgent(agent_name=self.settings.harness, harness_path=self.settings.agy)
        )
        self.worker = (
            worker
            if worker is not None
            else AcpWorker(agent_name=self.settings.harness, harness_path=self.settings.agy)
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

        try:
            # 1. Resolve or generate plan
            active_plan = plan
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
                        deal = dealer.deal(project_id, brief=brief)
                        dealt_text = deal_blocks(deal)
                        if dealt_text:
                            parts.append(f"\nDEALT DESIGN SPECIFICATION:\n{dealt_text}")
                    except Exception as exc:
                        logger.debug("Could not deal design block: %s", exc)

                return "\n".join(parts)

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

            verifier_ok, verifier_msg = self.verifier.verify(root)
            if not verifier_ok:
                raise RuntimeError(f"Build verification failed:\n{verifier_msg}")

            # 4. Acceptance testing
            if cancelled():
                raise RuntimeError("Build cancelled during testing")

            if active_plan.test_flows:
                tester_ok, test_results = self.tester.run_flows(active_plan.test_flows)
                if not tester_ok:
                    raise RuntimeError(f"Acceptance testing failed:\n{json.dumps(test_results)}")
            else:
                test_results = []

            # 5. Commit artifact digest and mark job succeeded
            commit_sha = head_commit(root)
            artifact = {
                "project_id": project_id,
                "job_id": job_id,
                "commit_sha": commit_sha,
                "tasks_completed": completed_tasks,
                "verification": verifier_msg,
                "test_results": test_results,
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
            self.store.finish(job_id, state="failed", error=err_msg)
            self.store.event(project_id, job_id, "build.failed", {"error": err_msg})
            raise
