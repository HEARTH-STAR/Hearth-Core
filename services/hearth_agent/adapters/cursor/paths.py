"""
Cursor CLI on-disk state.

``cursor-agent`` keeps its state under one home folder::

    ROOT/                                        $CURSOR_HOME or the manifest's home_dir (~/.cursor)
    ├── cli-config.json                          global CLI config (permissions, model); HEARTH never edits it
    ├── mcp.json                                 user-level MCP servers
    ├── chats/<workspace-hash>/<chatId>/store.db one SQLite store per chat — what ``--resume`` reads
    └── projects/<sanitized-path>/agent-transcripts/<chatId>.jsonl   (or <chatId>/<chatId>.jsonl)
                                                 a readable transcript, one ``{role, message}`` per line

The chat id is the ``session_id`` of the stream's ``system/init`` event. This
module only *locates* files; ``sessions.py``, ``session_prompts.py`` and
``usage.py`` read them. Layout from xo-space's Cursor telemetry reader
(``adapters/cursor/session_telemetry.py``).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def cursor_home() -> Path:
    """``$CURSOR_HOME`` → the cursor manifest's ``home_dir`` → ``~/.cursor``."""
    configured = (os.getenv("CURSOR_HOME") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    try:
        from services.hearth_agent.registry.agent_registry import get_agent

        return get_agent("cursor").home_dir
    except Exception:
        return Path.home() / ".cursor"


def chats_root() -> Path:
    return cursor_home() / "chats"


def projects_root() -> Path:
    return cursor_home() / "projects"


def valid_chat_id(chat_id: str) -> bool:
    """Guards every path built from a chat id (it comes from the CLI's stream or the index)."""
    return bool(chat_id) and bool(_CHAT_ID_RE.fullmatch(chat_id))


def find_chat(chat_id: str) -> Path | None:
    """The chat's store folder (``chats/<hash>/<chatId>``), or None. Its presence is what ``--resume`` needs."""
    root = chats_root()
    if not valid_chat_id(chat_id) or not root.is_dir():
        return None
    for workspace_dir in root.iterdir():
        candidate = workspace_dir / chat_id
        if candidate.is_dir():
            return candidate
    return None


def find_transcript(chat_id: str) -> Path | None:
    """The chat's readable transcript, newest first when several match, or None."""
    root = projects_root()
    if not valid_chat_id(chat_id) or not root.is_dir():
        return None
    matches: list[Path] = []
    for project in root.iterdir():
        transcripts = project / "agent-transcripts"
        for candidate in (transcripts / f"{chat_id}.jsonl", transcripts / chat_id / f"{chat_id}.jsonl"):
            if candidate.is_file():
                matches.append(candidate)
    return max(matches, key=lambda p: p.stat().st_mtime_ns) if matches else None


def iter_transcript(path: Path) -> Iterator[dict[str, Any]]:
    """Each JSON object in a transcript; blank, partial or malformed lines are skipped."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # an active chat can end mid-line
                if isinstance(row, dict):
                    yield row
    except OSError:
        return


def content_text(content: Any) -> str:
    """The text parts of a transcript message's ``content`` (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def global_mcp_servers() -> list[str]:
    """Names of the user-level MCP servers in ``~/.cursor/mcp.json`` (HEARTH denies their tools)."""
    try:
        data = json.loads((cursor_home() / "mcp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return sorted(str(name) for name in servers) if isinstance(servers, dict) else []


__all__ = [
    "chats_root", "content_text", "cursor_home", "find_chat", "find_transcript", "global_mcp_servers",
    "iter_transcript", "projects_root", "valid_chat_id",
]
