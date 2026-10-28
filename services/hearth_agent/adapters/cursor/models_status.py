"""
Cursor models-status view.

Runs ``cursor-agent status`` and maps it into the common envelope ``{default, models:[{id, status}]}``.
The command prints text, not JSON (``✓ Logged in as you@example.com`` / ``Not logged in``), so
:func:`parse_status` reads it: "logged in" without "not" → ``"ok"``, anything else → ``"error"``. A
non-zero exit is a state here, not a CLI failure; only a missing binary (503) or a hang (504) raise.

The probe runs with ``Adapter.cli_env()``, so ``CURSOR_API_KEY`` counts exactly when turns would use it.
"""

from __future__ import annotations

import re
from typing import Any

from services.hearth_agent.adapters.cli_status import CliStatusError as CursorStatusError
from services.hearth_agent.adapters.cli_status import run_cli
from services.hearth_agent.adapters.cursor.cli import cursor_binary
from services.hearth_agent.registry.agent_registry import get_agent

DEFAULT_TIMEOUT_SECONDS = 15.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_EMAIL_RE = re.compile(r"logged in as\s+(\S+@\S+)", re.IGNORECASE)
_LOGGED_OUT_RE = re.compile(r"\bnot\s+(?:logged|signed)\s+in\b|\bunauthenticated\b", re.IGNORECASE)
_LOGGED_IN_RE = re.compile(r"\b(?:logged|signed)\s+in\b|\bauthenticated\b", re.IGNORECASE)
_API_KEY_RE = re.compile(r"api[\s_-]?key", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x07", "")


def parse_status(output: str, returncode: int) -> dict[str, Any]:
    """``cursor-agent status`` → ``{logged_in, email, method}``. Never includes a credential."""
    text = strip_ansi(output)
    logged_in = returncode == 0 and not _LOGGED_OUT_RE.search(text) and bool(_LOGGED_IN_RE.search(text))
    email = _EMAIL_RE.search(text) if logged_in else None
    method = None
    if logged_in:
        method = "api_key" if _API_KEY_RE.search(text) and not email else "login"
    return {"logged_in": logged_in, "email": email.group(1).rstrip(".,") if email else None, "method": method}


def _model_id() -> str:
    agent = get_agent("cursor")
    return f"{agent.model_prefix}/{agent.model_default or 'auto'}"


def build_status_view(status: dict[str, Any]) -> dict[str, Any]:
    model_id = _model_id()
    return {"default": model_id, "models": [{"id": model_id, "status": "ok" if status.get("logged_in") else "error"}]}


async def fetch_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run ``cursor-agent status`` with the turns' env and parse it."""
    from services.hearth_agent.adapters.cursor.adapter import CursorAdapter

    res = await run_cli(cursor_binary(), ("status",), timeout=timeout, label="cursor-agent",
                        env=CursorAdapter({}).cli_env())
    return parse_status(f"{res.stdout}\n{res.stderr}", res.returncode)


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    status = await fetch_status() if timeout is None else await fetch_status(timeout=timeout)
    return build_status_view(status)


__all__ = ["CursorStatusError", "build_status_view", "fetch_status", "get_models_status", "parse_status", "strip_ansi"]
