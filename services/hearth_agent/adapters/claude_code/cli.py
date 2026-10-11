"""Which ``claude`` binary Hearth-Core runs.

Chat turns go through the Claude Agent SDK, which runs the CLI it ships
(``claude_agent_sdk/_bundled``) before any ``claude`` on PATH. Login, logout and
status must reach the same binary, or a login could land in a CLI the turns never
use; a space image has no other ``claude`` at all.

    CLAUDE_CLI_PATH → the SDK's bundled CLI → ``claude`` on PATH → ``claude``
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CLAUDE_BIN_ENV = "CLAUDE_CLI_PATH"


def bundled_cli() -> Path | None:
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    name = "claude.exe" if sys.platform == "win32" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return path if path.is_file() else None


def claude_binary() -> str:
    configured = (os.getenv(CLAUDE_BIN_ENV) or "").strip()
    if configured:
        return configured
    bundled = bundled_cli()
    if bundled is not None:
        return str(bundled)
    return shutil.which("claude") or "claude"
