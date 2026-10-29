"""
Antigravity models-status view.

agy has no ``auth status`` command, so the envelope ``{default, models:[{id, status}]}`` comes from its token
file (``auth.login_state``): usable → ``"ok"``, anything else → ``"error"``. No CLI call; a missing binary is
reported by ``/api/agents/health`` and ``/api/connectors/antigravity/status`` instead.

Shape mirrors xo-space ``adapters/antigravity/models_status.py``.
"""

from __future__ import annotations

from typing import Any

from services.hearth_agent.adapters.antigravity.auth import login_state
from services.hearth_agent.adapters.cli_status import CliStatusError as AntigravityStatusError
from services.hearth_agent.registry.agent_registry import get_agent


def _model_id() -> str:
    agent = get_agent("antigravity")
    return f"{agent.model_prefix}/{agent.model_default}"


def build_status_view(state: str) -> dict[str, Any]:
    model_id = _model_id()
    return {"default": model_id, "models": [{"id": model_id, "status": "ok" if state == "ok" else "error"}]}


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    return build_status_view(login_state())


__all__ = ["AntigravityStatusError", "build_status_view", "get_models_status"]
