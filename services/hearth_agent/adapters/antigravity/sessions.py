"""
Antigravity sessions capability.

A session is one HEARTH job's agent run or one web-app conversation; its row lives in the session index
(``engine/sessions_io``, ``backend: "antigravity"``) and its transcript at
``~/.gemini/antigravity-cli/brain/<conversation-uuid>/.system_generated/logs/transcript_full.jsonl``.

Same contract as ``claude_code/sessions.py``; shape follows xo-space ``adapters/antigravity/sessions.py``.
"""

from __future__ import annotations

from pathlib import Path

from services.hearth_agent.adapters.antigravity import transcript as _t
from services.hearth_agent.adapters.antigravity.paths import transcript_path, valid_conversation_id
from services.hearth_agent.engine import sessions_io as _session_index

USES_PROJECT_SESSIONS = True

_BACKEND = "antigravity"
_TITLE_CHARS = 80


def _row(session_id: str) -> dict | None:
    row = _session_index.read_session_row(session_id)
    return row if row and row.get("backend") == _BACKEND else None


def resolve_native_file(meta: dict, session_id: str) -> Path | None:
    native = (meta or {}).get("nativeSessionId") or ""
    if not valid_conversation_id(native):
        return None
    path = transcript_path(native)
    return path if path.is_file() else None


def _strip_rules(text: str) -> str:
    """The owner's or HEARTH's words, without the rules and context every HEARTH prompt starts with."""
    from services.hearth_agent.adapters.antigravity.session_prompts import clean_prompt

    return clean_prompt(text) or ""


def enrich_project_session(meta: dict, key: str, default_agent: str):
    """``(time_created, title, effective_agent)`` from the transcript's first prompt."""
    native = (meta or {}).get("nativeSessionId") or ""
    title = None
    for turn in _t.iter_turns(_t.read_steps(native)) if native else []:
        if turn["role"] == "user":
            text = " ".join(_strip_rules(turn["text"]).split())
            if text:
                title = text[:_TITLE_CHARS]
                break
    return meta.get("createdAt"), title, default_agent


def list_native_sessions() -> list[dict]:
    """antigravity has no session store HEARTH lists outside the index."""
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
    """The session's turns from its transcript (empty if it is not ours or has none)."""
    row = _row(session_id)
    native = (row or {}).get("nativeSessionId") or ""
    if not native:
        return []
    out = []
    for n, turn in enumerate(_t.iter_turns(_t.read_steps(native))):
        text = _strip_rules(turn["text"]) if turn["role"] == "user" else turn["text"]
        content = [{"type": "text", "text": text}] if text else []
        content += [{"type": "tool_use", "name": name} for name in turn.get("tool_names") or []]
        out.append({"id": f"{native}:{n}", "role": turn["role"], "session_id": native, "content": content,
                    "model": None, "parent_tool_use_id": None, "timestamp": turn.get("ts_ms")})
    return out


def set_session_directory(session_id: str, directory: str) -> dict | None:
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
