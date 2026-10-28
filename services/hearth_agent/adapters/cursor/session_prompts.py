"""Per-turn user prompts for one Cursor session (job detail view).

Served lazily, one session at a time, from the session's transcript
(``~/.cursor/projects/*/agent-transcripts/<chatId>.jsonl``). In HEARTH every prompt starts with the
``<hearth-rules>`` block (Cursor has no system prompt), and Cursor wraps what it was sent in tags such as
``<user_query>``; both are stripped so the reviewer sees the task, the fix-ups and the owner's messages.

``session_id`` may be a HEARTH job id or chat key (resolved through the session index), a Cursor chat id,
or ``cursor:<chat id>``. Same contract as ``claude_code/session_prompts.py``.
"""

from __future__ import annotations

import re

from services.hearth_agent.adapters.cursor.paths import content_text, find_transcript, iter_transcript
from services.hearth_agent.engine import sessions_io as _session_index

SOURCE_ID = "cursor"
SOURCE_LABEL = "Cursor"

MAX_PROMPTS = 200
MAX_PROMPT_CHARS = 4000
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DROP_BLOCK_RE = re.compile(r"<(hearth-rules|hearth-context|user_info|timestamp|system-reminder)>.*?</\1>", re.DOTALL)
_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)


def _native_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    native = session_id.split(":", 1)[1] if session_id.startswith(f"{SOURCE_ID}:") else session_id
    row = _session_index.read_session_row(session_id)
    if row and row.get("backend") == SOURCE_ID and row.get("nativeSessionId"):
        native = row["nativeSessionId"]
    return native


def clean_prompt(raw: str) -> str | None:
    """The part of a sent prompt a person wrote or HEARTH asked, or None."""
    query = _QUERY_RE.search(raw)
    text = query.group(1) if query else raw
    text = _DROP_BLOCK_RE.sub("", text).strip()
    return text or None


def collect_session_prompts(session_id: str) -> dict:
    """One entry per exchange: a turn starts at a prompt and owns every reply until the next one."""
    native_id = _native_session_id(session_id)
    transcript = find_transcript(native_id)
    if transcript is None:
        raise FileNotFoundError(f"No Cursor transcript found for session {native_id!r}")

    prompts: list[dict] = []
    for record in iter_transcript(transcript):
        message = record.get("message")
        role = record.get("role") or (message.get("role") if isinstance(message, dict) else None)
        content = message.get("content") if isinstance(message, dict) else message
        if role == "assistant":
            if prompts:
                prompts[-1]["responses"] += 1
                if isinstance(content, list):
                    prompts[-1]["tool_uses"] += sum(
                        1 for b in content if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_call")
                    )
            continue
        if role != "user":
            continue
        text = clean_prompt(content_text(content))
        if text is None:
            continue
        prompts.append({
            "timestamp": record.get("timestamp"),
            "text": text[:MAX_PROMPT_CHARS],
            "truncated": len(text) > MAX_PROMPT_CHARS,
            "responses": 0,
            "tool_uses": 0,
        })

    total = len(prompts)
    prompts = prompts[-MAX_PROMPTS:]
    for turn, prompt in enumerate(prompts, start=total - len(prompts) + 1):
        prompt["turn"] = turn
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL},
        "session_id": session_id,
        "supported": True,
        "total_prompts": total,
        "capped": total > len(prompts),
        "prompts": prompts,
    }
