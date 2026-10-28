"""Read-only session telemetry for the Cursor runtime — not supported in HEARTH.

xo-space reads Cursor's local stores (agent transcripts, chat ``store.db``, the desktop app's
``state.vscdb``) into its Space telemetry. HEARTH records per-session runs in the session index (served by
``usage.py``) and traces with OpenTelemetry inside the space (TDD §10), as for claude_code. The contract
stays so a telemetry route gets a clear "unsupported" answer instead of an import error.
"""

from __future__ import annotations

SOURCE_ID = "cursor"
SOURCE_LABEL = "Cursor"
META_PRIORITY = 20
COST_STATUS = "unavailable"
SUPPORTED = False
REASON = "HEARTH records per-session runs in the session index (see /api/usage); Cursor reports no tokens."

SOURCE_CONFIG = {
    "vendor": "cursor",
    "path_env": None,
    "path_default": None,
    "path_kind": None,
    "path_label": None,
    "collects": [],
    "never": "No prompt text stored",
}


def collect_session_telemetry() -> dict:
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL, "cost_status": COST_STATUS},
        "supported": SUPPORTED,
        "reason": REASON,
        "meta_priority": META_PRIORITY,
        "meta": {},
        "totals": {},
        "project_keys": [],
        "sessions": [],
        "daily_models": [],
        "daily_sessions": [],
        "daily_tools": [],
    }


def start_daemon() -> None:
    """No daemon to start in HEARTH."""


def stop_daemon() -> None:
    """No daemon to stop in HEARTH."""
