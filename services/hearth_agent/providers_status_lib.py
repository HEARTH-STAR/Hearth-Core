"""
Shared building blocks for ``/providers/status``.

Mirrors xo-space ``services/cowork_agent/providers_status_lib.py``:

1. ``_read_models_capabilities`` — the ``models`` section of the active agent's
   ``capabilities.json`` (HEARTH's counterpart of xo.json). The endpoint reports
   only on providers marked ``enabled: true``.
2. ``claude_auth_status`` — runs ``claude auth status --json`` once. Best-effort:
   any failure reads as ``{}`` (drives a frontend tile, not an alert; richer
   errors live on ``/models/status``).
3. ``build_providers_status`` — composes the response from per-provider callables.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from services.hearth_agent.adapters.claude_code.cli import claude_binary
from services.hearth_agent.registry.settings import load_agent_capabilities
from utils.commands import run

DEFAULT_TIMEOUT_SECONDS = 10.0


def _read_models_capabilities(agent: str) -> dict[str, Any]:
    models = load_agent_capabilities(agent).get("models")
    return models if isinstance(models, dict) else {}


def _leaf_enabled(node: Any, *keys: str) -> bool:
    """Walk ``node`` via ``keys``; True iff every level is enabled (parent ``enabled: false`` cascades)."""
    cur: Any = node
    if isinstance(cur, dict) and cur.get("enabled") is False:
        return False
    for k in keys:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(k)
        if isinstance(cur, dict) and cur.get("enabled") is False:
            return False
    return bool(isinstance(cur, dict) and cur.get("enabled"))


async def claude_auth_status(
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run ``claude auth status --json`` once and return the parsed payload, or ``{}`` on any failure.

    Pass the *same* env the agent subprocess uses (``Adapter.cli_env()``) so the
    status reflects the auth the CLI will actually resolve to.
    """
    res = await run([claude_binary(), "auth", "status", "--json"], env=env, timeout=timeout, separate_stderr=True)
    if not res.ok:
        return {}
    try:
        payload = json.loads(res.output or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def claude_oauth_connected(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    return bool((await claude_auth_status(timeout)).get("loggedIn"))


async def build_providers_status(
    agent: str,
    *,
    anthropic_key_present: Callable[[], bool],
    llm_gateway_present: Callable[[], bool],
    claude_oauth_present: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Compose the ``/providers/status`` response for ``agent``. Disabled providers are omitted."""
    models = _read_models_capabilities(agent)

    oauth: dict[str, dict] = {}
    if _leaf_enabled(models, "oauth", "claude_code"):
        connected = claude_oauth_present() if claude_oauth_present is not None else await claude_oauth_connected()
        oauth["claude_code"] = {"connected": bool(connected)}

    api_keys: dict[str, dict] = {}
    if _leaf_enabled(models, "api_keys", "anthropic"):
        api_keys["anthropic"] = {"connected": bool(anthropic_key_present())}
    if _leaf_enabled(models, "api_keys", "llm_gateway"):
        api_keys["llm_gateway"] = {"connected": bool(llm_gateway_present())}

    return {"agent": agent, "oauth": oauth, "api_keys": api_keys}


__all__ = ["build_providers_status", "claude_auth_status", "claude_oauth_connected"]
