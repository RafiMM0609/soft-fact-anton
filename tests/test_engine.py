"""End-to-end and unit tests for Config, Verifier, Tester, Engine, and CLI."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any
import httpx
import pytest

from acp.agent import AcpAgent
from acp.worker import AcpWorker
from factory import (
    Engine,
    Settings,
    Tester,
    Verifier,
)
from factory import cli
from factory.db import Store
from factory.models import AcceptanceFlow, Plan, Task
from factory.products import commit_all, ensure_repo, head_commit


# ==============================================================================
# Helper fixtures and builders
# ==============================================================================


def make_task(
    name: str,
    title: str = "",
    instructions: str = "",
    depends_on: list[str] | None = None,
    acceptance: list[str] | None = None,
) -> Task:
    return Task(
        id=name,
        title=title or f"Task {name}",
        instructions=instructions or f"Instructions for {name}",
        acceptance=acceptance or [f"Criterion for {name} met."],
        depends_on=depends_on or [],
    )


# ==============================================================================
# 1. Config & Settings Tests
# ==============================================================================


def test_settings_defaults(tmp_path: Path):
    s = Settings(data=tmp_path / "custom_data")
    assert s.data == tmp_path / "custom_data"
    assert s.data_dir == tmp_path / "custom_data"
    assert s.db_path == tmp_path / "custom_data" / "factory.db"
    assert s.products_dir == tmp_path / "custom_data" / "products"
    assert s.harness == "agy"
    assert s.worker_concurrency == 1
    assert s.max_repairs == 5
    assert s.agent_timeout == 600
    assert s.worker_timeout == 1800
    assert len(s.operator_token) > 10


def test_settings_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("FACTORY_DATA", str(tmp_path / "env_data"))
    monkeypatch.setenv("FACTORY_AGY", "/opt/custom/agy")
    monkeypatch.setenv("FACTORY_HARNESS", "custom-agent")
    monkeypatch.setenv("FACTORY_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("FACTORY_MAX_REPAIRS", "5")
    monkeypatch.setenv("FACTORY_AGENT_TIMEOUT", "120")
    monkeypatch.setenv("FACTORY_WORKER_TIMEOUT", "600")
    monkeypatch.setenv("FACTORY_OPERATOR_TOKEN", "secret-test-token-12345")

    s = Settings()
    assert s.data == (tmp_path / "env_data").resolve()
    assert s.agy == "/opt/custom/agy"
    assert s.harness == "custom-agent"
    assert s.worker_concurrency == 4
    assert s.max_repairs == 5
    assert s.agent_timeout == 120
    assert s.worker_timeout == 600
    assert s.operator_token == "secret-test-token-12345"


def test_settings_auto_detect_and_non_agy_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake_agy = bin_dir / "agy"
    fake_agy.write_text("#!/bin/sh\necho agy\n")
    fake_agy.chmod(0o755)
    fake_codex = bin_dir / "codex"
    fake_codex.write_text("#!/bin/sh\necho codex\n")
    fake_codex.chmod(0o755)

    monkeypatch.delenv("FACTORY_AGY", raising=False)
    monkeypatch.delenv("FACTORY_HARNESS", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    s_default = Settings(data=tmp_path / "data1")
    assert s_default.agy == str(fake_agy.resolve())
    assert s_default.resolve_harness_path() == str(fake_agy.resolve())

    monkeypatch.setenv("FACTORY_HARNESS", "codex")
    monkeypatch.setenv("FACTORY_CODEX", str(fake_codex))
    s_codex = Settings(data=tmp_path / "data2")
    engine = Engine(settings=s_codex)
    assert engine.agent.harness_path == str(fake_codex.resolve())
    assert engine.worker.harness_path == str(fake_codex.resolve())


# ==============================================================================
# 2. Verifier Tests
# ==============================================================================


def test_verifier_run_command_success(tmp_path: Path):
    v = Verifier()
    code, output = v.run_command([sys.executable, "-c", "print('hello verifier')"], cwd=tmp_path)
    assert code == 0
    assert "hello verifier" in output


def test_verifier_run_command_failure(tmp_path: Path):
    v = Verifier()
    code, output = v.run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('syntax error'); sys.exit(2)"],
        cwd=tmp_path,
    )
    assert code == 2
    assert "syntax error" in output


def test_verifier_run_command_not_found(tmp_path: Path):
    v = Verifier()
    code, output = v.run_command(["non_existent_binary_xyz_123"], cwd=tmp_path)
    assert code == -1
    assert "not found" in output


def test_verifier_run_command_timeout(tmp_path: Path):
    v = Verifier()
    code, output = v.run_command(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout=1,
    )
    assert code == -1
    assert "timed out" in output


def test_verifier_verify_pipeline(tmp_path: Path):
    v = Verifier()
    # Passing pipeline
    cmds = [
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        [sys.executable, "-c", "print('step 2 ok')"],
    ]
    ok, report = v.verify(tmp_path, commands=cmds)
    assert ok is True
    assert "passed" in report

    # Failing pipeline
    failing_cmds = [
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        [sys.executable, "-c", "import sys; sys.exit(1)"],
    ]
    failed_ok, failed_report = v.verify(tmp_path, commands=failing_cmds)
    assert failed_ok is False
    assert "failed" in failed_report


# ==============================================================================
# 3. Acceptance Tester Tests
# ==============================================================================


def test_tester_api_flow():
    def mock_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        elif request.url.path == "/api/items":
            return httpx.Response(201, json={"id": 1, "name": "widget"})
        return httpx.Response(404, text="Not Found")

    client = httpx.Client(transport=httpx.MockTransport(mock_transport), base_url="http://testserver")
    tester = Tester(http_client=client)

    flow_success = AcceptanceFlow(
        id="health_check",
        kind="api",
        goal="Verify service health endpoint",
        steps=["GET /api/health", "POST /api/items"],
    )

    all_passed, results = tester.run_flows([flow_success])
    assert all_passed is True
    assert len(results) == 1
    assert results[0]["passed"] is True
    assert "succeeded" in results[0]["detail"]

    # Failing flow
    flow_fail = AcceptanceFlow(
        id="missing_endpoint",
        kind="api",
        goal="Check endpoint that does not exist",
        steps=["GET /api/missing"],
    )
    fail_passed, fail_results = tester.run_flows([flow_fail])
    assert fail_passed is False
    assert fail_results[0]["passed"] is False
    assert "HTTP 404" in fail_results[0]["detail"]


def test_tester_browser_flow_simulation():
    tester = Tester()
    flow = AcceptanceFlow(
        id="login_interaction",
        kind="browser",
        goal="User navigates and enters credentials",
        steps=["Open login modal", "Enter username", "Click login button"],
        selectors={
            "modal": "div.modal",
            "username": "input[name='user']",
            "submit_button": "button#submit",
        },
    )

    all_passed, results = tester.run_flows([flow])
    assert all_passed is True
    assert len(results) == 1
    assert results[0]["passed"] is True
    assert "selectors" in results[0]["detail"]


# ==============================================================================
# 4. Engine Planning Tests
# ==============================================================================


def test_engine_plan_brief(tmp_path: Path):
    settings = Settings(data=tmp_path / "data")
    store = Store(settings.data / "factory.db")

    proj = store.create_project({
        "name": "Planner Test",
        "brief": "Build a responsive static landing page",
        "profile": "static",
    })

    plan_payload = {
        "summary": "Plan for building a responsive landing page",
        "tasks": [
            {
                "id": "html_structure",
                "title": "HTML layout skeleton",
                "instructions": "Create index.html with header, main, and footer sections.",
                "acceptance": ["index.html exists and contains valid HTML5 semantic tags."],
                "depends_on": [],
            },
            {
                "id": "css_styling",
                "title": "CSS stylesheet",
                "instructions": "Create style.css with flexbox layout and modern styling.",
                "acceptance": ["style.css is linked in index.html."],
                "depends_on": ["html_structure"],
            },
        ],
        "test_flows": [
            {
                "id": "landing_page_view",
                "kind": "browser",
                "goal": "Verify landing page renders key elements",
                "steps": ["Visit index page", "Check header"],
                "selectors": {"main_header": "header.hero"},
            }
        ],
    }

    def mock_runner(cmd: list[str], **kwargs: Any) -> str:
        return json.dumps(plan_payload)

    agent = AcpAgent(runner=mock_runner)
    engine = Engine(settings=settings, store=store, agent=agent)

    plan = engine.plan_brief(proj, brief=proj["brief"])
    assert isinstance(plan, Plan)
    assert len(plan.tasks) == 2
    assert plan.tasks[0].id == "html_structure"
    assert plan.tasks[1].depends_on == ["html_structure"]

    # Verify plan was persisted to the database
    db_proj = store.project(proj["id"])
    assert db_proj["plan"] is not None
    saved_plan = Plan.model_validate_json(db_proj["plan"])
    assert len(saved_plan.tasks) == 2


# ==============================================================================
# 5. Engine End-to-End Build Tests
# ==============================================================================


def test_engine_build_full_flow(tmp_path: Path):
    """Test full build flow:
    Brief -> Plan -> 2 Task Waves (Worktree -> Worker -> Diff -> Review -> Merge) -> Verifier -> Artifact
    """
    settings = Settings(data=tmp_path / "data", worker_concurrency=1)
    store = Store(settings.data / "factory.db")

    proj = store.create_project({
        "name": "E2E Test",
        "brief": "Build an HTML and CSS landing page",
        "profile": "static",
    })

    store.enqueue(proj["id"], kind="build", payload={"brief": proj["brief"]})
    claimed = store.claim("build")
    assert claimed is not None

    # Setup Plan: Wave 1 (layout), Wave 2 (styles)
    plan = Plan(
        summary="Two-wave test plan",
        tasks=[
            make_task("layout", title="Create HTML layout", depends_on=[]),
            make_task("styles", title="Add CSS styles", depends_on=["layout"]),
        ],
        test_flows=[
            AcceptanceFlow(
                id="check_html",
                kind="browser",
                goal="Check layout elements",
                steps=["Visit home page"],
                selectors={"title_element": "h1"},
            )
        ],
    )

    # Mock Worker: creates files in worktree and commits
    def mock_worker_runner(cmd: list[str], cwd: Path | str | None = None, **kwargs: Any) -> str:
        assert cwd is not None
        cwd_path = Path(cwd)
        # Check branch or task from git branch
        import subprocess

        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
        )
        branch = res.stdout.strip()

        if "layout" in branch:
            (cwd_path / "index.html").write_text(
                "<!DOCTYPE html><html><body><h1>Welcome</h1></body></html>\n",
                encoding="utf-8",
            )
        elif "styles" in branch:
            (cwd_path / "styles.css").write_text("body { margin: 0; background: #fff; }\n", encoding="utf-8")

        commit_all(cwd_path, f"Implement {branch}")
        return f"Completed work on {branch}"

    worker = AcpWorker(runner=mock_worker_runner)

    # Mock Agent: Diff Reviewer approves changes
    def mock_agent_runner(cmd: list[str], **kwargs: Any) -> str:
        return '{"decision": "approved", "findings": [], "confidence": 0.99}'

    agent = AcpAgent(runner=mock_agent_runner)

    engine = Engine(
        settings=settings,
        store=store,
        agent=agent,
        worker=worker,
    )

    # Execute build
    artifact = engine.build(project=proj, job=claimed, plan=plan)

    # Assertions on build artifact
    assert artifact["status"] == "succeeded"
    assert artifact["project_id"] == proj["id"]
    assert artifact["job_id"] == claimed["id"]
    assert artifact["tasks_completed"] == ["layout", "styles"]
    assert artifact["commit_sha"] is not None

    # Check store state
    db_job = store.one("SELECT * FROM jobs WHERE id=?", (claimed["id"],))
    assert db_job["state"] == "succeeded"

    db_proj = store.project(proj["id"])
    assert db_proj["state"] == "built"
    assert db_proj["artifact"] is not None

    # Verify repository files on main branch
    repo_root = ensure_repo(settings.data, proj["id"])
    assert (repo_root / "index.html").is_file()
    assert (repo_root / "styles.css").is_file()
    assert head_commit(repo_root) == artifact["commit_sha"]


def test_engine_build_auto_plans_when_none(tmp_path: Path):
    """Engine generates plan automatically if none is provided."""
    settings = Settings(data=tmp_path / "data")
    store = Store(settings.data / "factory.db")

    proj = store.create_project({
        "name": "Auto Plan Test",
        "brief": "Create an automated project",
        "profile": "static",
    })
    store.enqueue(proj["id"], kind="build")
    claimed = store.claim("build")
    assert claimed is not None

    plan_json = json.dumps({
        "summary": "Auto generated plan",
        "tasks": [
            {
                "id": "single_task",
                "title": "Single Task",
                "instructions": "Write output file",
                "acceptance": ["File created"],
                "depends_on": [],
            }
        ],
        "test_flows": [],
    })

    def mock_agent_runner(cmd: list[str], input_data: bytes | None = None, **kwargs: Any) -> str:
        text = input_data.decode("utf-8") if input_data else ""
        if "planner" in text.lower() or "planner" in str(cmd).lower():
            return plan_json
        return '{"decision": "approved", "findings": [], "confidence": 0.95}'

    def mock_worker_runner(cmd: list[str], cwd: Path | str | None = None, **kwargs: Any) -> str:
        assert cwd is not None
        p = Path(cwd)
        (p / "out.txt").write_text("Success\n", encoding="utf-8")
        commit_all(p, "Commit single task")
        return "Worker done"

    agent = AcpAgent(runner=mock_agent_runner)
    worker = AcpWorker(runner=mock_worker_runner)

    engine = Engine(settings=settings, store=store, agent=agent, worker=worker)
    artifact = engine.build(project=proj, job=claimed, plan=None)

    assert artifact["status"] == "succeeded"
    assert artifact["tasks_completed"] == ["single_task"]


# ==============================================================================
# 6. CLI Command Tests
# ==============================================================================


def test_cli_doctor(capsys: pytest.CaptureFixture):
    exit_code = cli.main(["doctor"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Doctor Diagnostics" in captured.out
    assert "[OK] Python" in captured.out
    assert "[OK] SQLite" in captured.out


def test_cli_init(tmp_path: Path, capsys: pytest.CaptureFixture):
    target_data = tmp_path / "factory_data"
    exit_code = cli.main(["init", "--data", str(target_data)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Initialized ACP Software Factory" in captured.out
    assert target_data.is_dir()
    assert (target_data / "factory.db").is_file()
    assert (target_data / ".operator_token").is_file()


def test_cli_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    data_dir = tmp_path / "cli_data"
    cli.main(["init", "--data", str(data_dir)])

    # Mock Engine.build to return test artifact without running full LLM
    def mock_build(self: Engine, project: Any, job: Any, plan: Any = None, **kwargs: Any) -> dict:
        return {
            "project_id": project["id"],
            "job_id": job["id"],
            "commit_sha": "abc123sha",
            "tasks_completed": ["setup"],
            "status": "succeeded",
        }

    monkeypatch.setattr(Engine, "build", mock_build)

    exit_code = cli.main([
        "build",
        "--brief",
        "Create a portfolio page",
        "--product",
        "my-portfolio",
        "--data",
        str(data_dir),
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "abc123sha" in captured.out
    assert "my-portfolio" in captured.out
