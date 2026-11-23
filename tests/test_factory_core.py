"""Core unit tests for ACP Software Factory:
- Plan DAG validation and topological scheduling
- Products repository and Git worktree isolation
- SQLite durable store and transactions
"""
import pytest

from factory import db, products
from factory.models import AcceptanceFlow, Plan, Review, Task


# ==============================================================================
# 1. DAG Validation & Topological Scheduling Tests
# ==============================================================================


def make_task(name: str, depends_on: list[str] | None = None, skills: list[str] | None = None) -> Task:
    return Task(
        id=name,
        title=f"Task {name}",
        instructions=f"Instructions for implementing component {name}.",
        acceptance=["Acceptance criterion met"],
        depends_on=depends_on or [],
        skills=skills or [],
    )


def make_plan(*tasks: Task, flows: list[AcceptanceFlow] | None = None) -> Plan:
    return Plan(
        summary="A test plan for factory DAG validation.",
        tasks=list(tasks),
        test_flows=flows or [],
    )


def test_task_model_validation():
    t = make_task("auth", skills=["design-system"])
    assert t.id == "auth"
    assert t.skills == ["design-system"]

    # Invalid task ID
    with pytest.raises(ValueError):
        Task(id="123_invalid", title="Test", instructions="Ten chars min", acceptance=["ok"])

    # Duplicate skills within task
    with pytest.raises(ValueError, match="unique"):
        make_task("auth", skills=["design", "design"])

    # Invalid skill name formatting
    with pytest.raises(ValueError, match="Invalid skill name"):
        make_task("auth", skills=["Invalid_Skill"])


def test_acceptance_flow_normalization_and_validation():
    # Browser flow normalizes camelCase selector names to snake_case
    flow = AcceptanceFlow(
        id="login_flow",
        kind="browser",
        goal="Operator logs into the application dashboard",
        steps=["Open login page", "Enter credentials", "Click submit"],
        selectors={"submitBtn": "button#submit", "user_name": "input#username"},
    )
    assert "submit_btn" in flow.selectors
    assert "user_name" in flow.selectors

    # Browser flow requires declared selectors
    with pytest.raises(ValueError, match="selectors"):
        AcceptanceFlow(
            id="empty_selectors",
            kind="browser",
            goal="Browser flow without declared selectors",
            steps=["Step 1"],
            selectors={},
        )

    # API flow does not require selectors
    api_flow = AcceptanceFlow(
        id="health_check",
        kind="api",
        goal="Check that the health endpoint returns 200",
        steps=["GET /api/health"],
    )
    assert api_flow.kind == "api"


def test_review_model_decisions():
    # Standard decision
    rev_approved = Review(decision="approved", findings=[], confidence=0.95)
    assert rev_approved.approved is True
    assert rev_approved.decision == "approved"

    rev_rejected = Review(decision="rejected", findings=["Missing tests"], confidence=0.8)
    assert rev_rejected.approved is False

    # Legacy approved field support
    legacy = Review(approved=True, findings=[])  # type: ignore[call-arg]
    assert legacy.decision == "approved"
    assert legacy.approved is True


def test_independent_tasks_share_parallel_frontier():
    t_content = make_task("content")
    t_layout = make_task("layout")
    t_home = make_task("home", ["content", "layout"])

    plan = make_plan(t_content, t_layout, t_home)

    assert [t.id for t in plan.ready_set([])] == ["content", "layout"]
    assert [t.id for t in plan.ready_set(["content"])] == ["layout"]
    assert [t.id for t in plan.ready_set(["content", "layout"])] == ["home"]
    assert [t.id for t in plan.ready_set(["content", "layout", "home"])] == []


def test_forward_references_allowed():
    t_after = make_task("after", ["before"])
    t_before = make_task("before")

    plan = make_plan(t_after, t_before)
    assert plan.execution_order() == ["before", "after"]
    assert [t.id for t in plan.ready_set([])] == ["before"]


def test_dag_cycle_detection():
    t_a = make_task("a", ["b"])
    t_b = make_task("b", ["a"])

    with pytest.raises(ValueError, match="cycle"):
        make_plan(t_a, t_b)


def test_dag_self_dependency():
    t_self = make_task("self_dep", ["self_dep"])
    with pytest.raises(ValueError, match="cannot depend on itself"):
        make_plan(t_self)


def test_dag_unknown_dependency():
    t_orphan = make_task("orphan", ["non_existent"])
    with pytest.raises(ValueError, match="unknown tasks"):
        make_plan(t_orphan)


def test_dag_duplicate_task_ids():
    t1 = make_task("dup")
    t2 = make_task("dup")
    with pytest.raises(ValueError, match="unique"):
        make_plan(t1, t2)


def test_diamond_graph_execution_and_waves():
    # Diamond graph:
    #       root
    #      /    \
    #   branch1  branch2
    #      \    /
    #       join
    t_root = make_task("root")
    t_b1 = make_task("b1", ["root"])
    t_b2 = make_task("b2", ["root"])
    t_join = make_task("join", ["b1", "b2"])

    plan = make_plan(t_root, t_b1, t_b2, t_join)

    assert plan.execution_order() == ["root", "b1", "b2", "join"]
    assert plan.waves() == [["root"], ["b1", "b2"], ["join"]]
    assert [t.id for t in plan.ordered_tasks()] == ["root", "b1", "b2", "join"]


