"""
A chat turn's caller-run tools (``ChatSpec.tools``), served to Cursor as the MCP server ``client``.

The counterpart of ``claude_code/mcp_tools.py``'s ``build_client_tools``. Claude Code calls those tools in
process; ``cursor-agent`` can only start an MCP server as a subprocess, so the call crosses two hops:

    cursor-agent ──stdio MCP──▶ mcp_server.py ──loopback socket──▶ ClientToolRelay (this process)

:class:`ClientToolRelay` listens on 127.0.0.1 for the length of one turn and writes the server's spec (the
tools, the port and a random token) into the run's private folder. Each call it receives is put on the
chat stream as a ``tool-call`` event with a fresh ``tool_use_id``; the web app runs the tool and posts the
result to ``POST /api/chat/{session}/tool-results``, where :class:`~services.hearth_agent.engine.chat_bridge.ToolResultBridge`
hands it back here. ``handoff`` tools are acknowledged at once, because the owner answers in the next turn.
Calls without the turn's token are refused.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.cursor.hooks import CLIENT_SERVER_NAME
from services.hearth_agent.adapters.cursor.streaming import trim_output
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import ChatTool

log = logging.getLogger(__name__)

Emit = Callable[[dict], Awaitable[None]]

SERVER_SCRIPT = Path(__file__).resolve().with_name("mcp_server.py")
_REQUEST_LIMIT = 4 * 1024 * 1024

HANDOFF_ACK = (
    "Shown to the owner in the Hearth web app. Stop here and end your turn without calling more tools; "
    "their decision arrives as the next message."
)


class ClientToolRelay:
    """Serves one turn's caller-run tools to Cursor. ``async with`` starts and stops the listener."""

    def __init__(self, tools: list[ChatTool], bridge: ToolResultBridge, emit: Emit, run_dir: Path):
        self.tools = {tool.name: tool for tool in tools}
        self.bridge = bridge
        self.emit = emit
        self.spec_path = run_dir / "client-tools.json"
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.Server | None = None

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.tools)

    def server_entry(self) -> dict[str, Any]:
        """The ``mcpServers.client`` entry for ``.cursor/mcp.json``: Hearth-Core's own Python runs the server."""
        return {"command": sys.executable, "args": [str(SERVER_SCRIPT), str(self.spec_path)]}

    async def __aenter__(self) -> ClientToolRelay:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=_REQUEST_LIMIT)
        port = self._server.sockets[0].getsockname()[1]
        spec = {
            "name": CLIENT_SERVER_NAME,
            "host": "127.0.0.1",
            "port": port,
            "token": self._token,
            "timeout": self.bridge.timeout + 30,
            "tools": [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                      for t in self.tools.values()],
        }
        self.spec_path.write_text(json.dumps(spec), encoding="utf-8")
        if os.name != "nt":
            self.spec_path.chmod(0o600)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Show one call to the web app and wait for its result (or acknowledge a handoff)."""
        spec = self.tools.get(name)
        if spec is None:
            return f"{name} is not one of this conversation's tools.", True
        tool = f"mcp__{CLIENT_SERVER_NAME}__{name}"
        tool_use_id = f"toolu_cursor_{uuid.uuid4().hex}"
        await self.emit(se.running(tool))
        await self.emit(se.tool_call(tool, tool_use_id, arguments))
        if spec.handoff:
            output, is_error = HANDOFF_ACK, False
        else:
            output, is_error = await self.bridge.wait(tool_use_id)
        await self.emit(se.tool_result(tool, tool_use_id, trim_output(output)))
        return output, is_error

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline() or b"{}")
            if not isinstance(request, dict) or not hmac.compare_digest(str(request.get("token", "")), self._token):
                return
            arguments = request.get("arguments")
            output, is_error = await self.call(str(request.get("name", "")),
                                               arguments if isinstance(arguments, dict) else {})
            writer.write(json.dumps({"output": output, "is_error": is_error}).encode() + b"\n")
            await writer.drain()
        except (ValueError, OSError, asyncio.IncompleteReadError) as exc:
            log.warning("client tool call failed: %s", exc)
        finally:
            writer.close()
