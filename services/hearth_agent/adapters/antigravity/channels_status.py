"""
Antigravity channels-status view.

antigravity has ``channels.enabled = false`` in capabilities.json (HEARTH sends notifications from the
backend, not through the agent), so this returns the empty envelope, like claude_code.
"""

from __future__ import annotations

from typing import Any

from services.hearth_agent.adapters.antigravity.models_status import AntigravityStatusError

_EMPTY_VIEW: dict[str, Any] = {"channels": []}


def build_status_view(_: Any = None) -> dict[str, Any]:
    return dict(_EMPTY_VIEW)


async def get_channels_status(timeout: float | None = None) -> dict[str, Any]:
    """No CLI call needed — antigravity has no channels."""
    return build_status_view()


__all__ = ["AntigravityStatusError", "build_status_view", "get_channels_status"]
