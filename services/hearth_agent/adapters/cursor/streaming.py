"""
``cursor-agent -p --output-format stream-json`` → HEARTH's event vocabulary (``engine/stream_events``).

The counterpart of ``claude_code/streaming.py``. Wire format (cursor.com/docs/cli/reference/output-format),
one JSON object per line:

    {"type":"system","subtype":"init","session_id":…,"model":…,"cwd":…,"apiKeySource":…,"permissionMode":…}
    {"type":"user","message":{"role":"user","content":[{"type":"text","text":…}]},"session_id":…}
    {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":…}]},"session_id":…}
    {"type":"tool_call","subtype":"started"|"completed","call_id":…,"tool_call":{"<kind>ToolCall":{"args":…,"result":…}}}
    {"type":"result","subtype":"success","is_error":false,"result":…,"duration_ms":…,"session_id":…}

With ``--stream-partial-output`` (chat turns) an ``assistant`` event carrying ``timestamp_ms`` and no
``model_call_id`` is a text delta; the others are flushes that repeat text already sent. Deltas go out as
``partial`` tokens, and the text gathered since the last block becomes one full token — the authoritative
text — when a tool call starts, a flush arrives, or the turn ends. Thinking is suppressed in print mode.
There is no token usage on the wire.

Tool calls carry their full input as audit events (every agent action must be on the record), and each one
is also queued as a :class:`ToolCall` for the adapter's policy check (``hooks.check_tool``). Calls of the web
app's own tools (MCP server ``client``) are left to ``mcp_tools.ClientToolRelay``, which reports them itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from services.hearth_agent.adapters.cursor.hooks import CLIENT_SERVER_NAME, Action, classify
from services.hearth_agent.engine import stream_events as se


@dataclass(frozen=True)
class ToolCall:
    call_id: str | None
    phase: str  # "started" | "completed"
    action: Action
    succeeded: bool | None = None  # completed only: True when Cursor reports a ``success`` result


def trim_output(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} chars trimmed]"


def _text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


class CursorStream:
    """Stateful parser for one run's stdout. ``feed`` each line; ``finish`` at the end."""

    def __init__(self, *, partial: bool = False, client_tools: frozenset[str] = frozenset()):
        self.partial = partial
        self.client_tools = client_tools
        self.result: dict[str, Any] | None = None
        self._pending: list[str] = []
        self._saw_delta = False
        self._calls: list[ToolCall] = []

    def pop_calls(self) -> list[ToolCall]:
        """Tool calls seen since the last call, for the policy check."""
        calls, self._calls = self._calls, []
        return calls

    def flush(self) -> list[dict]:
        """The text streamed since the last full token, as one full token."""
        if not self._pending:
            return []
        text, self._pending = "".join(self._pending), []
        return [se.token(text)] if text else []

    def finish(self) -> list[dict]:
        return self.flush()

    def feed(self, raw: bytes) -> list[dict]:
        try:
            event = json.loads(raw.decode("utf-8", errors="replace").strip() or "null")
        except json.JSONDecodeError:
            return []  # the CLI's own progress lines, if any, are not part of the record
        if not isinstance(event, dict):
            return []
        etype = event.get("type")

        if etype == "system":
            sid = event.get("session_id")
            return [se.session_id(sid)] if event.get("subtype") == "init" and isinstance(sid, str) and sid else []

        if etype == "assistant":
            text = _text(event.get("message"))
            if not self.partial:
                return [se.token(text)] if text else []
            if "timestamp_ms" in event and "model_call_id" not in event:
                self._saw_delta = True
                self._pending.append(text)
                return [se.token(text, partial=True)] if text else []
            if self._saw_delta:
                return self.flush()  # a flush repeats the deltas already gathered
            return [se.token(text)] if text else []

        if etype == "tool_call":
            return [*self.flush(), *self._tool_call(event)]

        if etype == "result":
            self.result = event
            payload = se.result(
                result=event.get("result") or "",
                session_id=event.get("session_id"),
                duration_ms=event.get("duration_ms"),
                duration_api_ms=event.get("duration_api_ms"),
                request_id=event.get("request_id"),
            )
            events = self.flush()
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                events.append(se.error(str(event.get("result") or f"Cursor error ({event.get('subtype')})")))
            return [*events, payload]

        if etype == "error":
            message = event.get("message") or event.get("error") or "Cursor error"
            return [*self.flush(), se.error(str(message))]

        return []  # user echoes and anything newer: not part of the record

    def _tool_call(self, event: dict[str, Any]) -> list[dict]:
        call_id = event.get("call_id") if isinstance(event.get("call_id"), str) else None
        tool_call = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
        action = classify(tool_call, self.client_tools)
        client = action.kind == "mcp" and action.server == CLIENT_SERVER_NAME and action.tool in self.client_tools
        subtype = event.get("subtype")
        if subtype == "started":
            self._calls.append(ToolCall(call_id, "started", action))
            return [] if client else [se.running(action.name), se.tool_call(action.name, call_id, action.input)]
        if subtype == "completed":
            body = next(iter(tool_call.values()), {}) if tool_call else {}
            result = body.get("result") if isinstance(body, dict) else None
            succeeded = isinstance(result, dict) and "success" in result
            self._calls.append(ToolCall(call_id, "completed", action, succeeded))
            return [] if client else [se.tool_result(action.name, call_id, trim_output(result))]
        return []


def parse_stream_line(raw: bytes) -> list[dict]:
    """One line without partial-output state, for reading saved CLI output (tests, transcripts)."""
    return CursorStream().feed(raw)
