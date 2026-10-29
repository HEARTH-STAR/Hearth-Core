"""Which ``agy`` binary Hearth-Core runs.

Google's installer puts the Antigravity CLI in ``~/.local/bin``, which a service's PATH often lacks.
Turns, jobs and login all reach the same binary.

    AGY_CLI_PATH → ``agy`` on PATH → ~/.local/bin/agy → ``agy``
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

AGY_BIN_ENV = "AGY_CLI_PATH"
DEFAULT_BIN = "agy"


def agy_binary() -> str:
    configured = (os.getenv(AGY_BIN_ENV) or "").strip()
    if configured:
        return configured
    found = shutil.which(DEFAULT_BIN)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / DEFAULT_BIN
    return str(local) if local.is_file() else DEFAULT_BIN
