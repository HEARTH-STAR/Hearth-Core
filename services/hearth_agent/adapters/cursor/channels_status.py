"""
Cursor channels-status view.

cursor has ``channels.enabled = false`` in capabilities.json (HEARTH sends Slack/email notifications from
the backend, not through the agent), so this returns the empty envelope and the ``/channels/status`` route
treats cursor like claude_code.
"""

from __future__ import annotations

from typing import Any

from services.hearth_agent.adapters.cursor.models_status import CursorStatusError

_EMPTY_VIEW: dict[str, Any] = {"channels": []}


def build_status_view(_: Any = None) -> dict[str, Any]:
    return dict(_EMPTY_VIEW)


async def get_channels_status(timeout: float | None = None) -> dict[str, Any]:
    """No CLI call needed — cursor has no channels."""
    return build_status_view()


__all__ = ["CursorStatusError", "build_status_view", "get_channels_status"]
