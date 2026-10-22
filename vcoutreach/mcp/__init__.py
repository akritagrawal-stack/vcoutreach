"""Local MCP (Model Context Protocol) server for Publikreach.

Importing this package is cheap and has no side effects: constants only.
The server itself lives in ``vcoutreach.mcp.server``.
"""
from __future__ import annotations

from vcoutreach import __version__

# Newest first. A client asking for anything else gets PROTOCOL_VERSIONS[0].
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "publikreach"
SERVER_VERSION = __version__

# One JSON-RPC request line may be at most this many bytes (payload, newline excluded).
MAX_LINE_BYTES = 1024 * 1024
# One serialized response line may be at most this many bytes.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# A single tool call may run at most this long.
TOOL_TIMEOUT_SECONDS = 120

__all__ = [
    "PROTOCOL_VERSIONS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "MAX_LINE_BYTES",
    "MAX_RESPONSE_BYTES",
    "TOOL_TIMEOUT_SECONDS",
]
