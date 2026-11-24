"""Unit tests for ACP harness, AcpAgent structured generation, and AcpWorker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import pytest
from pydantic import BaseModel, Field

from acp.agent import AcpAgent, clean_markdown_fences, parse_schema_output
from acp.harness import acp_command, isolated_env, print_command, resolve_harness
from acp.protocol import AcpProcessError
from acp.worker import AcpWorker, AcpWorkerError

MOCK_SERVER_PATH = Path(__file__).parent / "mock_server.py"


# ---------------------------------------------------------------------------
# Test Schemas
# ---------------------------------------------------------------------------


class PlanStep(BaseModel):
    step_number: int
    title: str
    command: str


class PlanSchema(BaseModel):
    summary: str
    steps: list[PlanStep] = Field(default_factory=list)


class ReviewSchema(BaseModel):
    approved: bool
    verdict: str
    comments: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Harness Discovery & Commands Tests
# ---------------------------------------------------------------------------


def test_resolve_harness_agy_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_agy = tmp_path / "custom_agy"
    fake_agy.write_text("#!/bin/sh\necho custom agy\n")
    fake_agy.chmod(0o755)

    monkeypatch.setenv("FACTORY_AGY", str(fake_agy))
    resolved = resolve_harness("agy")
    assert resolved == str(fake_agy.resolve())


def test_resolve_harness_default_or_local() -> None:
    resolved = resolve_harness("agy")
    assert resolved.endswith("agy")
    assert os.path.isfile(resolved)


def test_resolve_harness_custom_agent_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\necho codex\n")
    fake_codex.chmod(0o755)

    monkeypatch.setenv("FACTORY_CODEX", str(fake_codex))
    resolved = resolve_harness("codex")
    assert resolved == str(fake_codex.resolve())


def test_resolve_harness_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FACTORY_NONEXISTENT", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_harness("nonexistent_agent_xyz")


def test_acp_command_agy(tmp_path: Path) -> None:
    cmd = acp_command(worktree=tmp_path, agent_name="agy")
    assert cmd[0].endswith("agy")
    assert cmd[1] == "acp"
    assert "--dangerously-skip-permissions" in cmd
    assert "--add-dir" in cmd
    assert str(tmp_path.resolve()) in cmd


def test_acp_command_other_agent(tmp_path: Path) -> None:
    cmd = acp_command(worktree=tmp_path, agent_name="pi", harness_path="/usr/bin/pi")
    assert cmd == ["/usr/bin/pi", "acp", "--add-dir", str(tmp_path.resolve())]


def test_print_command_agy(tmp_path: Path) -> None:
    cmd = print_command(
        instruction="Generate code",
        schema=PlanSchema,
        mode="auto",
        agent_name="agy",
        model="claude-3-7-sonnet",
    )
    assert cmd[0].endswith("agy")
    assert "--print" in cmd
    assert "Generate code" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--mode" in cmd
    assert "auto" in cmd
    assert "--model" in cmd
    assert "claude-3-7-sonnet" in cmd
    assert "--json-schema" in cmd

    schema_idx = cmd.index("--json-schema") + 1
    schema_data = json.loads(cmd[schema_idx])
    assert "properties" in schema_data
    assert "summary" in schema_data["properties"]


def test_print_command_pi() -> None:
    cmd = print_command(
        instruction="Build app",
        agent_name="pi",
        harness_path="/usr/bin/pi",
        model="gemini-flash",
    )
    assert cmd == ["/usr/bin/pi", "--print", "--mode", "json", "--model", "gemini-flash"]


# ---------------------------------------------------------------------------
# Environment Isolation Tests
# ---------------------------------------------------------------------------


def test_isolated_env_redirection(tmp_path: Path) -> None:
    temp_home = tmp_path / "fake_home"
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    env = isolated_env(worktree=worktree, temp_home=temp_home)

    assert env["HOME"] == str(temp_home.resolve())
    assert env["XDG_CONFIG_HOME"] == str(temp_home.resolve() / ".config")
    assert env["XDG_CACHE_HOME"] == str(temp_home.resolve() / ".cache")
    assert env["XDG_DATA_HOME"] == str(temp_home.resolve() / ".local" / "share")
    assert env["PWD"] == str(worktree.resolve())
    assert (temp_home / ".config").is_dir()
    assert (temp_home / ".cache").is_dir()


def test_isolated_env_withholds_sensitive_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_OPERATOR_TOKEN", "super-secret-token-12345")
    monkeypatch.setenv("FACTORY_RELEASE_TOKEN", "release-secret-67890")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "operator@example.test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-ok-key")

    env = isolated_env(temp_home=tmp_path / "home")

    assert "FACTORY_OPERATOR_TOKEN" not in env
    assert "FACTORY_RELEASE_TOKEN" not in env
    assert "GIT_AUTHOR_EMAIL" not in env
    assert "super-secret-token-12345" not in " ".join(env.values())
    assert "release-secret-67890" not in " ".join(env.values())
    assert env["ANTHROPIC_API_KEY"] == "anthropic-ok-key"


# ---------------------------------------------------------------------------
# AcpAgent Unit Tests (Structured Output & Retries)
# ---------------------------------------------------------------------------


def test_clean_markdown_fences() -> None:
    assert clean_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert clean_markdown_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert clean_markdown_fences('  {"a": 1}  ') == '{"a": 1}'


def test_parse_schema_output() -> None:
    # Direct JSON
    res1 = parse_schema_output('{"approved": true, "verdict": "LGTM"}', ReviewSchema)
    assert res1.approved is True
    assert res1.verdict == "LGTM"

    # Markdown fenced
    res2 = parse_schema_output('```json\n{"approved": false, "verdict": "Issues"}\n```', ReviewSchema)
    assert res2.approved is False

    # CLI Output Wrapper
    wrapped = json.dumps({"output": '{"approved": true, "verdict": "Passed"}'})
    res3 = parse_schema_output(wrapped, ReviewSchema)
    assert res3.approved is True
    assert res3.verdict == "Passed"


def test_acp_agent_generate_success() -> None:
    expected_plan = PlanSchema(
        summary="Build architecture plan",
        steps=[
            PlanStep(step_number=1, title="Setup repository", command="git init"),
            PlanStep(step_number=2, title="Create src", command="mkdir src"),
        ],
    )

    def mock_runner(cmd: list[str], **kwargs: object) -> str:
        return expected_plan.model_dump_json()

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    result = agent.generate(
        role="Planner",
        instruction="Create plan for project",
        context={"project": "acp-factory"},
        schema=PlanSchema,
    )

    assert isinstance(result, PlanSchema)
    assert result.summary == "Build architecture plan"
    assert len(result.steps) == 2
    assert result.steps[0].command == "git init"


def test_acp_agent_generate_markdown_fence() -> None:
    def mock_runner(cmd: list[str], **kwargs: object) -> str:
        return (
            "```json\n"
            '{"summary": "Markdown plan", "steps": [{"step_number": 1, "title": "Init", "command": "echo 1"}]}\n'
            "```"
        )

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    result = agent.generate(
        role="Planner",
        instruction="Plan",
        context={},
        schema=PlanSchema,
    )
    assert result.summary == "Markdown plan"
    assert len(result.steps) == 1


def test_acp_agent_generate_auto_retry_success() -> None:
    calls = 0

    def mock_runner(cmd: list[str], **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            # First attempt: invalid raw text / non-JSON
            return "I am thinking about the plan. Here is step 1..."
        # Second attempt: correct JSON schema
        return '{"summary": "Recovered plan", "steps": []}'

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    result = agent.generate(
        role="Planner",
        instruction="Plan",
        context={},
        schema=PlanSchema,
    )

    assert calls == 2
    assert result.summary == "Recovered plan"
    assert result.steps == []


def test_acp_agent_generate_retry_failure() -> None:
    calls = 0

    def mock_runner(cmd: list[str], **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return "Still invalid response without JSON format"

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    with pytest.raises(Exception):
        agent.generate(
            role="Planner",
            instruction="Plan",
            context={},
            schema=PlanSchema,
        )
    assert calls == 2


def test_acp_agent_cancellation() -> None:
    def mock_runner(cmd: list[str], **kwargs: object) -> str:
        return '{"summary": "Never reached", "steps": []}'

    agent = AcpAgent(runner=mock_runner, harness_path="/usr/bin/agy")
    with pytest.raises(AcpProcessError, match="cancelled"):
        agent.generate(
            role="Planner",
            instruction="Plan",
            context={},
            schema=PlanSchema,
            cancelled=lambda: True,
        )


# ---------------------------------------------------------------------------
# AcpWorker Unit Tests
# ---------------------------------------------------------------------------


def test_acp_worker_with_mock_runner(tmp_path: Path) -> None:
    worktree = tmp_path / "mock_worktree"
    worktree.mkdir()

    expected_log = "[running] Compiling code\n[completed] Build succeeded"

    def mock_runner(cmd: list[str], cwd: Path | None = None, **kwargs: object) -> str:
        assert cwd == worktree
        assert "--dangerously-skip-permissions" in cmd
        return expected_log

    worker = AcpWorker(runner=mock_runner, harness_path="/usr/bin/agy")
    log = worker.run_task(
        worktree=worktree,
        instruction="Fix bug in main.py",
        skills=("python", "testing"),
    )

    assert log == expected_log


def test_acp_worker_with_mock_acp_server(tmp_path: Path) -> None:
    worktree = tmp_path / "mock_worktree"
    worktree.mkdir()

    mock_command = [sys.executable, str(MOCK_SERVER_PATH)]
    worker = AcpWorker(command=mock_command)

    log = worker.run_task(
        worktree=worktree,
        instruction="Refactor database layer",
    )

    assert "Analyzing prompt..." in log
    assert "Execution finished." in log
    assert "Executed:" in log and "Refactor database layer" in log


def test_acp_worker_nonexistent_worktree(tmp_path: Path) -> None:
    non_existent = tmp_path / "no_such_directory"
    worker = AcpWorker(harness_path="/usr/bin/agy")
    with pytest.raises(FileNotFoundError):
        worker.run_task(worktree=non_existent, instruction="do something")


def test_acp_worker_cancellation(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    worker = AcpWorker(harness_path="/usr/bin/agy")
    with pytest.raises(AcpWorkerError, match="cancelled"):
        worker.run_task(
            worktree=worktree,
            instruction="test",
            cancelled=lambda: True,
        )


def test_acp_worker_runner_failure(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def crashing_runner(*args: object, **kwargs: object) -> str:
        raise RuntimeError("Subprocess segmentation fault")

    worker = AcpWorker(runner=crashing_runner, harness_path="/usr/bin/agy")
    with pytest.raises(AcpWorkerError, match="segmentation fault"):
        worker.run_task(worktree=worktree, instruction="test crashing")
