"""Coding worker harness for Git worktree execution.

Provides AcpWorker and AcpWorkerError for running agent sessions inside isolated
worktrees with full tool access, tracking execution transcripts, and reporting errors.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from acp.client import AcpClient
from acp.harness import acp_command, isolated_env
from acp.protocol import AcpError, AcpProcessError, AcpRpcError, AcpTimeoutError, JsonRpcNotification

logger = logging.getLogger(__name__)


class AcpWorkerError(AcpError):
    """Raised when an AcpWorker execution fails or crashes."""

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.output = output


def _run_coroutine_sync(coro: Any) -> Any:
    """Execute an async coroutine synchronously, even inside an active event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class AcpWorker:
    """Coding worker harness confined to a single Git worktree."""

    def __init__(
        self,
        agent_name: str = "agy",
        model: str | None = None,
        runner: Callable[..., str] | None = None,
        harness_path: str | None = None,
        command: list[str] | None = None,
        skills_dir: Path | str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.model = model
        self.runner = runner
        self.harness_path = harness_path
        self.command = command
        self.skills_dir = (
            Path(skills_dir).resolve()
            if skills_dir
            else (Path(__file__).parent.parent / "skills").resolve()
        )

    def _prepare_prompt(
        self,
        instruction: str,
        skills: tuple[str, ...] | list[str],
        worktree_path: Path | None = None,
    ) -> str:
        prompt_parts = [
            "You are an expert autonomous software engineer working strictly within this project repository.",
            "IMPORTANT OPERATIONAL CONSTRAINTS:",
            "- Work strictly inside the current workspace (current working directory). Never explore, inspect, or query parent directories ('..') or external databases.",
            "- Inspect existing files in this workspace to understand conventions, structure, and styling.",
            "- Implement all requirements and acceptance criteria directly by creating or editing files in the workspace.",
            "- Ensure all code, styles, and assets are cleanly written and saved in this directory.",
            "",
            instruction,
        ]
        base_prompt = "\n".join(prompt_parts)

        if not skills:
            return base_prompt

        skills_sections: list[str] = []
        for skill in skills:
            skill_dir = self.skills_dir / skill
            skill_md = skill_dir / "SKILL.md"
            if skill_md.is_file():
                try:
                    content = skill_md.read_text(encoding="utf-8")
                    skills_sections.append(f"\n\n=== LOADED SKILL GUIDELINES: {skill} ===\n{content}\n")
                except Exception as exc:
                    logger.warning("Could not read skill %s: %s", skill, exc)
                    skills_sections.append(f"\n\n- Active Skill: {skill}")
            else:
                skills_sections.append(f"\n\n- Active Skill: {skill}")

            # Also mount into .agents/skills in worktree so agy workspace discovery finds it
            if worktree_path and skill_dir.is_dir():
                target_skill_dir = worktree_path / ".agents" / "skills" / skill
                try:
                    target_skill_dir.parent.mkdir(parents=True, exist_ok=True)
                    if not target_skill_dir.exists():
                        shutil.copytree(skill_dir, target_skill_dir, dirs_exist_ok=True)
                except Exception as exc:
                    logger.debug("Could not copy skill %s to worktree: %s", skill, exc)

        return base_prompt + "".join(skills_sections)

    def run_task(
        self,
        worktree: Path | str,
        instruction: str,
        timeout: int = 1800,
        cancelled: Callable[[], bool] = lambda: False,
        skills: tuple[str, ...] | list[str] = (),
    ) -> str:
        """Run a coding worker task synchronously against the specified worktree.

        Args:
            worktree: Path to the isolated Git worktree directory.
            instruction: Task instructions for the worker agent.
            timeout: Execution timeout in seconds (default 1800s / 30m).
            cancelled: Callback returning True if task has been cancelled.
            skills: Optional list of skill names or paths to provide.

        Returns:
            Transcript log output summary of the execution.

        Raises:
            AcpWorkerError: If execution fails, crashes, or times out.
            FileNotFoundError: If worktree directory does not exist.
        """
        if cancelled():
            raise AcpWorkerError("Worker task was cancelled before starting", output="")

        worktree_path = Path(worktree).resolve()
        if not worktree_path.is_dir():
            raise FileNotFoundError(f"Worktree directory not found: {worktree_path}")

        prompt_text = self._prepare_prompt(instruction, skills, worktree_path=worktree_path)

        with tempfile.TemporaryDirectory(prefix="acp_worker_home_") as tmp_home_dir:
            temp_home = Path(tmp_home_dir)
            env = isolated_env(worktree=worktree_path, temp_home=temp_home)
            is_opencode = self.agent_name.strip().lower() == "opencode"
            if is_opencode:
                env["OPENCODE_PURE"] = "1"

            cmd = self.command or acp_command(
                worktree=worktree_path,
                agent_name=self.agent_name,
                harness_path=self.harness_path,
            )

            # If an explicit runner callable was supplied, execute via runner
            if self.runner is not None:
                try:
                    return self.runner(
                        cmd,
                        cwd=worktree_path,
                        env=env,
                        input_data=prompt_text.encode(),
                        timeout=timeout,
                        cancelled=cancelled,
                    )
                except AcpWorkerError:
                    raise
                except Exception as exc:
                    out = getattr(exc, "output", "")
                    raise AcpWorkerError(f"Worker process failed: {exc}", output=out) from exc

            # Otherwise, execute via asynchronous AcpClient session wrapped synchronously
            async def _run_acp_session() -> str:
                transcript_lines: list[str] = []

                def handle_notification(notif: JsonRpcNotification) -> None:
                    params = notif.params
                    if isinstance(params, dict):
                        update = params.get("update")
                        delta = None
                        if isinstance(update, dict):
                            content = update.get("content")
                            if isinstance(content, dict):
                                delta = content.get("text")
                            elif isinstance(content, str):
                                delta = content
                            elif "sessionUpdate" in update:
                                delta = f"[{update['sessionUpdate']}]"
                        if not delta:
                            delta = params.get("delta") or params.get("content") or params.get("message")
                        status = params.get("status")
                        if delta:
                            line = f"[{status}] {delta}" if status else str(delta)
                            transcript_lines.append(line)

                client = AcpClient(
                    cmd,
                    cwd=str(worktree_path),
                    env=env,
                    default_timeout=float(timeout),
                )
                client.on_notification("session/update", handle_notification)
                client.on_notification("*", handle_notification)

                try:
                    async with client:
                        if cancelled():
                            raise AcpWorkerError("Task cancelled", output="\n".join(transcript_lines))

                        await client.initialize()
                        session_options = {"mcpServers": []} if is_opencode else {}
                        session_id = await client.session_new(cwd=str(worktree_path), **session_options)

                        if cancelled():
                            await client.session_cancel(session_id)
                            raise AcpWorkerError("Task cancelled", output="\n".join(transcript_lines))

                        if is_opencode and self.model:
                            try:
                                await client.call(
                                    "session/set_config_option",
                                    {"sessionId": session_id, "configId": "model", "value": self.model},
                                )
                            except AcpRpcError as exc:
                                if exc.code != -32601:
                                    raise
                                await client.call(
                                    "session/set_model",
                                    {"sessionId": session_id, "modelId": self.model},
                                )

                        result = await client.session_prompt(
                            session_id, prompt_text, timeout=float(timeout)
                        )

                        if isinstance(result, dict):
                            out = result.get("output") or result.get("result") or str(result)
                        else:
                            out = str(result)

                        if out:
                            transcript_lines.append(out)

                        return "\n".join(transcript_lines)
                except (AcpProcessError, AcpTimeoutError) as exc:
                    out = "\n".join(transcript_lines)
                    detail = str(exc)
                    if isinstance(exc, AcpTimeoutError) and client.stderr_lines:
                        detail += "\nStderr tail:\n" + "\n".join(client.stderr_lines[-20:])[-6000:]
                    raise AcpWorkerError(f"ACP Worker error: {detail}", output=out) from exc
                except AcpWorkerError:
                    raise
                except Exception as exc:
                    out = "\n".join(transcript_lines)
                    raise AcpWorkerError(f"ACP Worker failed: {exc}", output=out) from exc

            try:
                return _run_coroutine_sync(_run_acp_session())
            except AcpWorkerError:
                raise
            except Exception as exc:
                try:
                    for logf in temp_home.glob("**/*.log"):
                        if logf.stat().st_size > 0:
                            target_log = Path("data/logs") / logf.name
                            target_log.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(logf, target_log)
                except Exception:
                    pass
                out = getattr(exc, "output", "")
                raise AcpWorkerError(f"Worker execution failed: {exc}", output=out) from exc

    async def arun_task(
        self,
        worktree: Path | str,
        instruction: str,
        timeout: int = 1800,
        cancelled: Callable[[], bool] = lambda: False,
        skills: tuple[str, ...] | list[str] = (),
    ) -> str:
        """Asynchronous variant of run_task."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.run_task,
            worktree,
            instruction,
            timeout,
            cancelled,
            skills,
        )
