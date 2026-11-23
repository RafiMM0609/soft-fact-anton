"""Parallel wave builder with merge barriers, review guard, and repair loops."""

from __future__ import annotations

from collections import defaultdict
import concurrent.futures
from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import threading
from typing import Any, Callable

from acp.agent import AcpAgent
from acp.worker import AcpWorker
from factory.models import Plan, Review, Task
from factory.products import (
    branch_delete,
    commit_all,
    diff_against_base,
    ensure_repo,
    head_commit,
    merge_branch,
    worktree_add,
    worktree_remove,
)
from factory.repair import format_repair_feedback
from factory.reviewguard import review_diff

logger = logging.getLogger(__name__)


class ParallelBuildError(RuntimeError):
    """Raised when a task build fails, is cancelled, or exceeds repair limits."""


@dataclass
class BuilderSettings:
    """Runtime configuration settings for ParallelBuilder."""

    data_dir: Path = Path("data")
    worker_concurrency: int = 2
    max_repairs: int = 3
    worker_timeout: int = 1800
    review_timeout: int = 300


@dataclass
class TaskOutcome:
    """Result of worker execution for a single task."""

    task: Task
    success: bool
    worktree_path: Path
    branch: str
    base_commit: str
    diff: str = ""
    commit_sha: str | None = None
    worker_id: str | None = None
    error: Exception | str | None = None


