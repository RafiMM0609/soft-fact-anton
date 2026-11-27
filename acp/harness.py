"""Harness runner and environment isolation utilities for ACP factory.

Provides functions to locate harness executables (agy, codex, pi, claude),
build ACP and non-interactive print commands, prepare isolated environments,
and execute bounded subprocesses.
"""

from __future__ import annotations

from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import threading
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


def _first_executable(candidates: list[Path]) -> str | None:
    for candidate in candidates:
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def resolve_harness(agent_name: str = "agy") -> str:
    """Find and validate a local harness executable for the specified agent.

    Resolution order for 'agy':
      1. os.environ.get("FACTORY_AGY")
      2. shutil.which("agy") / shutil.which("antigravity")
      3. ~/.local/bin/{agy,antigravity}, /usr/local/bin/agy, /usr/bin/agy, /opt/homebrew/bin/agy

    Supports 'opencode', 'codex', 'pi', 'claude' via FACTORY_<NAME>, PATH, and ~/.local/bin.

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

        which_agy = shutil.which("agy")
        if which_agy:
            return which_agy

        which_anti = shutil.which("antigravity")
        if which_anti:
            return which_anti

        home = Path.home()
        found = _first_executable(
            [
                home / ".local" / "bin" / "agy",
                home / ".local" / "bin" / "antigravity",
                Path("/usr/local/bin/agy"),
                Path("/usr/bin/agy"),
                Path("/opt/homebrew/bin/agy"),
                Path("/home/ubuntu/.local/bin/agy"),
            ]
        )
        if found:
            return found

        raise FileNotFoundError(
            "Harness executable 'agy' not found in FACTORY_AGY, PATH, or ~/.local/bin"
        )

    if normalized == "opencode":
        env_val = os.environ.get("FACTORY_OPENCODE")
        if env_val:
            candidate = Path(env_val).expanduser().resolve()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
            raise FileNotFoundError(
                f"Configured FACTORY_OPENCODE binary not executable or not found: {env_val}"
            )

        which_oc = shutil.which("opencode")
        if which_oc:
            if os.name == "nt" and Path(which_oc).suffix.lower() == ".cmd":
                npm_exe = Path(which_oc).parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
                if npm_exe.is_file():
                    return str(npm_exe)
            return which_oc

        home = Path.home()
        found = _first_executable(
            [
                home / ".local" / "bin" / "opencode",
                home / ".opencode" / "bin" / "opencode",
                Path("/usr/local/bin/opencode"),
                Path("/opt/homebrew/bin/opencode"),
            ]
        )
        if found:
            return found

        raise FileNotFoundError(
            "Harness executable 'opencode' not found in FACTORY_OPENCODE, PATH, or ~/.local/bin"
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

    home = Path.home()
    found = _first_executable(
        [
            home / ".local" / "bin" / normalized,
            Path("/usr/local/bin") / normalized,
            Path("/opt/homebrew/bin") / normalized,
        ]
    )
    if found:
        return found

    raise FileNotFoundError(
        f"Harness executable '{normalized}' not found in {env_key}, PATH, or ~/.local/bin"
    )


def detect_available_harnesses(
    agents: tuple[str, ...] = ("agy", "opencode", "codex", "pi", "claude"),
) -> dict[str, str]:
    """Detect all locally available harness executables."""
    detected: dict[str, str] = {}
    for name in agents:
        try:
            detected[name] = resolve_harness(name)
        except FileNotFoundError:
            continue
    return detected


def get_harness_path(agent_name: str = "agy") -> str:
    """Alias for resolve_harness."""
    return resolve_harness(agent_name)


@lru_cache(maxsize=8)
def opencode_supports_standalone(executable: str) -> bool:
    """Check whether OpenCode can run with a private server."""
    try:
        result = subprocess.run(
            [executable, "run", "--help"],
            env=isolated_env(),
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return b"--standalone" in result.stdout


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
    elif normalized == "opencode":
        cmd = [executable, "acp"]
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
    elif normalized == "opencode":
        cmd = [executable, "run", "--format", "json"]
        if model:
            cmd.extend(["--model", model])
        cmd.append(instruction)
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

        # Preserve opencode configuration and credentials without leaking session db/history
        real_oc_cfg = real_home / ".config" / "opencode" / "opencode.json"
        if real_oc_cfg.is_file():
            target_oc_cfg_dir = home_path / ".config" / "opencode"
            target_oc_cfg_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                shutil.copy2(real_oc_cfg, target_oc_cfg_dir / "opencode.json")
            except OSError:
                pass

        target_oc_share = home_path / ".local" / "share" / "opencode"
        oc_auth_sources = (
            real_home / ".local" / "share" / "opencode" / "auth.json",
            real_home / ".opencode-local" / "data" / "opencode" / "auth.json",
        )
        for oc_auth in oc_auth_sources:
            if oc_auth.is_file():
                target_oc_share.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    shutil.copy2(oc_auth, target_oc_share / "auth.json")
                except OSError:
                    pass
                break

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
    assert process.stdout is not None
    stdout = process.stdout

    def append_chunk(chunk: bytes) -> None:
        nonlocal truncated
        output.extend(chunk)
        if len(output) > max_output:
            excess = len(output) - max_output
            del output[:excess]
            truncated += excess

    def read_windows_pipe() -> None:
        with stdout:
            while chunk := stdout.read1(65536):
                append_chunk(chunk)

    reader = None
    selector = None

    try:
        if os.name == "nt":
            # Windows selectors handle sockets, but cannot monitor subprocess pipes.
            reader = threading.Thread(target=read_windows_pipe, daemon=True)
            reader.start()
        else:
            selector = selectors.DefaultSelector()
            selector.register(stdout, selectors.EVENT_READ)

        while (reader is not None and reader.is_alive()) or (selector is not None and selector.get_map()):
            if cancelled():
                raise AcpProcessError("Cancellation requested")
            if time.monotonic() > deadline:
                tail = output.decode("utf-8", errors="replace")[-100000:]
                raise AcpProcessError(
                    f"Process exceeded its {timeout}s deadline and was stopped: {tail}"
                )
            if reader is not None:
                reader.join(timeout=0.2)
            elif selector is not None:
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if chunk:
                        append_chunk(chunk)
                    else:
                        selector.unregister(key.fileobj)

        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text = f"[OUTPUT TRUNCATED: discarded {truncated} earlier characters]\n" + text
        if code != 0:
            raise AcpProcessError(f"Process failed with exit code {code}: {text[-10000:]}")
        return text
    finally:
        if selector is not None:
            selector.close()
        if reader is None:
            stdout.close()
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        process.wait()
        if reader is not None:
            reader.join(timeout=0.2)
