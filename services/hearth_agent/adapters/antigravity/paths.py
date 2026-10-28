"""
Antigravity (agy) on-disk paths.

All agy state lives under ``~/.gemini/antigravity-cli/`` (the manifest's ``home_dir``); its hooks live
beside it, in ``~/.gemini/config/``. Layout (from xo-space, verified there against agy 1.1.2)::

    ~/.gemini/antigravity-cli/
    ├── antigravity-oauth-token          consumer OAuth credential (agy writes and refreshes it)
    ├── settings.json                    CLI settings (toolPermission, trustedWorkspaces, …)
    ├── brain/<conversation-uuid>/.system_generated/logs/transcript_full.jsonl   the step stream
    ├── conversations/<uuid>.db          SQLite trajectory store — token counts (tokens.py)
    └── cache/last_conversations.json    {"<abs launch dir>": "<conversation uuid>"}
    ~/.gemini/config/hooks.json          user-level hooks (HEARTH's PreToolUse policy hook, hooks.py)

Functions, not constants, so the home is read when needed (and tests can move it).
Shape mirrors xo-space ``adapters/antigravity/paths.py``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def agy_home() -> Path:
    try:
        from services.hearth_agent.registry.agent_registry import get_agent

        return get_agent("antigravity").home_dir
    except Exception:
        return Path(os.path.expanduser("~/.gemini/antigravity-cli"))


def token_path() -> Path:
    return agy_home() / "antigravity-oauth-token"


def settings_path() -> Path:
    return agy_home() / "settings.json"


def brain_dir() -> Path:
    return agy_home() / "brain"


def last_conversations() -> Path:
    return agy_home() / "cache" / "last_conversations.json"


def hooks_file() -> Path:
    return agy_home().parent / "config" / "hooks.json"


def valid_conversation_id(conversation_id: str) -> bool:
    """Guards every path built from a conversation id (it comes from agy's log or the index)."""
    return bool(conversation_id) and bool(_UUID_RE.fullmatch(conversation_id))


def transcript_path(conversation_id: str) -> Path:
    return brain_dir() / conversation_id / ".system_generated" / "logs" / "transcript_full.jsonl"


def conversation_db(conversation_id: str) -> Path:
    return agy_home() / "conversations" / f"{conversation_id}.db"


__all__ = [
    "agy_home", "brain_dir", "conversation_db", "hooks_file", "last_conversations", "settings_path",
    "token_path", "transcript_path", "valid_conversation_id",
]
