"""
Antigravity (agy) transcript and log parsing — shared by the adapter, sessions, prompts and usage.

agy has no JSON output: ``agy -p`` prints narrative text, and the record of a run is the event-sourced
``brain/<conversation-uuid>/.system_generated/logs/transcript_full.jsonl``. The conversation id is never
printed; it is read from the run's own ``--log-file`` (``conversation=<uuid>``). xo-space also falls back
to agy's caches and its newest conversation; HEARTH does not, because a failed run would then borrow
another run's conversation.

Step envelope (xo-space, verified against agy 1.1.2): ``step_index``, ``source`` ∈ {USER_EXPLICIT, MODEL,
SYSTEM}, ``type``, ``status`` ∈ {RUNNING, DONE}, ``created_at`` (RFC3339 UTC), optional ``content``,
``tool_calls``, ``error``. The final answer is the last ``PLANNER_RESPONSE`` with ``content`` and no
``tool_calls`` — what ``agy -p`` printed.

Shape mirrors xo-space ``adapters/antigravity/transcript.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.antigravity.paths import transcript_path, valid_conversation_id

_CONV_RE = re.compile(r"conversation=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def conversation_id_from_log(log_path: str | Path | None) -> str | None:
    """``conversation=<uuid>`` from an agy ``--log-file`` (first match), or None."""
    if not log_path:
        return None
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CONV_RE.search(text)
    return match.group(1) if match else None


def read_steps(conversation_id: str) -> list[dict]:
    """Every step of a conversation's transcript; ``[]`` when absent or unreadable (never raises)."""
    if not valid_conversation_id(conversation_id):
        return []
    path = transcript_path(conversation_id)
    steps: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a running conversation can end mid-line
                if isinstance(obj, dict):
                    steps.append(obj)
    except OSError:
        return []
    return steps


def strip_user_request(content: str) -> str:
    """The prompt inside a ``USER_INPUT`` step's ``<USER_REQUEST>`` wrapper (or the whole content)."""
    if not content:
        return ""
    match = _USER_REQUEST_RE.search(content)
    return (match.group(1) if match else content).strip()


def final_answer(conversation_id: str, steps: list[dict] | None = None) -> str | None:
    """The last tool-less ``PLANNER_RESPONSE`` with content, or None."""
    for step in reversed(read_steps(conversation_id) if steps is None else steps):
        if step.get("type") != "PLANNER_RESPONSE" or step.get("tool_calls"):
            continue
        content = step.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def created_at_ms(step: dict) -> int | None:
    ts = step.get("created_at")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def tool_calls(step: dict) -> list[tuple[str, dict[str, Any]]]:
    """``(name, args)`` for each call in a ``PLANNER_RESPONSE``'s ``tool_calls``."""
    out = []
    for call in step.get("tool_calls") or []:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            continue
        args = call.get("args", call.get("arguments"))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"arguments": args}
        out.append((call["name"], args if isinstance(args, dict) else {}))
    return out


def iter_turns(steps: list[dict]) -> Iterator[dict[str, Any]]:
    """One normalized turn per meaningful step::

        {"role": "user", "text", "ts_ms"}
        {"role": "assistant", "text", "tool_names", "final", "ts_ms"}
    """
    for step in steps:
        stype = step.get("type")
        if stype == "USER_INPUT":
            text = strip_user_request(step.get("content") or "")
            if text:
                yield {"role": "user", "text": text, "ts_ms": created_at_ms(step)}
        elif stype == "PLANNER_RESPONSE":
            content = step.get("content").strip() if isinstance(step.get("content"), str) else ""
            names = [name for name, _ in tool_calls(step)]
            if content or names:
                yield {"role": "assistant", "text": content, "tool_names": names,
                       "final": bool(content) and not names, "ts_ms": created_at_ms(step)}


__all__ = [
    "conversation_id_from_log", "created_at_ms", "final_answer", "iter_turns", "read_steps",
    "strip_user_request", "tool_calls",
]
