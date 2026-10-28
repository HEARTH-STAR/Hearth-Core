"""Which Cursor CLI binary Hearth-Core runs.

Cursor's installer (``curl https://cursor.com/install | bash``) puts the CLI in
``~/.local/bin`` as ``cursor-agent`` and, in newer releases, also as ``agent``.
``agent`` is too generic a name to trust on PATH, so it is only taken from the
installer's own folder. Chat turns, jobs, login, logout and status all reach the
same binary, so a login always lands in the CLI the turns run.

    CURSOR_CLI_PATH → ``cursor-agent`` on PATH → ~/.local/bin/cursor-agent → ~/.local/bin/agent → ``cursor-agent``
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CURSOR_BIN_ENV = "CURSOR_CLI_PATH"
DEFAULT_BIN = "cursor-agent"


def cursor_binary() -> str:
    configured = (os.getenv(CURSOR_BIN_ENV) or "").strip()
    if configured:
        return configured
    found = shutil.which(DEFAULT_BIN)
    if found:
        return found
    local_bin = Path.home() / ".local" / "bin"
    for name in (DEFAULT_BIN, "agent"):
        candidate = local_bin / name
        if candidate.is_file():
            return str(candidate)
    return DEFAULT_BIN
