"""Unit tests for ParallelBuilder, ReviewGuard, and Repair feedback."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any
import pytest

from acp.agent import AcpAgent
from acp.worker import AcpWorker, AcpWorkerError
from factory import (
    BuilderSettings,
    ParallelBuilder,
    ParallelBuildError,
    format_repair_feedback,
    review_diff,
)
from factory.db import Store
from factory.models import Plan, Review, Task
from factory.products import (
    ensure_repo,
    list_worktrees,
)


def make_task(
    name: str,
    title: str = "",
    depends_on: list[str] | None = None,
    acceptance: list[str] | None = None,
) -> Task:
    return Task(
        id=name,
        title=title or f"Task {name}",
        instructions=f"Instructions for implementing {name}.",
        acceptance=acceptance or [f"Criterion for {name} met."],
        depends_on=depends_on or [],
    )


# ==============================================================================
# 1. ReviewGuard Tests
# ==============================================================================


def test_review_diff_approved():
    task = make_task("auth", acceptance=["JWT token generated", "401 returned on invalid credentials"])
    sample_diff = "diff --git a/auth.py b/auth.py\n+def login(): return 'jwt'"

    def mock_runner(cmd: list[str], **kwargs: Any) -> str:
        return '{"decision": "approved", "findings": [], "confidence": 0.98}'

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    rev = review_diff(agent, task, sample_diff, base_context={"attempt": 1})

    assert isinstance(rev, Review)
    assert rev.approved is True
    assert rev.decision == "approved"
    assert rev.confidence == 0.98
    assert rev.findings == []


def test_review_diff_rejected_with_findings():
    task = make_task("billing", acceptance=["Stripe charge processed", "Invoice created"])
    sample_diff = "diff --git a/billing.py b/billing.py\n+def charge(): pass"

    def mock_runner(cmd: list[str], **kwargs: Any) -> str:
        return (
            '{"decision": "rejected", "findings": ["Invoice creation is missing"], "confidence": 0.9}'
        )

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    rev = review_diff(agent, task, sample_diff)

    assert rev.approved is False
    assert rev.decision == "rejected"
    assert "Invoice creation is missing" in rev.findings


def test_review_diff_truncates_large_diff():
    task = make_task("big_diff")
    large_diff = "A" * 300_000

    received_context: dict[str, Any] = {}

    def mock_runner(cmd: list[str], input_data: bytes | None = None, **kwargs: Any) -> str:
        # Prompt contains TASK DATA
        if input_data:
            text = input_data.decode("utf-8")
            if "[DIFF TRUNCATED" in text:
                received_context["truncated"] = True
        return '{"decision": "approved", "findings": []}'

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    rev = review_diff(agent, task, large_diff)
    assert rev.approved is True
    assert received_context.get("truncated") is True


# ==============================================================================
# 2. Repair Feedback Tests
# ==============================================================================


def test_format_repair_feedback_from_review():
    rev = Review(
        decision="rejected",
        findings=["Missing error handler for 404", "Unused import sys"],
        confidence=0.85,
    )
    feedback = format_repair_feedback(rev, previous_findings=["Earlier bug"])
    assert "Earlier bug" in feedback
    assert "Missing error handler for 404" in feedback
    assert "Unused import sys" in feedback
    assert "### REPAIR INSTRUCTIONS" in feedback


def test_format_repair_feedback_from_exception():
    exc = AcpWorkerError("Compilation failed: syntax error in main.py", output="SyntaxError: line 42")
    feedback = format_repair_feedback(exc)
    assert "Compilation failed" in feedback
    assert "SyntaxError: line 42" in feedback


def test_format_repair_feedback_from_merge_conflict():
    feedback = format_repair_feedback("Merge conflict in src/utils.py")
    assert "Merge conflict in src/utils.py" in feedback


# ==============================================================================
# 3. ParallelBuilder Wave Execution & Merge Barrier Tests
# ==============================================================================


@pytest.fixture
def test_setup(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "factory.db"
    store = Store(db_path)

    project = store.create_project({"name": "TestProject", "brief": "Build a test app"})
    job = store.enqueue(project["id"], "build")

    settings = BuilderSettings(
        data_dir=data_dir,
        worker_concurrency=2,
        max_repairs=3,
        worker_timeout=60,
        review_timeout=30,
    )

    return {
        "data_dir": data_dir,
        "store": store,
        "project": project,
        "job": job,
        "settings": settings,
    }


def test_parallel_wave_execution_diamond_dag(test_setup: dict[str, Any]):
    """Test 2-wave execution:
    Wave 1: task_a and task_b run concurrently.
    Wave 2: task_c (depends on task_a and task_b) runs after wave 1 merges.
    """
    data_dir = test_setup["data_dir"]
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]

    t_a = make_task("task_a", "Implement Component A")
    t_b = make_task("task_b", "Implement Component B")
    t_c = make_task("task_c", "Integrate A and B", depends_on=["task_a", "task_b"])
    plan = Plan(summary="Diamond test plan", tasks=[t_a, t_b, t_c])

    active_workers = 0
    max_concurrent_seen = 0
    lock = threading.Lock()

    def mock_worker_runner(cmd: list[str], cwd: Path | None = None, **kwargs: Any) -> str:
        nonlocal active_workers, max_concurrent_seen
        with lock:
            active_workers += 1
            if active_workers > max_concurrent_seen:
                max_concurrent_seen = active_workers

        # Simulate work by writing a file into the worktree
        assert cwd is not None and cwd.is_dir()
        task_id = cwd.name.split("_", 1)[1]
        (cwd / f"{task_id}.txt").write_text(f"Content from {task_id}\n", encoding="utf-8")

        time.sleep(0.05)
        with lock:
            active_workers -= 1
        return f"Completed {task_id}"

    worker = AcpWorker(runner=mock_worker_runner, harness_path="/usr/bin/agy")

    # Review always approves
    def mock_review(task: Task, diff: str, ctx: dict[str, Any]) -> Review:
        assert len(diff) > 0
        return Review(decision="approved", findings=[], confidence=1.0)

    builder = ParallelBuilder(settings=settings, store=store, worker=worker)
    completed = builder.build_tasks(
        project=project,
        job=job,
        plan=plan,
        instructions_for=lambda t: f"Implement {t.id}",
        cancelled=lambda: False,
        review_for=mock_review,
    )

    assert set(completed) == {"task_a", "task_b", "task_c"}
    assert max_concurrent_seen <= 2

    # Check root repository on target branch
    root = ensure_repo(data_dir, project["id"])
    assert (root / "task_a.txt").exists()
    assert (root / "task_b.txt").exists()
    assert (root / "task_c.txt").exists()
    assert (root / "task_a.txt").read_text() == "Content from task_a\n"

    # Check store task records
    tasks = {t["id"]: t for t in store.tasks_for_job(job["id"])}
    assert tasks["task_a"]["state"] == "merged"
    assert tasks["task_b"]["state"] == "merged"
    assert tasks["task_c"]["state"] == "merged"

    # Verify no dangling worktrees remain in git
    worktree_list = list_worktrees(root)
    assert len(worktree_list) == 1  # Only the main root worktree


def test_wave_barrier_synchronization(test_setup: dict[str, Any]):
    """Verify that Wave Barrier waits for all workers in the wave to finish before review and merge."""
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]

    t1 = make_task("task_fast")
    t2 = make_task("task_slow")
    plan = Plan(summary="Wave barrier sync test", tasks=[t1, t2])

    worker_finished = {"task_fast": False, "task_slow": False}
    review_checked_at_barrier = []

    def mock_worker_runner(cmd: list[str], cwd: Path | None = None, **kwargs: Any) -> str:
        assert cwd is not None
        task_id = cwd.name.split("_", 1)[1]
        (cwd / f"{task_id}.txt").write_text("done", encoding="utf-8")
        if task_id == "task_slow":
            time.sleep(0.1)
        worker_finished[task_id] = True
        return "ok"

    def mock_review(task: Task, diff: str, ctx: dict[str, Any]) -> Review:
        # At the review stage of any wave task, all workers in wave must have finished
        review_checked_at_barrier.append(dict(worker_finished))
        return Review(decision="approved", findings=[])

    worker = AcpWorker(runner=mock_worker_runner, harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)
    builder.build_tasks(
        project=project,
        job=job,
        plan=plan,
        instructions_for="Build task",
        review_for=mock_review,
    )

    # For every review, both fast and slow workers must have already finished
    assert len(review_checked_at_barrier) == 2
    for state in review_checked_at_barrier:
        assert state["task_fast"] is True
        assert state["task_slow"] is True


def test_review_rejection_and_repair_retry(test_setup: dict[str, Any]):
    """Test that a task rejected by review is retried with repair feedback and passes on attempt 2."""
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]

    task = make_task("retry_task", acceptance=["Return 200"])
    plan = Plan(summary="Retry plan", tasks=[task])

    instructions_received = []

    def mock_worker_runner(
        cmd: list[str], cwd: Path | None = None, input_data: bytes | None = None, **kwargs: Any
    ) -> str:
        assert cwd is not None
        if input_data:
            instructions_received.append(input_data.decode("utf-8"))
        (cwd / "code.py").write_text("fixed = True\n", encoding="utf-8")
        return "attempt done"

    attempts_seen = 0

    def mock_review(t: Task, diff: str, ctx: dict[str, Any]) -> Review:
        nonlocal attempts_seen
        attempts_seen += 1
        if attempts_seen == 1:
            return Review(decision="rejected", findings=["Missing unit tests", "Bad naming"])
        return Review(decision="approved", findings=[])

    worker = AcpWorker(runner=mock_worker_runner, harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)
    completed = builder.build_tasks(
        project=project,
        job=job,
        plan=plan,
        instructions_for=lambda t, ctx: f"Implement {t.id} (attempt {ctx.get('attempt')})",
        review_for=mock_review,
    )

    assert completed == ["retry_task"]
    assert attempts_seen == 2

    # Second worker execution must have received repair instructions with findings
    assert len(instructions_received) == 2
    assert "Missing unit tests" in instructions_received[1]
    assert "Bad naming" in instructions_received[1]

    # Check store task records
    t_rec = store.task(job["id"], "retry_task")
    assert t_rec["state"] == "merged"


def test_merge_conflict_handling_and_retry(test_setup: dict[str, Any]):
    """Test that concurrent tasks with conflicting file edits trigger conflict repair."""
    data_dir = test_setup["data_dir"]
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]

    # Initial base file in repository
    root = ensure_repo(data_dir, project["id"], {"shared.txt": "Original line 1\nOriginal line 2\n"})

    t1 = make_task("conflict_a")
    t2 = make_task("conflict_b")
    plan = Plan(summary="Conflict plan", tasks=[t1, t2])

    t2_attempts = 0

    def mock_worker_runner(cmd: list[str], cwd: Path | None = None, **kwargs: Any) -> str:
        nonlocal t2_attempts
        assert cwd is not None
        task_id = cwd.name.split("_", 1)[1]
        shared_file = cwd / "shared.txt"

        if task_id == "conflict_a":
            shared_file.write_text("Modified by A\nOriginal line 2\n", encoding="utf-8")
        else:
            t2_attempts += 1
            if t2_attempts == 1:
                # First attempt: conflicting change on line 1
                shared_file.write_text("Conflicting edit by B\nOriginal line 2\n", encoding="utf-8")
            else:
                # Second attempt: appended change without conflict
                shared_file.write_text("Modified by A\nOriginal line 2\nAppended by B\n", encoding="utf-8")
        return "done"

    worker = AcpWorker(runner=mock_worker_runner, harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)
    completed = builder.build_tasks(
        project=project,
        job=job,
        plan=plan,
        instructions_for="Edit shared file",
        review_for=lambda t, diff, ctx: Review(decision="approved", findings=[]),
    )

    assert set(completed) == {"conflict_a", "conflict_b"}
    assert t2_attempts == 2

    # Verify both changes present in root repository
    content = (root / "shared.txt").read_text(encoding="utf-8")
    assert "Modified by A" in content
    assert "Appended by B" in content


@pytest.mark.parametrize("worker_fails", [False, True])
def test_max_repairs_exceeded(test_setup: dict[str, Any], worker_fails: bool):
    """Test that exhausted attempts fail and ACP errors remain available."""
    data_dir = test_setup["data_dir"]
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]
    settings.max_repairs = 2

    task = make_task("failing_task")
    plan = Plan(summary="Failing plan", tasks=[task])

    def mock_worker_runner(cmd: list[str], cwd: Path | None = None, **kwargs: Any) -> str:
        if worker_fails:
            raise AcpWorkerError("Model unavailable")
        assert cwd is not None
        (cwd / "fail.py").write_text("# incomplete\n", encoding="utf-8")
        return "done"

    # Review always rejects
    def mock_review(t: Task, diff: str, ctx: dict[str, Any]) -> Review:
        return Review(decision="rejected", findings=["Permanent flaw detected"])

    worker = AcpWorker(runner=mock_worker_runner, harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)

    with pytest.raises(ParallelBuildError, match="failed after 2 attempts|rejected after 2 attempts"):
        builder.build_tasks(
            project=project,
            job=job,
            plan=plan,
            instructions_for="Do work",
            review_for=mock_review,
        )

    t_rec = store.task(job["id"], "failing_task")
    assert t_rec["state"] == "failed"
    errors = store.all("SELECT detail FROM events WHERE job_id=? AND kind='acp.error' ORDER BY seq", (job["id"],))
    assert [json.loads(event["detail"])["attempt"] for event in errors] == ([1, 2] if worker_fails else [])

    # Verify worktree directory is cleaned up
    root = ensure_repo(data_dir, project["id"])
    assert len(list_worktrees(root)) == 1


def test_resume_runs_only_unmerged_tasks(test_setup: dict[str, Any]):
    store, project, original = (test_setup[key] for key in ("store", "project", "job"))
    settings = test_setup["settings"]
    settings.max_repairs = 1
    plan = Plan(summary="Resume test", tasks=[
        make_task("setup"), make_task("feature", depends_on=["setup"]),
    ])

    def run_original(cmd, *, cwd, **kwargs):
        if (cwd / "setup.txt").exists():
            raise AcpWorkerError("Original ACP failed")
        (cwd / "setup.txt").write_text("merged", encoding="utf-8")
        return "done"

    def run_retry(cmd, *, cwd, **kwargs):
        assert cmd[0] == "opencode-test"
        assert (cwd / "setup.txt").read_text(encoding="utf-8") == "merged"
        (cwd / "feature.txt").write_text("done", encoding="utf-8")
        return "done"

    def run(job, worker):
        builder = ParallelBuilder(settings=settings, store=store, worker=worker)
        return builder.build_tasks(
            project=project, job=job, plan=plan, instructions_for="Do work",
            review_for=lambda *_: Review(decision="approved", findings=[]),
        )

    original_worker = AcpWorker(runner=run_original, harness_path="/usr/bin/agy")
    with pytest.raises(ParallelBuildError, match="Original ACP failed"):
        run(original, original_worker)
    store.finish(original["id"], "failed")

    retry = store.enqueue(project["id"], "build", retry_of=original["id"])
    retry_worker = AcpWorker(agent_name="opencode", harness_path="opencode-test", runner=run_retry)
    assert run(retry, retry_worker) == ["feature", "setup"]
    assert [worker["task_id"] for worker in store.workers_for(retry["id"])] == ["feature"]
    assert json.loads(store.task(retry["id"], "setup")["detail"]) == {"reused": True}
    assert store.task(original["id"], "feature")["state"] == "failed"


def test_cancellation_aborts_immediately(test_setup: dict[str, Any]):
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]

    t1 = make_task("task_cancel")
    plan = Plan(summary="Cancel plan", tasks=[t1])

    worker = AcpWorker(runner=lambda *args, **kwargs: "done", harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)

    with pytest.raises(ParallelBuildError, match="cancelled"):
        builder.build_tasks(
            project=project,
            job=job,
            plan=plan,
            instructions_for="Do work",
            cancelled=lambda: True,
        )


def test_worktree_cleanup_on_worker_crash(test_setup: dict[str, Any]):
    data_dir = test_setup["data_dir"]
    store = test_setup["store"]
    project = test_setup["project"]
    job = test_setup["job"]
    settings = test_setup["settings"]
    settings.max_repairs = 1

    task = make_task("crashing_task")
    plan = Plan(summary="Crash plan", tasks=[task])

    def mock_crash(*args: Any, **kwargs: Any) -> str:
        raise AcpWorkerError("Hard worker crash")

    worker = AcpWorker(runner=mock_crash, harness_path="/usr/bin/agy")
    builder = ParallelBuilder(settings=settings, store=store, worker=worker)

    with pytest.raises(ParallelBuildError, match="Hard worker crash"):
        builder.build_tasks(
            project=project,
            job=job,
            plan=plan,
            instructions_for="Crash me",
        )

    # Verify no dangling worktrees remain in git
    root = ensure_repo(data_dir, project["id"])
    assert len(list_worktrees(root)) == 1
