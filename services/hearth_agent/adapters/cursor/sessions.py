"""
Cursor sessions capability.

Message reading and session-directory updates for cursor sessions. A session is one HEARTH job's agent
run or one web-app conversation; its row lives in the session index (``engine/sessions_io``, ``backend:
"cursor"``) and its readable transcript at ``~/.cursor/projects/*/agent-transcripts/<chatId>.jsonl``
(``paths.find_transcript``). Cursor's resumable state is its own SQLite chat store, which HEARTH does not
read.

Same contract as ``claude_code/sessions.py``, so the generic sessions route needs no cursor branch.
"""

from __future__ import annotations

from pathlib import Path

from services.hearth_agent.adapters.cursor.paths import content_text, find_transcript, iter_transcript
from services.hearth_agent.engine import sessions_io as _session_index

USES_PROJECT_SESSIONS = True

_BACKEND = "cursor"
_TITLE_CHARS = 80


def _row(session_id: str) -> dict | None:
    row = _session_index.read_session_row(session_id)
    return row if row and row.get("backend") == _BACKEND else None


def _message(row: dict) -> dict | None:
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else message
    role = row.get("role") or (message.get("role") if isinstance(message, dict) else None)
    if role not in ("user", "assistant"):
        return None
    return {"role": role, "content": content}


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    """The transcript for an indexed cursor session, or None."""
    return find_transcript((meta or {}).get("nativeSessionId") or "")


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """``(time_created, title, effective_agent)``: the row's creation time and the first prompt."""
    title = None
    path = resolve_native_file(meta, key)
    if path is not None:
        for row in iter_transcript(path):
            message = _message(row)
            if message and message["role"] == "user":
                text = " ".join(content_text(message["content"]).split())
                if text:
                    title = text[:_TITLE_CHARS]
                    break
    return meta.get("createdAt"), title, default_agent


def list_native_sessions() -> list[dict]:
    """cursor has no session store HEARTH lists outside the index."""
    return []


def owns_session(session_id: str) -> bool:
    return _row(session_id) is not None


def list_sessions() -> list[dict]:
    out: list[dict] = []
    for key, meta in _session_index.iter_session_rows():
        if meta.get("backend") != _BACKEND:
            continue
        time_created, title, _ = enrich_project_session(meta, key, _BACKEND)
        out.append({**meta, "key": key, "timeCreated": time_created, "title": title})
    return out


def get_messages(session_id: str) -> list:
    """The session's transcript messages (empty if it is not ours or Cursor kept no transcript)."""
    row = _row(session_id)
    path = resolve_native_file(row, session_id) if row else None
    if path is None:
        return []
    out = []
    for index, record in enumerate(iter_transcript(path)):
        message = _message(record)
        if message is None:
            continue
        out.append({"id": f"{row['nativeSessionId']}:{index}", "session_id": row["nativeSessionId"],
                    "model": None, "parent_tool_use_id": None, **message})
    return out


def set_session_directory(session_id: str, directory: str) -> dict | None:
    """Set the workspace directory for a cursor session; None if not ours."""
    row = _row(session_id)
    if row is None:
        return None
    now_ms = _session_index.now_ms()
    history = list(row.get("directoryHistory") or [])
    history.append({"directory": directory, "selectedAt": now_ms})
    row.update(directoryHistory=history[-200:], directory=directory, updatedAt=now_ms)
    if not _session_index.write_session_row(session_id, row):
        return None
    return {"ok": True, "session_id": session_id, "directory": directory}
