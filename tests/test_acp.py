"""Unit tests for the ACP protocol and stdio client."""

import asyncio
from pathlib import Path
import sys
import pytest

from acp.client import AcpClient
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

MOCK_SERVER_PATH = Path(__file__).parent / "mock_server.py"


@pytest.fixture
def mock_command() -> list[str]:
    return [sys.executable, str(MOCK_SERVER_PATH)]


def test_protocol_models():
    """Verify serialization and validation of ACP protocol models."""
    # Test Request
    req = JsonRpcRequest(id=1, method="test/method", params={"key": "value"})
    req_dict = req.model_dump()
    assert req_dict["jsonrpc"] == "2.0"
    assert req_dict["id"] == 1
    assert req_dict["method"] == "test/method"
    assert req_dict["params"] == {"key": "value"}

    # Test Response with result
    resp = JsonRpcResponse(id=1, result={"status": "ok"})
    assert resp.result == {"status": "ok"}
    assert resp.error is None

    # Test Response with error
    err_resp = JsonRpcResponse(
        id=2,
        error={"code": -32600, "message": "Invalid Request", "data": "detail"},
    )
    assert err_resp.error is not None
    assert err_resp.error.code == -32600
    assert err_resp.error.message == "Invalid Request"
    assert err_resp.error.data == "detail"

    # Test Notification
    notif = JsonRpcNotification(method="session/update", params={"status": "done"})
    assert notif.method == "session/update"
    assert notif.params["status"] == "done"

    # Test ACP specific params
    init_params = InitializeParams(
        protocolVersion="1.0",
        clientInfo=ClientInfo(name="test-client", version="1.0"),
    )
    assert init_params.protocolVersion == "1.0"
    assert init_params.clientInfo.name == "test-client"

    new_params = SessionNewParams(cwd="/path/to/dir", customKey="customVal")
    assert new_params.cwd == "/path/to/dir"
    assert getattr(new_params, "customKey") == "customVal"

    prompt_params = SessionPromptParams(sessionId="s1", prompt="do work")
    assert prompt_params.sessionId == "s1"
    assert prompt_params.prompt == "do work"

    cancel_params = SessionCancelParams(sessionId="s1")
    assert cancel_params.sessionId == "s1"


@pytest.mark.asyncio
async def test_acp_initialize(mock_command: list[str]):
    """Test client initialize handshake."""
    async with AcpClient(mock_command) as client:
        result = await client.initialize(protocol_version="1.0")
        assert isinstance(result, dict)
        assert result.get("protocolVersion") == "1.0"
        assert result.get("serverInfo", {}).get("name") == "mock-acp-server"


@pytest.mark.asyncio
async def test_acp_session_new(mock_command: list[str]):
    """Test creating a new session via session/new."""
    async with AcpClient(mock_command) as client:
        session_id = await client.session_new(cwd="/tmp/workspace")
        assert session_id == "mock-session-xyz"


@pytest.mark.asyncio
async def test_acp_session_prompt_and_notifications(mock_command: list[str]):
    """Test session/prompt execution and notification handling."""
    received_notifications = []

    async with AcpClient(mock_command) as client:
        # Register notification handler
        client.on_notification("session/update", lambda notif: received_notifications.append(notif))

        prompt_res = await client.session_prompt(
            session_id="mock-session-xyz",
            prompt="Build feature X",
        )

        assert prompt_res.get("status") == "completed"
        assert "Executed: Build feature X" in prompt_res.get("output", "")

        # Notifications should have been dispatched
        assert len(received_notifications) >= 2
        deltas = [n.params.get("delta") for n in received_notifications]
        assert "Analyzing prompt..." in deltas
        assert "Execution finished." in deltas

        # Also verify client.notifications list
        assert len(client.notifications) >= 2


@pytest.mark.asyncio
async def test_acp_wait_for_notification(mock_command: list[str]):
    """Test wait_for_notification helper."""
    async with AcpClient(mock_command) as client:
        # Start prompt and wait for notification concurrently
        wait_task = asyncio.create_task(
            client.wait_for_notification(method="session/update", timeout=5.0)
        )
        prompt_task = asyncio.create_task(
            client.session_prompt(session_id="mock-session-xyz", prompt="Hello")
        )

        notif, prompt_res = await asyncio.gather(wait_task, prompt_task)
        assert notif.method == "session/update"
        assert notif.params.get("sessionId") == "mock-session-xyz"
        assert prompt_res.get("status") == "completed"


@pytest.mark.asyncio
async def test_acp_session_cancel(mock_command: list[str]):
    """Test session/cancel execution."""
    async with AcpClient(mock_command) as client:
        cancel_res = await client.session_cancel(session_id="mock-session-xyz")
        assert cancel_res.get("cancelled") is True
        assert cancel_res.get("sessionId") == "mock-session-xyz"


@pytest.mark.asyncio
async def test_acp_rpc_error_handling(mock_command: list[str]):
    """Test handling of JSON-RPC errors returned by server."""
    async with AcpClient(mock_command) as client:
        with pytest.raises(AcpRpcError) as exc_info:
            await client.call("error_trigger")

        assert exc_info.value.code == -32600
        assert "Invalid request triggered" in exc_info.value.message
        assert exc_info.value.data == {"detail": "test"}


@pytest.mark.asyncio
async def test_acp_timeout_handling(mock_command: list[str]):
    """Test request timeout raises AcpTimeoutError."""
    async with AcpClient(mock_command) as client:
        with pytest.raises(AcpTimeoutError):
            await client.call("sleep_long", timeout=0.2)


@pytest.mark.asyncio
async def test_acp_wait_for_notification_timeout(mock_command: list[str]):
    """Test wait_for_notification timeout raises AcpTimeoutError."""
    async with AcpClient(mock_command) as client:
        with pytest.raises(AcpTimeoutError):
            await client.wait_for_notification(method="nonexistent/update", timeout=0.2)


@pytest.mark.asyncio
async def test_acp_process_crash_handling(mock_command: list[str]):
    """Test handling when subprocess exits unexpectedly."""
    async with AcpClient(mock_command) as client:
        with pytest.raises(AcpProcessError):
            await client.call("crash")


@pytest.mark.asyncio
async def test_acp_client_stopped_state():
    """Test send_request on non-running client raises AcpProcessError."""
    client = AcpClient(["echo", "hello"])
    with pytest.raises(AcpProcessError):
        await client.send_request("test")
