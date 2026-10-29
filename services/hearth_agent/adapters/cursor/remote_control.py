"""Cursor Remote Control — not supported in HEARTH.

claude_code's Remote Control lets claude.ai drive a session in the space (and stays off unless an
operator enables it). Cursor's CLI has no counterpart HEARTH could gate the same way, so this keeps the
``/api/remote-control/*`` contract (``routes.py``) and always answers "not running, not enabled".
"""

from __future__ import annotations

from typing import Any

SUPPORTED = False
REASON = "Cursor has no Remote Control that HEARTH supports."


def status() -> dict[str, Any]:
    return {"running": False, "enabled": False, "supported": SUPPORTED, "login_present": None,
            "session_url": None, "detail": REASON}


def start(name: str | None = None) -> dict[str, Any]:
    return {"ok": False, "running": False, "error": "disabled", "supported": SUPPORTED, "detail": REASON}


def stop() -> dict[str, Any]:
    return {"ok": True, "running": False}
