"""Per-turn user prompts for one Antigravity session (job detail view).

Served lazily from the session's transcript: one entry per ``USER_INPUT`` step (agy wraps the prompt in
``<USER_REQUEST>``), owning the model's responses and tool calls until the next prompt. HEARTH's prompts
start with ``<hearth-rules>`` and ``<hearth-context>`` blocks (agy has no system prompt); both are stripped
so the reviewer sees the task, the fix-ups and the owner's messages.

``session_id`` may be a HEARTH job id or chat key, a conversation uuid, or ``antigravity:<uuid>``.
Same contract as ``claude_code/session_prompts.py``.
"""

from __future__ import annotations

import re

from services.hearth_agent.adapters.antigravity import transcript as _t
from services.hearth_agent.adapters.antigravity.paths import transcript_path, valid_conversation_id
from services.hearth_agent.engine import sessions_io as _session_index

SOURCE_ID = "antigravity"
SOURCE_LABEL = "Antigravity"

MAX_PROMPTS = 200
MAX_PROMPT_CHARS = 4000
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DROP_BLOCK_RE = re.compile(r"<(hearth-rules|hearth-context)>.*?</\1>", re.DOTALL)


def clean_prompt(raw: str) -> str | None:
    text = _DROP_BLOCK_RE.sub("", _t.strip_user_request(raw)).strip()
    return text or None


def _native_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session id: {session_id!r}")
    native = session_id.split(":", 1)[1] if session_id.startswith(f"{SOURCE_ID}:") else session_id
    row = _session_index.read_session_row(session_id)
    if row and row.get("backend") == SOURCE_ID and row.get("nativeSessionId"):
        native = row["nativeSessionId"]
    return native


def collect_session_prompts(session_id: str) -> dict:
    native = _native_session_id(session_id)
    if not valid_conversation_id(native) or not transcript_path(native).is_file():
        raise FileNotFoundError(f"No Antigravity transcript found for session {native!r}")
    prompts: list[dict] = []
    for step in _t.read_steps(native):
        stype = step.get("type")
        if stype == "PLANNER_RESPONSE" and prompts:
            calls = len(_t.tool_calls(step))
            prompts[-1]["tool_uses"] += calls
            prompts[-1]["responses"] += 0 if calls else 1
            continue
        if stype != "USER_INPUT":
            continue
        text = clean_prompt(step.get("content") or "")
        if text is None:
            continue
        prompts.append({
            "timestamp": step.get("created_at"),
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