# ==============================================================================
# 2. Products & Git Worktree Isolation Tests
# ==============================================================================


@pytest.fixture
def product_dir(tmp_path):
    return tmp_path / "data"


def test_ensure_repo_creation_and_idempotency(product_dir):
    root = products.ensure_repo(product_dir, "proj_1")
    assert (root / ".git").is_dir()
    assert (root / ".gitignore").is_file()
    assert products.target_branch(root) == "main"
    initial_commit = products.head_commit(root)
    assert len(initial_commit) == 40

    # Idempotent call returns identical repo without modifying HEAD
    root2 = products.ensure_repo(product_dir, "proj_1")
    assert root2 == root
    assert products.head_commit(root2) == initial_commit


def test_ensure_repo_with_seed_files(product_dir):
    seed = {
        "README.md": "# Test Project\n",
        "src/app.py": "print('hello')\n",
    }
    root = products.ensure_repo(product_dir, "seeded_proj", seed_files=seed)
    assert (root / "README.md").read_text() == "# Test Project\n"
    assert (root / "src" / "app.py").read_text() == "print('hello')\n"
    assert products.head_commit(root)


def test_worktree_isolation_and_commit(product_dir):
    root = products.ensure_repo(product_dir, "demo")
    base_commit = products.head_commit(root)

    wt1_path = product_dir / "wt" / "w1"
    wt2_path = product_dir / "wt" / "w2"

    wt1 = products.worktree_add(root, wt1_path, "worker/t1")
    wt2 = products.worktree_add(root, wt2_path, "worker/t2")

    (wt1 / "task1.txt").write_text("task 1 content\n")
    (wt2 / "task2.txt").write_text("task 2 content\n")

    c1 = products.commit_all(wt1, "finish task 1")
    c2 = products.commit_all(wt2, "finish task 2")

    assert c1 != base_commit
    assert c2 != base_commit

    # Isolation: wt1 does not see task2.txt, wt2 does not see task1.txt, root sees neither
    assert not (wt1 / "task2.txt").exists()
    assert not (wt2 / "task1.txt").exists()
    assert not (root / "task1.txt").exists()
    assert not (root / "task2.txt").exists()

    assert products.changed_files(wt1, base_commit) == ["task1.txt"]
    assert products.changed_files(wt2, base_commit) == ["task2.txt"]

    # Worktree removal and cleanup
    products.worktree_remove(root, wt1)
    assert not wt1.exists()


def test_diff_against_base_and_truncation(product_dir):
    root = products.ensure_repo(product_dir, "demo_diff")
    base_commit = products.head_commit(root)

    wt_path = product_dir / "wt" / "diff_worker"
    wt = products.worktree_add(root, wt_path, "worker/diff")

    (wt / "small.txt").write_text("hello small world\n")
    products.commit_all(wt, "add small.txt")

    diff = products.diff_against_base(wt, base_commit)
    assert "small.txt" in diff
    assert "+hello small world" in diff

    # Diff truncation limit
    (wt / "large.txt").write_text("x" * 2000)
    products.commit_all(wt, "add large.txt")

    truncated_diff = products.diff_against_base(wt, base_commit, max_bytes=500)
    assert "DIFF TRUNCATED" in truncated_diff
    assert len(truncated_diff) > 500


def test_merge_branch_success_and_delete(product_dir):
    root = products.ensure_repo(product_dir, "merge_demo")
    wt_path = product_dir / "wt" / "m1"
    wt = products.worktree_add(root, wt_path, "worker/merge1")

    (wt / "feature.py").write_text("def feature(): pass\n")
    products.commit_all(wt, "implement feature")

    # Clean up worktree before merge
    products.worktree_remove(root, wt)

    success, message = products.merge_branch(root, "worker/merge1")
    assert success is True
    assert (root / "feature.py").exists()

    # Branch deletion
    assert products.branch_exists(root, "worker/merge1") is True
    products.branch_delete(root, "worker/merge1")
    assert products.branch_exists(root, "worker/merge1") is False


def test_merge_branch_conflict_aborts_cleanly(product_dir):
    root = products.ensure_repo(product_dir, "conflict_demo")

    # Set up file on main
    (root / "conflict.txt").write_text("base content\n")
    products.commit_all(root, "base commit")
    base_commit = products.head_commit(root)

    # Worker 1 modifies conflict.txt
    wt1 = products.worktree_add(root, product_dir / "wt" / "c1", "worker/c1")
    (wt1 / "conflict.txt").write_text("worker 1 modification\n")
    products.commit_all(wt1, "worker 1 change")
    products.worktree_remove(root, wt1)

    # Worker 2 modifies same line in conflict.txt
    wt2 = products.worktree_add(root, product_dir / "wt" / "c2", "worker/c2")
    (wt2 / "conflict.txt").write_text("worker 2 modification\n")
    products.commit_all(wt2, "worker 2 change")
    products.worktree_remove(root, wt2)

    # Merge worker 1 first -> succeeds
    ok1, _ = products.merge_branch(root, "worker/c1")
    assert ok1 is True

    # Merge worker 2 -> conflicts and aborts cleanly
    ok2, err = products.merge_branch(root, "worker/c2")
    assert ok2 is False
    assert err == "Merge conflict"

    # Repo is not left in a merge state
    assert products.head_commit(root) != base_commit
    status = products._run(["status", "--porcelain"], cwd=root)
    assert status == ""