class ParallelBuilder:
    """Orchestrates parallel wave execution of plan tasks over isolated Git worktrees."""

    def __init__(
        self,
        settings: Any = None,
        store: Any = None,
        worker: AcpWorker | None = None,
        agent: AcpAgent | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.worker = worker if worker is not None else AcpWorker()
        self.agent = agent if agent is not None else AcpAgent()

    def _get_setting(self, key: str, default: Any) -> Any:
        if self.settings is None:
            return default
        if isinstance(self.settings, dict):
            return self.settings.get(key, default)
        return getattr(self.settings, key, default)

    @property
    def data_dir(self) -> Path:
        return Path(self._get_setting("data_dir", Path("data")))

    @property
    def concurrency(self) -> int:
        val = self._get_setting("worker_concurrency", 2)
        return max(1, int(val))

    @property
    def max_repairs(self) -> int:
        val = self._get_setting("max_repairs", 3)
        return max(1, int(val))

    @property
    def worker_timeout(self) -> int:
        return int(self._get_setting("worker_timeout", 1800))

    @property
    def review_timeout(self) -> int:
        return int(self._get_setting("review_timeout", 300))

    def build_tasks(
        self,
        project: Any,
        job: Any,
        plan: Plan,
        instructions_for: Callable[..., str] | dict[str, str] | str,
        cancelled: Callable[[], bool] | None = None,
        review_for: Callable[..., Review] | None = None,
    ) -> list[str]:
        """Execute tasks wave by wave with concurrency limits and merge barriers.

        Args:
            project: Project record or dict (containing id).
            job: Job record or dict (containing id).
            plan: Validated Plan DAG.
            instructions_for: Callable, mapping, or string to provide instructions per task.
            cancelled: Optional callable returning True if cancellation requested.
            review_for: Optional custom review callable (task, diff, context) -> Review.

        Returns:
            List of successfully completed and merged task IDs.

        Raises:
            ParallelBuildError: If tasks fail, cannot be merged, or exceed max repairs.
        """
        project_id = project["id"] if isinstance(project, dict) else getattr(project, "id", str(project))
        job_id = job["id"] if isinstance(job, dict) else getattr(job, "id", str(job))

        is_cancelled = cancelled if cancelled is not None else (lambda: False)

        def check_cancelled() -> None:
            if is_cancelled():
                raise ParallelBuildError("Task building was cancelled")

        def resolve_instruction(task: Task, context: dict[str, Any]) -> str:
            if callable(instructions_for):
                try:
                    return instructions_for(task, context)
                except TypeError:
                    return instructions_for(task)
            elif isinstance(instructions_for, dict):
                return instructions_for.get(task.id, task.instructions)
            elif isinstance(instructions_for, str):
                return instructions_for
            return task.instructions

        root: Path | None = None
        git_lock = threading.Lock()
        active_worktrees: set[tuple[Path, str]] = set()

        try:
            root = ensure_repo(self.data_dir, project_id)
            worktrees_dir = self.data_dir / "worktrees"
            worktrees_dir.mkdir(parents=True, exist_ok=True)

            completed: set[str] = set()
            if self.store and root is not None:
                try:
                    import subprocess
                    proc_res = subprocess.run(
                        ["git", "rev-list", "HEAD"],
                        cwd=root,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    present_commits = set(proc_res.stdout.split())
                    if hasattr(self.store, "tasks_merged_for_project"):
                        completed.update(
                            self.store.tasks_merged_for_project(
                                project_id, present_commits=present_commits
                            )
                        )
                except Exception as exc:
                    logger.debug("Failed querying tasks_merged_for_project: %s", exc)

            if self.store and hasattr(self.store, "completed_tasks"):
                try:
                    completed.update(self.store.completed_tasks(job_id))
                except Exception:
                    pass

            attempts: dict[str, int] = defaultdict(int)
            repair_feedbacks: dict[str, str] = {}
            task_findings: dict[str, list[str]] = defaultdict(list)

            while len(completed) < len(plan.tasks):
                check_cancelled()
                ready = plan.ready_set(completed)
                if not ready:
                    remaining = [t.id for t in plan.tasks if t.id not in completed]
                    raise ParallelBuildError(
                        f"No ready tasks found for remaining tasks {remaining}. "
                        "Plan has cycle or deadlocked dependencies."
                    )

                def run_worker(task: Task) -> TaskOutcome:
                    attempts[task.id] += 1
                    cur_attempt = attempts[task.id]
                    if cur_attempt > self.max_repairs:
                        return TaskOutcome(
                            task=task,
                            success=False,
                            worktree_path=worktrees_dir / f"{job_id}_{task.id}",
                            branch=f"factory/{job_id}/{task.id}",
                            base_commit="",
                            error=ParallelBuildError(
                                f"Task '{task.id}' exceeded maximum repair attempts ({self.max_repairs})"
                            ),
                        )

                    branch_name = f"factory/{job_id}/{task.id}"
                    worktree_path = worktrees_dir / f"{job_id}_{task.id}"

                    with git_lock:
                        check_cancelled()
                        worktree_remove(root, worktree_path)
                        branch_delete(root, branch_name)
                        if worktree_path.exists():
                            shutil.rmtree(worktree_path, ignore_errors=True)

                        base_commit = head_commit(root)
                        worktree_add(root, worktree_path, branch_name)
                        active_worktrees.add((worktree_path, branch_name))

                    worker_id: str | None = None
                    if self.store:
                        try:
                            w_rec = self.store.create_worker(
                                project_id=project_id,
                                job_id=job_id,
                                task_id=task.id,
                                branch=branch_name,
                                base_commit=base_commit,
                                depends_on=task.depends_on,
                                worktree=worktree_path,
                            )
                            worker_id = w_rec["id"]
                            self.store.set_task_state(
                                job_id, task.id, "running", {"attempt": cur_attempt}
                            )
                            self.store.set_worker(worker_id, state="running", attempts=cur_attempt)
                        except Exception as exc:
                            logger.warning("Failed to update store for worker: %s", exc)

                    ctx = {
                        "project_id": project_id,
                        "job_id": job_id,
                        "task_id": task.id,
                        "attempt": cur_attempt,
                    }
                    instr = resolve_instruction(task, ctx)
                    if task.id in repair_feedbacks:
                        instr = f"{instr}\n\n{repair_feedbacks[task.id]}"

                    check_cancelled()
                    try:
                        self.worker.run_task(
                            worktree=worktree_path,
                            instruction=instr,
                            timeout=self.worker_timeout,
                            cancelled=is_cancelled,
                            skills=task.skills,
                        )
                    except Exception as exc:
                        return TaskOutcome(
                            task=task,
                            success=False,
                            worktree_path=worktree_path,
                            branch=branch_name,
                            base_commit=base_commit,
                            worker_id=worker_id,
                            error=exc,
                        )

                    check_cancelled()
                    try:
                        commit_msg = f"Task {task.id}: {task.title}"
                        commit_sha = commit_all(worktree_path, commit_msg)
                        diff = diff_against_base(worktree_path, base_commit)
                        return TaskOutcome(
                            task=task,
                            success=True,
                            worktree_path=worktree_path,
                            branch=branch_name,
                            base_commit=base_commit,
                            diff=diff,
                            commit_sha=commit_sha,
                            worker_id=worker_id,
                        )
                    except Exception as exc:
                        return TaskOutcome(
                            task=task,
                            success=False,
                            worktree_path=worktree_path,
                            branch=branch_name,
                            base_commit=base_commit,
                            worker_id=worker_id,
                            error=exc,
                        )

                # Execute ready wave tasks in parallel bounded by worker concurrency
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                    future_to_task = {executor.submit(run_worker, t): t for t in ready}
                    wave_outcomes: list[TaskOutcome] = []
                    for future in concurrent.futures.as_completed(future_to_task):
                        check_cancelled()
                        wave_outcomes.append(future.result())

                # Wave Barrier: all worker executions in wave complete before review & merge
                # Preserve deterministic processing order matching ready task IDs
                ready_ids = [t.id for t in ready]
                wave_outcomes.sort(key=lambda o: ready_ids.index(o.task.id))

                for outcome in wave_outcomes:
                    check_cancelled()
                    t = outcome.task

                    # Case A: Worker execution failure or unhandled exception
                    if not outcome.success:
                        if self.store and outcome.worker_id:
                            self.store.set_worker(
                                outcome.worker_id,
                                state="failed",
                                detail={"error": str(outcome.error)},
                            )

                        if attempts[t.id] >= self.max_repairs:
                            if self.store:
                                self.store.set_task_state(
                                    job_id, t.id, "failed", {"error": str(outcome.error)}
                                )
                            raise ParallelBuildError(
                                f"Task '{t.id}' failed after {attempts[t.id]} attempts: {outcome.error}"
                            )

                        repair_feedbacks[t.id] = format_repair_feedback(
                            outcome.error, task_findings[t.id]
                        )
                        if self.store:
                            self.store.set_task_state(
                                job_id, t.id, "repairing", {"error": str(outcome.error)}
                            )

                        with git_lock:
                            worktree_remove(root, outcome.worktree_path)
                            branch_delete(root, outcome.branch)
                            active_worktrees.discard((outcome.worktree_path, outcome.branch))
                        continue

                    # Case B: Worker succeeded, proceed to diff review
                    if self.store and outcome.worker_id:
                        self.store.set_worker(outcome.worker_id, state="reviewing")
                        self.store.set_task_state(job_id, t.id, "reviewing")

                    base_ctx = {
                        "project_id": project_id,
                        "job_id": job_id,
                        "task_id": t.id,
                        "attempt": attempts[t.id],
                    }
                    try:
                        if review_for is not None:
                            rev = review_for(t, outcome.diff, base_ctx)
                        else:
                            rev = review_diff(
                                self.agent,
                                t,
                                outcome.diff,
                                base_context=base_ctx,
                                timeout=self.review_timeout,
                                cancelled=is_cancelled,
                            )
                    except Exception as exc:
                        rev = Review(
                            decision="rejected",
                            findings=[f"Review generation error: {exc}"],
                            confidence=0.0,
                        )

                    # Check review verdict
                    if not rev.approved:
                        task_findings[t.id].extend(rev.findings)
                        if self.store and outcome.worker_id:
                            self.store.set_worker(
                                outcome.worker_id,
                                state="rejected",
                                detail={"findings": rev.findings},
                            )

                        if attempts[t.id] >= self.max_repairs:
                            if self.store:
                                self.store.set_task_state(
                                    job_id, t.id, "failed", {"findings": rev.findings}
                                )
                            raise ParallelBuildError(
                                f"Task '{t.id}' review rejected after {attempts[t.id]} attempts: {rev.findings}"
                            )

                        repair_feedbacks[t.id] = format_repair_feedback(rev, task_findings[t.id])
                        if self.store:
                            self.store.set_task_state(
                                job_id, t.id, "repairing", {"findings": rev.findings}
                            )

                        with git_lock:
                            worktree_remove(root, outcome.worktree_path)
                            branch_delete(root, outcome.branch)
                            active_worktrees.discard((outcome.worktree_path, outcome.branch))
                        continue

                    # Review approved! Merge branch sequentially into target HEAD
                    with git_lock:
                        merge_ok, merge_msg = merge_branch(
                            root, outcome.branch, f"Merge task {t.id}: {t.title}"
                        )

                    if merge_ok:
                        completed.add(t.id)
                        repair_feedbacks.pop(t.id, None)
                        if self.store and outcome.worker_id:
                            self.store.set_worker(
                                outcome.worker_id, state="merged", commit_sha=merge_msg
                            )
                            self.store.set_task_state(job_id, t.id, "merged", {"commit": merge_msg})
                            self.store.event(
                                project_id,
                                job_id,
                                "task.merged",
                                {"task": t.id, "commit": merge_msg},
                            )
                        with git_lock:
                            worktree_remove(root, outcome.worktree_path)
                            branch_delete(root, outcome.branch)
                            active_worktrees.discard((outcome.worktree_path, outcome.branch))
                    else:
                        # Merge conflict
                        if self.store and outcome.worker_id:
                            self.store.set_worker(
                                outcome.worker_id, state="conflict", detail={"error": merge_msg}
                            )

                        if attempts[t.id] >= self.max_repairs:
                            if self.store:
                                self.store.set_task_state(
                                    job_id, t.id, "failed", {"error": merge_msg}
                                )
                            raise ParallelBuildError(
                                f"Task '{t.id}' merge conflict after {attempts[t.id]} attempts: {merge_msg}"
                            )

                        repair_feedbacks[t.id] = format_repair_feedback(
                            f"Merge conflict: {merge_msg}", task_findings[t.id]
                        )
                        if self.store:
                            self.store.set_task_state(
                                job_id, t.id, "repairing", {"error": merge_msg}
                            )

                        with git_lock:
                            worktree_remove(root, outcome.worktree_path)
                            branch_delete(root, outcome.branch)
                            active_worktrees.discard((outcome.worktree_path, outcome.branch))

            return sorted(completed)
        finally:
            if root is not None:
                with git_lock:
                    for wt_path, br in list(active_worktrees):
                        try:
                            worktree_remove(root, wt_path)
                        except Exception:
                            pass
                        try:
                            branch_delete(root, br)
                        except Exception:
                            pass
                        if wt_path.exists():
                            shutil.rmtree(wt_path, ignore_errors=True)
                    active_worktrees.clear()
