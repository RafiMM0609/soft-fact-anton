"""Build and syntax verification runner for ACP Software Factory."""

from __future__ import annotations

import logging
from pathlib import Path
import subprocess

from checks.secret_scan import scan_directory

logger = logging.getLogger(__name__)

# Bounded output tail to prevent high memory consumption or log explosion
VERIFIER_MAX_OUTPUT_CHARS = 8000


class Verifier:
    """Executes verification and compilation checks on project repositories."""

    def run_command(
        self,
        argv: list[str],
        cwd: Path | str,
        timeout: int = 120,
    ) -> tuple[int, str]:
        """Execute a verification command with a strict timeout and bounded output.

        Args:
            argv: Command and argument list.
            cwd: Working directory.
            timeout: Maximum execution seconds (default 120s).

        Returns:
            Tuple of (exit_code, combined_output).
        """
        cwd_path = Path(cwd)
        try:
            res = subprocess.run(
                argv,
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (res.stdout + ("\n" + res.stderr if res.stderr else "")).strip()
            if len(output) > VERIFIER_MAX_OUTPUT_CHARS:
                output = output[-VERIFIER_MAX_OUTPUT_CHARS:]
            return res.returncode, output
        except subprocess.TimeoutExpired:
            msg = f"Command '{' '.join(argv)}' timed out after {timeout}s"
            logger.warning(msg)
            return -1, msg
        except FileNotFoundError as exc:
            msg = f"Command '{argv[0]}' not found: {exc}"
            logger.warning(msg)
            return -1, msg
        except Exception as exc:
            msg = f"Command '{' '.join(argv)}' failed to execute: {exc}"
            logger.warning(msg)
            return -1, msg

    def verify(
        self,
        root: Path,
        commands: list[list[str]] | None = None,
    ) -> tuple[bool, str]:
        """Verify that all commands execute cleanly (exit code 0).

        Args:
            root: Root path of the project repository.
            commands: Optional list of commands to run. If None or empty, default
                      profile checks are performed.

        Returns:
            Tuple of (success: bool, report: str).
        """
        root_path = Path(root).resolve()

        # Security gate: verify no sensitive secrets or tokens are leaked in repo
        secret_findings = scan_directory(root_path)
        if secret_findings:
            err_lines = [f"- {f.file_path}: [{f.rule}] {f.detail}" for f in secret_findings]
            err = "Security check failed: Leaked secret(s) detected:\n" + "\n".join(err_lines)
            logger.error(err)
            return False, err

        cmds = commands or []

        if not cmds:
            cmds = self._detect_default_commands(root_path)

        if not cmds:
            return True, "No verification commands specified or detected."

        outputs: list[str] = []
        for cmd in cmds:
            cmd_str = " ".join(cmd)
            code, out = self.run_command(cmd, cwd=root_path)
            if code != 0:
                err = f"Verification check failed [{cmd_str}] (exit {code}):\n{out}"
                logger.error(err)
                return False, err
            outputs.append(f"[{cmd_str}] passed")

        return True, "\n".join(outputs)

    def _detect_default_commands(self, root: Path) -> list[list[str]]:
        """Detect standard verification commands based on project files."""
        cmds: list[list[str]] = []
        if (root / "pyproject.toml").is_file():
            cmds.append(["python3", "-m", "compileall", "-q", "."])
        elif (root / "package.json").is_file():
            cmds.append(["node", "-e", "JSON.parse(require('fs').readFileSync('package.json'))"])
        return cmds
