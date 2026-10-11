from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from services.hearth_agent.engine import stream_events as se


def parse_message(msg: Any) -> list[dict]:
    """
    Turn one Claude Agent SDK message into normalised events (see ``engine.stream_events``).

    The counterpart of xo-space's ``parse_stream_line``: xo-space decodes the CLI's
    ``stream-json`` lines itself, while HEARTH receives the same messages already
    typed by the SDK. Thinking text never leaves here (it becomes a label); tool
    inputs do, as audit events, because every agent action must be on the record.
    """
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init":
            sid = (msg.data or {}).get("session_id")
            return [se.session_id(sid)] if sid else []
        return []

    if isinstance(msg, AssistantMessage):
        events: list[dict] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                if block.text:
                    events.append(se.token(block.text))
            elif isinstance(block, ThinkingBlock):
                events.append(se.activity(se.THINKING))
            elif isinstance(block, ToolUseBlock):
                events.append(se.running(block.name))
                events.append(se.tool_call(block.name, block.id, dict(block.input or {})))
        if msg.error:
            events.append(se.error(str(msg.error)))
        return events

    if isinstance(msg, ResultMessage):
        payload = se.result(
            result=msg.result or "",
            session_id=msg.session_id,
            usage=msg.usage or {},
            total_cost_usd=msg.total_cost_usd,
            num_turns=msg.num_turns,
            stop_reason=msg.stop_reason,
        )
        if msg.is_error:
            return [se.error(msg.result or f"Claude Code error ({msg.subtype})"), payload]
        return [payload]

    # UserMessage (tool results going back to the agent; captured by the PostToolUse
    # hook instead), rate-limit and stream events: not part of the record here.
    return []


def parse_partial(msg: Any) -> list[dict]:
    """Live deltas from a ``StreamEvent`` (``include_partial_messages``, chat turns only).

    Text arrives as partial tokens and the start of a thinking or tool block as a partial label;
    the complete message still follows through :func:`parse_message`, whose full token is the
    authoritative text. Same shape as :func:`parse_stream_line`'s deltas. Subagent events are skipped.
    """
    if not isinstance(msg, StreamEvent) or msg.parent_tool_use_id:
        return []
    event = msg.event or {}
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and delta.get("text"):
            return [se.token(delta["text"], partial=True)]
        return []
    if event.get("type") == "content_block_start":
        act = _block_activity(event.get("content_block") or {}, partial=True)
        return [act] if act else []
    return []


def _block_activity(block: dict, *, partial: bool) -> dict | None:
    """The activity a non-text content block stands for, as a label only."""
    btype = block.get("type")
    if btype == "thinking":
        return se.activity(se.THINKING, partial=partial)
    if btype == "tool_use":
        return se.running(block.get("name") or "", partial=partial)
    return None


def parse_stream_line(raw: bytes) -> dict | None:
    """Decode one raw line of ``claude --output-format stream-json`` output into a normalised event.

    xo-space's parser, kept for reading the CLI's raw output (e.g. a transcript or a
    CLI run outside the SDK). HEARTH's adapter itself uses :func:`parse_message`.
    """
    try:
        line = raw.decode("utf-8").strip()
    except (UnicodeDecodeError, AttributeError):
        return None
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    etype = event.get("type", "")

    if etype == "system" and event.get("subtype") == "init":
        sid = event.get("session_id") or event.get("sessionId")
        return se.session_id(sid) if sid else None
    if etype == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return se.token(delta["text"], partial=True)
            return None
        if inner.get("type") == "content_block_start":
            return _block_activity(inner.get("content_block") or {}, partial=True)
        return None
    if etype == "assistant":
        blocks = (event.get("message") or {}).get("content", [])
        parts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")]
        if parts:
            return se.token("".join(parts))
        for block in blocks:
            act = _block_activity(block, partial=False)
            if act is not None:
                return act
        return None
    if etype == "result":
        if event.get("is_error"):
            return se.error(event.get("result", "Claude Code error"))
        return se.result(result=event.get("result", ""), session_id=event.get("session_id"),
                         usage=event.get("usage") or {}, model=event.get("model", ""))
    if etype == "error":
        return se.error(event.get("error", event.get("message", "unknown error")))
    return None


def trim_output(value: Any, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} chars trimmed]"
