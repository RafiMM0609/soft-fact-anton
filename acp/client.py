"""Agent Client Protocol (ACP) asynchronous stdio client.

Provides the AcpClient class for launching and communicating with ACP agent
processes over stdio using JSON-RPC 2.0.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Callable
from typing import Any
from pydantic import BaseModel

from acp.protocol import (
    AcpProcessError,
    AcpRpcError,
    AcpTimeoutError,
    ClientInfo,
    InitializeParams,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
    SessionCancelParams,
    SessionNewParams,
    SessionPromptParams,
)

logger = logging.getLogger(__name__)


class AcpClient:
    """Asynchronous client for Agent Client Protocol over stdio JSON-RPC 2.0.

    Spawns an agent subprocess, multiplexes requests/responses, dispatches notifications,
    and provides convenience methods for ACP sessions.
    """

    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        default_timeout: float = 30.0,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.default_timeout = default_timeout

        self.process: asyncio.subprocess.Process | None = None
        self._pending_requests: dict[int | str, asyncio.Future[JsonRpcResponse]] = {}
        self._notification_handlers: dict[str, list[Callable[..., Any]]] = {}
        self._notification_waiters: list[tuple[str | None, asyncio.Future[JsonRpcNotification]]] = []
        self.notifications: list[JsonRpcNotification] = []
        self.stderr_lines: list[str] = []

        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._id_counter = 0
        self._closed = False

    @property
    def is_running(self) -> bool:
        """Check whether the subprocess is currently running."""
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        """Start the subprocess and background stdio reader loops."""
        if self.is_running:
            return

        self._closed = False
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
            )
        except Exception as exc:
            raise AcpProcessError(f"Failed to start ACP process {self.command}: {exc}") from exc

        self._reader_task = asyncio.create_task(self._stdout_loop(), name="acp_stdout_reader")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="acp_stderr_reader")

    async def stop(self) -> None:
        """Gracefully terminate the subprocess and cancel background loops."""
        self._closed = True

        if self.process is not None and self.process.returncode is None:
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning("Error stopping ACP process: %s", exc)

        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._reader_task = None
        self._stderr_task = None
        self.process = None

        # Fail any remaining pending requests
        abort_err = AcpProcessError("ACP client stopped")
        for fut in list(self._pending_requests.values()):
            if not fut.done():
                fut.set_exception(abort_err)
        self._pending_requests.clear()

        # Reject any notification waiters
        for _, waiter in list(self._notification_waiters):
            if not waiter.done():
                waiter.set_exception(abort_err)
        self._notification_waiters.clear()

    async def __aenter__(self) -> AcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def on_notification(self, method: str, handler: Callable[..., Any]) -> None:
        """Register a callback for incoming notifications.

        Args:
            method: Method name (e.g. "session/update") or "*" for all notifications.
            handler: Callable taking (JsonRpcNotification) or (dict). Can be sync or async.
        """
        handlers = self._notification_handlers.setdefault(method, [])
        handlers.append(handler)

    async def wait_for_notification(
        self, method: str | None = None, timeout: float = 10.0
    ) -> JsonRpcNotification:
        """Wait asynchronously for a notification matching method (or any if method is None)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonRpcNotification] = loop.create_future()
        entry = (method, future)
        self._notification_waiters.append(entry)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if entry in self._notification_waiters:
                self._notification_waiters.remove(entry)
            raise AcpTimeoutError(
                f"Timed out after {timeout}s waiting for notification '{method or '*'}'"
            )

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Serialize and write a JSON-RPC message line to subprocess stdin."""
        if not self.is_running or self.process is None or self.process.stdin is None:
            raise AcpProcessError("Cannot write to ACP process: process is not running")

        payload = (json.dumps(message) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                self.process.stdin.write(payload)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise AcpProcessError(f"Failed writing to subprocess stdin: {exc}") from exc

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | BaseModel | None = None,
        timeout: float | None = None,
    ) -> JsonRpcResponse:
        """Send a JSON-RPC request and wait for its matching response.

        Args:
            method: The RPC method name.
            params: Parameters dictionary or Pydantic model.
            timeout: Maximum seconds to wait. Defaults to default_timeout.

        Returns:
            The parsed JsonRpcResponse.
        """
        if not self.is_running:
            raise AcpProcessError("Cannot send request: ACP process is not running")

        req_id = self._next_id()
        params_dict: dict[str, Any]
        if isinstance(params, BaseModel):
            params_dict = params.model_dump(exclude_none=True)
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = {}

        request = JsonRpcRequest(id=req_id, method=method, params=params_dict)

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[JsonRpcResponse] = loop.create_future()
        self._pending_requests[req_id] = response_future

        effective_timeout = timeout if timeout is not None else self.default_timeout

        try:
            await self._write_message(request.model_dump(exclude_none=True))
            return await asyncio.wait_for(response_future, timeout=effective_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise AcpTimeoutError(
                f"Request '{method}' (id={req_id}) timed out after {effective_timeout}s"
            )
        except Exception:
            self._pending_requests.pop(req_id, None)
            raise

    async def call(
        self,
        method: str,
        params: dict[str, Any] | BaseModel | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a request and unwrap the result, raising AcpRpcError if an error occurred."""
        response = await self.send_request(method, params, timeout=timeout)
        if response.error is not None:
            raise AcpRpcError(
                code=response.error.code,
                message=response.error.message,
                data=response.error.data,
            )
        return response.result

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | BaseModel | None = None,
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        params_dict: dict[str, Any]
        if isinstance(params, BaseModel):
            params_dict = params.model_dump(exclude_none=True)
        elif isinstance(params, dict):
            params_dict = params
        else:
            params_dict = {}

        notification = JsonRpcNotification(method=method, params=params_dict)
        await self._write_message(notification.model_dump(exclude_none=True))

    def _dispatch_notification(self, notification: JsonRpcNotification) -> None:
        """Dispatch an incoming notification to registered handlers and waiters."""
        self.notifications.append(notification)

        # Notify any waiters
        for entry in list(self._notification_waiters):
            method_filter, future = entry
            if method_filter is None or method_filter == notification.method:
                if not future.done():
                    future.set_result(notification)
                self._notification_waiters.remove(entry)

        # Notify callbacks
        handlers = self._notification_handlers.get(notification.method, []) + self._notification_handlers.get("*", [])
        for handler in handlers:
            try:
                res = handler(notification)
                if inspect.isawaitable(res):
                    asyncio.create_task(res)
            except Exception as exc:
                logger.exception("Error in notification handler for %s: %s", notification.method, exc)

    async def _stdout_loop(self) -> None:
        """Continuously read line-delimited JSON messages from subprocess stdout."""
        assert self.process is not None and self.process.stdout is not None
        while not self._closed:
            try:
                line_bytes = await self.process.stdout.readline()
            except Exception:
                break

            if not line_bytes:
                # Subprocess closed stdout / exited
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Received non-JSON line from ACP process stdout: %s", line)
                continue

            if not isinstance(data, dict):
                continue

            # Identify message type: response vs notification
            req_id = data.get("id")
            if req_id is not None:
                # Response message
                fut = self._pending_requests.pop(req_id, None)
                if fut is None and isinstance(req_id, int):
                    fut = self._pending_requests.pop(str(req_id), None)
                elif fut is None and isinstance(req_id, str) and req_id.isdigit():
                    fut = self._pending_requests.pop(int(req_id), None)

                if fut is not None and not fut.done():
                    try:
                        response = JsonRpcResponse.model_validate(data)
                        fut.set_result(response)
                    except Exception as exc:
                        fut.set_exception(exc)
            elif "method" in data:
                # Notification message
                try:
                    notification = JsonRpcNotification.model_validate(data)
                    self._dispatch_notification(notification)
                except Exception as exc:
                    logger.warning("Failed parsing notification %r: %s", line, exc)

        # If stdout closed while requests pending, reject them
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1.0)
            except Exception:
                pass
        exit_code = self.process.returncode if self.process else None
        stderr_tail = "\n".join(self.stderr_lines[-20:]) if self.stderr_lines else ""
        err_msg = f"ACP subprocess stdout closed unexpectedly (exit code {exit_code})"
        if stderr_tail:
            err_msg += f"\nStderr tail:\n{stderr_tail}"
        err = AcpProcessError(err_msg)
        for fut in list(self._pending_requests.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending_requests.clear()

    async def _stderr_loop(self) -> None:
        """Continuously read lines from subprocess stderr for debugging."""
        assert self.process is not None and self.process.stderr is not None
        while not self._closed:
            try:
                line_bytes = await self.process.stderr.readline()
            except Exception:
                break

            if not line_bytes:
                break

            line = line_bytes.decode("utf-8", errors="replace").strip()
            if line:
                self.stderr_lines.append(line)
                logger.debug("ACP stderr: %s", line)

    # -------------------------------------------------------------------------
    # High-level ACP Protocol Helpers
    # -------------------------------------------------------------------------

    async def initialize(
        self,
        protocol_version: int | str = 1,
        client_info: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        """Perform the ACP initialize handshake.

        Args:
            protocol_version: Protocol version (default 1).
            client_info: Client identification info.
            timeout: Timeout in seconds.

        Returns:
            The server's initialization response payload.
        """
        c_info = ClientInfo(**client_info) if client_info else ClientInfo()
        params = InitializeParams(protocolVersion=protocol_version, clientInfo=c_info)
        return await self.call("initialize", params, timeout=timeout)

    async def session_new(
        self,
        cwd: str,
        timeout: float = 30.0,
        **extra: Any,
    ) -> str:
        """Create a new ACP session.

        Args:
            cwd: Working directory for the session.
            timeout: Timeout in seconds.
            extra: Extra parameters passed to session/new.

        Returns:
            The created sessionId string.
        """
        params = SessionNewParams(cwd=cwd, **extra)
        result = await self.call("session/new", params, timeout=timeout)
        if isinstance(result, dict) and "sessionId" in result:
            return str(result["sessionId"])
        return str(result)

    async def session_prompt(
        self,
        session_id: str,
        prompt: str | list[dict[str, Any]],
        timeout: float | None = None,
    ) -> Any:
        """Send a prompt to an active ACP session.

        Args:
            session_id: The session ID.
            prompt: The user/instruction prompt string or content block list.
            timeout: Timeout in seconds.

        Returns:
            The prompt execution result from the server.
        """
        prompt_content = (
            [{"type": "text", "text": prompt}] if isinstance(prompt, str) else prompt
        )
        params = SessionPromptParams(sessionId=session_id, prompt=prompt_content)
        return await self.call("session/prompt", params, timeout=timeout)

    async def session_cancel(
        self,
        session_id: str,
        timeout: float = 10.0,
    ) -> Any:
        """Cancel an ongoing ACP session prompt execution.

        Args:
            session_id: The session ID to cancel.
            timeout: Timeout in seconds.

        Returns:
            The cancellation result from the server.
        """
        params = SessionCancelParams(sessionId=session_id)
        return await self.call("session/cancel", params, timeout=timeout)
