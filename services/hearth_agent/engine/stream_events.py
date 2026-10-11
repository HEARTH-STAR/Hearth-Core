"""The one vocabulary an adapter's ``stream()`` speaks.

Every adapter turns its agent's wire format into these events and nothing else.
The first five are xo-space's chat vocabulary unchanged (so a UI built for one
reads the other); the audit events are HEARTH's addition, because a security
reviewer must see every command the agent ran and every file it changed (PRD §3).

======================  ==================================================
event ``type``          meaning
======================  ==================================================
``token``               a piece of the agent's text. ``partial=True`` marks a live delta.
``model-loading``       the agent is working: thinking, running a tool. A short ``label``
                        only — safe for the live UI stream.
``session_id``          the agent's native session id, seen early; bookkeeping.
``result``              the agent's end-of-turn rollup (usage, cost, model); bookkeeping.
``error``               the agent failed.
``tool-call``           AUDIT: a tool call with its full input.
``tool-result``         AUDIT: a tool's (trimmed) output.
``policy-denied``       AUDIT: the tool-layer policy blocked a call, with the reason.
``validation``          a harness validation round (agent-requested or finish gate).
======================  ==================================================

The stream ends with exactly one ``{"done": True, "native_session_id", "result"}``.
Audit events go to the job's ``events.jsonl``; a live UI stream may forward only
``token`` / ``model-loading`` / ``error`` / ``validation``. The chat stream
(``POST /api/chat``) is the exception: it goes to the space owner's own web app,
which shows every tool call and its output, so it carries the audit events too.
Chat text also streams as it is written: ``partial`` tokens, then one full token
per text block that is the authoritative text.
"""

from __future__ import annotations

from typing import Any

TOKEN = "token"
ACTIVITY = "model-loading"
SESSION_ID = "session_id"
RESULT = "result"
ERROR = "error"
TOOL_CALL = "tool-call"
TOOL_RESULT = "tool-result"
POLICY_DENIED = "policy-denied"
VALIDATION = "validation"

EVENT_TYPES = frozenset({TOKEN, ACTIVITY, SESSION_ID, RESULT, ERROR, TOOL_CALL, TOOL_RESULT, POLICY_DENIED, VALIDATION})
AUDIT_TYPES = frozenset({TOOL_CALL, TOOL_RESULT, POLICY_DENIED})
LIVE_TYPES = frozenset({TOKEN, ACTIVITY, ERROR, VALIDATION})

# Activity labels: a fixed label, or ``running <tool>``. A label, never input.
THINKING = "thinking"
RUNNING_COMMAND = "running command"
EDITING_FILES = "editing files"
CALLING_TOOL = "calling tool"
VALIDATING = "validating"

FIXED_LABELS = frozenset({THINKING, RUNNING_COMMAND, EDITING_FILES, CALLING_TOOL, VALIDATING})
_RUNNING_PREFIX = "running "
_MCP_PREFIX = "mcp__"


def display_tool_name(name: str) -> str:
    """``mcp__hearth__run_validation`` → ``run_validation``: the server prefix is routing, not the tool."""
    if name.startswith(_MCP_PREFIX):
        return name.split("__", 2)[-1]
    return name


def is_known_label(label: object) -> bool:
    if not isinstance(label, str) or not label:
        return False
    return label in FIXED_LABELS or (label.startswith(_RUNNING_PREFIX) and len(label) > len(_RUNNING_PREFIX))


def token(text: str, *, partial: bool = False) -> dict:
    event: dict[str, Any] = {"type": TOKEN, "token": text}
    if partial:
        event["partial"] = True
    return event


def activity(label: str, *, partial: bool = False) -> dict:
    event: dict[str, Any] = {"type": ACTIVITY, "label": label}
    if partial:
        event["partial"] = True
    return event


def running(tool_name: str, *, partial: bool = False) -> dict:
    name = display_tool_name(tool_name) if isinstance(tool_name, str) else ""
    if name == "Bash":
        return activity(RUNNING_COMMAND, partial=partial)
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        return activity(EDITING_FILES, partial=partial)
    if name in {"run_validation", "finish"}:
        return activity(VALIDATING, partial=partial)
    return activity(_RUNNING_PREFIX + name if name else CALLING_TOOL, partial=partial)


def session_id(native_id: str) -> dict:
    return {"type": SESSION_ID, "session_id": native_id}


def result(**fields: Any) -> dict:
    return {"type": RESULT, **fields}


def error(message: str) -> dict:
    return {"type": ERROR, "error": message}


def tool_call(tool: str, tool_use_id: str | None, input: dict) -> dict:
    return {"type": TOOL_CALL, "tool": tool, "tool_use_id": tool_use_id, "input": input}


def tool_result(tool: str | None, tool_use_id: str | None, output: str) -> dict:
    return {"type": TOOL_RESULT, "tool": tool, "tool_use_id": tool_use_id, "output": output}


def policy_denied(tool: str, tool_use_id: str | None, input: dict, reason: str) -> dict:
    return {"type": POLICY_DENIED, "tool": tool, "tool_use_id": tool_use_id, "input": input, "reason": reason}


def validation(*, phase: str, passed: bool, regressions: list[str], round: int | None = None) -> dict:
    event: dict[str, Any] = {"type": VALIDATION, "phase": phase, "passed": passed, "regressions": regressions}
    if round is not None:
        event["round"] = round
    return event


def done(native_session_id: str | None, result_payload: dict) -> dict:
    return {"done": True, "native_session_id": native_session_id, "result": result_payload}
