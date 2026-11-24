"""Harness runner and environment isolation utilities for ACP factory.

Provides functions to locate harness executables (agy, codex, pi, claude),
build ACP and non-interactive print commands, prepare isolated environments,
and execute bounded subprocesses.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import time
from typing import Any, Callable

from pydantic import BaseModel

from acp.protocol import AcpProcessError

logger = logging.getLogger(__name__)

# Sensitive environment variables that must never leak to child workers or untrusted tasks
WITHHELD_ENV_VARS = (
    "FACTORY_OPERATOR_TOKEN",
    "FACTORY_RELEASE_TOKEN",
    "OPERATOR_TOKEN",
    "RELEASE_TOKEN",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "SSH_AUTH_SOCK",
)

# Standard environment variables safe to inherit
SAFE_BASE_ENV_VARS = (
    "PATH",
    "TERM",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "CI",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)

# Model provider API keys and tokens that model runners legitimately need
PROVIDER_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "ZAI_API_KEY",
    "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "COPILOT_GITHUB_TOKEN",
    "OPENCODE_API_KEY",
    "FACTORY_MODEL",
    "FACTORY_THINKING",
)


def resolve_harness(agent_name: str = "agy") -> str:
    """Find and validate a local harness executable for the specified agent.

    Resolution order for 'agy':
      1. os.environ.get("FACTORY_AGY")
      2. /home/ubuntu/.local/bin/agy
      3. shutil.which("agy")
      4. shutil.which("antigravity")

    Supports 'codex', 'pi', 'claude' via FACTORY_<NAME> and shutil.which.

    Raises:
        FileNotFoundError: If the binary cannot be found or is not executable.
    """
    normalized = agent_name.strip().lower()

    if normalized == "agy":
        env_val = os.environ.get("FACTORY_AGY")
        if env_val:
            candidate = Path(env_val).expanduser().resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            raise FileNotFoundError(f"Configured FACTORY_AGY binary not executable or not found: {env_val}")

        default_local = Path("/home/ubuntu/.local/bin/agy")
        if default_local.is_file() and os.access(default_local, os.X_OK):
            return str(default_local)

        which_agy = shutil.which("agy")
        if which_agy:
            return which_agy

        which_anti = shutil.which("antigravity")
        if which_anti:
            return which_anti

        raise FileNotFoundError(
            "Harness executable 'agy' not found in FACTORY_AGY, /home/ubuntu/.local/bin/agy, or PATH"
        )

    # Generic or other known agents (codex, pi, claude)
    env_key = f"FACTORY_{normalized.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        candidate = Path(env_val).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise FileNotFoundError(f"Configured {env_key} binary not executable or not found: {env_val}")

    which_agent = shutil.which(normalized)
    if which_agent:
        return which_agent

    raise FileNotFoundError(f"Harness executable '{normalized}' not found in {env_key} or PATH")


def get_harness_path(agent_name: str = "agy") -> str:
    """Alias for resolve_harness."""
    return resolve_harness(agent_name)


def acp_command(
    worktree: Path | str | None = None,
    agent_name: str = "agy",
    harness_path: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the command-line arguments to launch an ACP stdio server.

    Args:
        worktree: Optional worktree directory to attach.
        agent_name: Name of harness agent (default 'agy').
        harness_path: Explicit executable path override.
        extra_args: Additional command-line flags.

    Returns:
        List of command argument strings.
    """
    executable = harness_path or resolve_harness(agent_name)
    normalized = agent_name.strip().lower()

    if normalized == "agy":
        cmd = [executable, "acp", "--dangerously-skip-permissions"]
        if worktree is not None:
            cmd.extend(["--add-dir", str(Path(worktree).resolve())])
    else:
        cmd = [executable, "acp"]
        if worktree is not None:
            cmd.extend(["--add-dir", str(Path(worktree).resolve())])

    if extra_args:
        cmd.extend(extra_args)
    return cmd


