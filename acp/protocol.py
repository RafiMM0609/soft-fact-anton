"""Agent Client Protocol (ACP) Models and Exceptions.

JSON-RPC 2.0 data structures and ACP message models for communication
between ACP clients and agent harnesses.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class JsonRpcError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any = None


class JsonRpcRequest(BaseModel):
    """JSON-RPC 2.0 request message."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JsonRpcResponse(BaseModel):
    """JSON-RPC 2.0 response message."""

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str | None = None
    result: Any = None
    error: JsonRpcError | None = None


class JsonRpcNotification(BaseModel):
    """JSON-RPC 2.0 notification message (no id field)."""

    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class ClientInfo(BaseModel):
    """Client identification info sent during initialize."""

    name: str = "acp-factory"
    version: str = "0.1.0"


class InitializeParams(BaseModel):
    """Parameters for ACP initialize request."""

    protocolVersion: int | str = 1
    clientInfo: ClientInfo = Field(default_factory=ClientInfo)


class SessionNewParams(BaseModel):
    """Parameters for session/new request."""

    cwd: str
    model_config = {"extra": "allow"}


class SessionPromptParams(BaseModel):
    """Parameters for session/prompt request."""

    sessionId: str
    prompt: Any


class SessionCancelParams(BaseModel):
    """Parameters for session/cancel request."""

    sessionId: str


class AcpError(Exception):
    """Base exception for all ACP errors."""


def is_quota_exhausted(error: BaseException | None) -> bool:
    """Recognize explicit quota exhaustion without treating all rate limits as permanent."""
    if not isinstance(error, AcpError):
        return False
    message = str(error).casefold()
    return any(marker in message for marker in (
        "free usage limit reached",
        "freeusagelimiterror",
        "gousagelimiterror",
        "insufficient_quota",
        "billing_hard_limit_reached",
    ))


class AcpRpcError(AcpError):
    """Raised when an ACP server returns a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"RPC Error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class AcpTimeoutError(AcpError):
    """Raised when an ACP request times out."""


class AcpProcessError(AcpError):
    """Raised when the ACP agent subprocess exits unexpectedly or encounters an error."""