# ==============================================================================
# 3. SQLite Durable Store Tests
# ==============================================================================


@pytest.fixture
def store(tmp_path):
    return db.Store(tmp_path / "factory.db")


def test_store_table_initialization(store):
    tables = [row["name"] for row in store.all("SELECT name FROM sqlite_master WHERE type='table'")]
    for expected in ("projects", "jobs", "tasks", "events", "workers"):
        assert expected in tables


def test_store_transaction_commit_and_rollback(store):
    # Success transaction commits
    with store.tx() as conn:
        conn.execute("INSERT INTO runtime(key, value) VALUES(?, ?)", ("test_key", "val1"))

    assert store.one("SELECT value FROM runtime WHERE key='test_key'")["value"] == "val1"

    # Failing transaction rolls back
    with pytest.raises(RuntimeError):
        with store.tx() as conn:
            conn.execute("INSERT INTO runtime(key, value) VALUES(?, ?)", ("fail_key", "val2"))
            raise RuntimeError("Transaction fail")

    assert len(store.all("SELECT * FROM runtime WHERE key='fail_key'")) == 0


def test_store_events_logging(store):
    store.event("proj_10", "job_10", "task.started", {"step": 1})
    store.event("proj_10", "job_10", "task.completed", {"step": 2})

    events = store.all("SELECT * FROM events WHERE project_id='proj_10' ORDER BY seq")
    assert len(events) == 2
    assert events[0]["kind"] == "task.started"
    assert events[1]["kind"] == "task.completed"


def test_store_project_and_job_lifecycle(store):
    proj = store.create_project({"name": "App Alpha", "brief": "Build a SaaS app", "profile": "static"})
    proj_id = proj["id"]
    assert proj["name"] == "App Alpha"
    assert proj["state"] == "draft"

    job = store.enqueue(proj_id, "build", payload={"revision": 0}, next_state="building")
    job_id = job["id"]
    assert job["state"] == "queued"
    assert store.project(proj_id)["state"] == "building"

    # Claim job in lane
    claimed = store.claim("build")
    assert claimed is not None
    assert claimed["id"] == job_id
    assert claimed["state"] == "running"

    # Finish job
    store.finish(job_id, "succeeded", result={"artifacts": ["dist"]})
    finished_job = store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    assert finished_job["state"] == "succeeded"


def test_store_worker_and_task_state_synchronization(store):
    proj = store.create_project({"name": "Sync App", "brief": "Sync worker tasks", "profile": "static"})
    job = store.enqueue(proj["id"], "build")
    job_id = job["id"]

    worker = store.create_worker(
        project_id=proj["id"],
        job_id=job_id,
        task_id="task_backend",
        branch="worker/backend",
        base_commit="abc1234",
        depends_on=["task_db"],
    )
    assert worker["task_id"] == "task_backend"
    assert worker["state"] == "queued"

    # Update state via set_task_state
    store.set_task_state(job_id, "task_backend", "running", detail={"step": "compiling"})

    updated_task = store.task(job_id, "task_backend")
    assert updated_task["state"] == "running"

    updated_worker = store.worker(worker["id"])
    assert updated_worker["state"] == "running"

    # Merge task
    store.set_worker(worker["id"], state="merged", commit_sha="def5678")
    assert store.worker_states(job_id)["task_backend"] == "merged"
    assert store.completed_tasks(job_id) == {"task_backend"}


def test_tasks_merged_for_project_with_reachability(store):
    proj = store.create_project({"name": "Reachability App", "brief": "Testing git commit reachability"})
    proj_id = proj["id"]
    job = store.enqueue(proj_id, "build")
    job_id = job["id"]

    w1 = store.create_worker(proj_id, job_id, "t1", "b1", "base", revision=1)
    w2 = store.create_worker(proj_id, job_id, "t2", "b2", "base", revision=1)

    store.set_worker(w1["id"], state="merged", commit_sha="sha_1")
    store.set_worker(w2["id"], state="merged", commit_sha="sha_2")

    # Present commits contains sha_1 but not sha_2
    present_commits = {"sha_1", "sha_root"}
    merged = store.tasks_merged_for_project(proj_id, revision=1, present_commits=present_commits)
    assert merged == {"t1"}

    # Present commits contains both
    present_commits.add("sha_2")
    merged_both = store.tasks_merged_for_project(proj_id, revision=1, present_commits=present_commits)
    assert merged_both == {"t1", "t2"}