def print_command(
    instruction: str,
    schema: type[BaseModel] | dict[str, Any] | str | None = None,
    mode: str = "auto",
    agent_name: str = "agy",
    harness_path: str | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build command-line arguments for non-interactive execution.

    Args:
        instruction: Prompt or instruction text.
        schema: Optional Pydantic model class, dict, or JSON schema string.
        mode: Execution mode ('auto', 'plan', 'accept-edits').
        agent_name: Name of harness agent (default 'agy').
        harness_path: Explicit executable path override.
        model: Optional model identifier.
        extra_args: Additional command-line flags.

    Returns:
        List of command argument strings.
    """
    executable = harness_path or resolve_harness(agent_name)
    normalized = agent_name.strip().lower()

    if normalized == "agy":
        cmd = [executable, "--print", instruction, "--output-format", "json"]
        if mode:
            cmd.extend(["--mode", mode])
        if schema is not None:
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                schema_str = json.dumps(schema.model_json_schema())
            elif isinstance(schema, dict):
                schema_str = json.dumps(schema)
            else:
                schema_str = str(schema)
            cmd.extend(["--json-schema", schema_str])
        if model:
            cmd.extend(["--model", model])
    elif normalized == "pi":
        cmd = [executable, "--print", "--mode", "json"]
        if model:
            cmd.extend(["--model", model])
    else:
        cmd = [executable, "--print", instruction]
        if model:
            cmd.extend(["--model", model])

    if extra_args:
        cmd.extend(extra_args)
    return cmd


def playwright_browsers_path() -> str:
    """Return the shared Playwright browser directory if present, else empty string."""
    configured = os.environ.get("FACTORY_PLAYWRIGHT_BROWSERS")
    if configured:
        candidate = Path(configured)
    else:
        home = os.environ.get("HOME")
        if not home:
            return ""
        candidate = Path(home) / ".cache" / "ms-playwright"
    try:
        if not candidate.is_dir():
            return ""
        if not any(child.is_dir() for child in candidate.iterdir()):
            return ""
    except OSError:
        return ""
    return str(candidate)


def isolated_env(
    worktree: Path | str | None = None,
    temp_home: Path | str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct an isolated environment dictionary for worker or agent processes.

    Redirects HOME and XDG directories into temp_home and scrubs sensitive tokens.

    Args:
        worktree: Optional path to worktree root.
        temp_home: Temporary directory to use as HOME.
        base_env: Optional base environment to filter (defaults to os.environ).

    Returns:
        Cleaned, isolated environment dictionary.
    """
    source = dict(os.environ) if base_env is None else dict(base_env)

    # Collect sensitive token values to prevent leaking under different keys
    sensitive_values: set[str] = set()
    for key in WITHHELD_ENV_VARS:
        val = source.get(key)
        if val and len(val) >= 8:
            sensitive_values.add(val)

    env: dict[str, str] = {}
    for k, v in source.items():
        if k in WITHHELD_ENV_VARS:
            continue
        if k.startswith("FACTORY_OPERATOR_") or k.startswith("FACTORY_RELEASE_"):
            continue
        if any(secret in v for secret in sensitive_values):
            continue
        env[k] = v

    if temp_home is not None:
        home_path = Path(temp_home).resolve()
        home_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        (home_path / ".config").mkdir(parents=True, exist_ok=True, mode=0o700)
        (home_path / ".cache").mkdir(parents=True, exist_ok=True, mode=0o700)
        (home_path / ".local" / "share").mkdir(parents=True, exist_ok=True, mode=0o700)

        # Preserve agy/gemini CLI authentication without leaking parent conversation history or transcripts
        real_home = Path.home()
        real_gemini_cli = real_home / ".gemini" / "antigravity-cli"
        target_gemini_cli = home_path / ".gemini" / "antigravity-cli"
        target_gemini_cli.mkdir(parents=True, exist_ok=True, mode=0o700)

        if real_gemini_cli.is_dir():
            for fname in ("antigravity-oauth-token", "settings.json", "installation_id"):
                src = real_gemini_cli / fname
                if src.is_file():
                    try:
                        shutil.copy2(src, target_gemini_cli / fname)
                    except OSError:
                        pass
            real_builtin = real_gemini_cli / "builtin"
            target_builtin = target_gemini_cli / "builtin"
            if real_builtin.is_dir() and not target_builtin.exists():
                try:
                    target_builtin.symlink_to(real_builtin)
                except OSError:
                    pass

        # Preserve agy/gemini CLI project configs (e.g. default-cli-project.json)
        real_gemini_config = real_home / ".gemini" / "config"
        if real_gemini_config.is_dir():
            target_gemini_config = home_path / ".gemini" / "config"
            target_gemini_config.mkdir(parents=True, exist_ok=True, mode=0o700)
            real_projects = real_gemini_config / "projects"
            if real_projects.is_dir():
                target_projects = target_gemini_config / "projects"
                target_projects.mkdir(parents=True, exist_ok=True, mode=0o700)
                for pjson in real_projects.glob("*.json"):
                    try:
                        shutil.copy2(pjson, target_projects / pjson.name)
                    except OSError:
                        pass

        env["HOME"] = str(home_path)
        env["XDG_CONFIG_HOME"] = str(home_path / ".config")
        env["XDG_CACHE_HOME"] = str(home_path / ".cache")
        env["XDG_DATA_HOME"] = str(home_path / ".local" / "share")
        env["AGY_APP_DATA_DIR"] = str(home_path / ".gemini" / "antigravity-cli")
        env["PI_CODING_AGENT_DIR"] = str(home_path / ".pi" / "agent")
        env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
        env["PI_TELEMETRY_DISABLED"] = "1"

    browsers = playwright_browsers_path()
    if browsers:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers

    if worktree is not None:
        env["PWD"] = str(Path(worktree).resolve())

    return env


def run_process(
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    input_data: bytes = b"",
    timeout: float = 300.0,
    cancelled: Callable[[], bool] = lambda: False,
    max_output: int = 4_000_000,
) -> str:
    """Execute a process with deadline, bounded output memory, and group cleanup."""
    if cancelled():
        raise AcpProcessError("Process cancelled before execution")

    cwd_str = str(cwd) if cwd is not None else None

    process = subprocess.Popen(
        argv,
        cwd=cwd_str,
        env=env,
        stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    if input_data and process.stdin:
        try:
            process.stdin.write(input_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    output = bytearray()
    truncated = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)

    try:
        while selector.get_map():
            if cancelled():
                raise AcpProcessError("Cancellation requested")
            if time.monotonic() > deadline:
                tail = output.decode("utf-8", errors="replace")[-100000:]
                raise AcpProcessError(
                    f"Process exceeded its {timeout}s deadline and was stopped: {tail}"
                )
            for key, _ in selector.select(timeout=0.2):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
                    if len(output) > max_output:
                        excess = len(output) - max_output
                        del output[:excess]
                        truncated += excess

        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text = f"[OUTPUT TRUNCATED: discarded {truncated} earlier characters]\n" + text
        if code != 0:
            raise AcpProcessError(f"Process failed with exit code {code}: {text[-10000:]}")
        return text
    finally:
        selector.close()
        if process.stdout:
            process.stdout.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        process.wait()
