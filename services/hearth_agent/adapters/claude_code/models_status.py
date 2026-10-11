"""
Claude Code models-status view.

Runs `claude auth status --json` and maps the result into the common envelope
`{default, models:[{id, status}]}` — strictly the common schema, no extras (no
email, org or subscription tier).

`claude auth status --json` returns one of three shapes today:

OAuth (claude.ai login):
    {"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty",
     "email": "…", "orgId": "…", "orgName": "…", "subscriptionType": "max"}

API key (ANTHROPIC_API_KEY env / similar):
    {"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty",
     "apiKeySource": "ANTHROPIC_API_KEY", "email": null, ...}

Logged out:
    {"loggedIn": false, "authMethod": "none", "apiProvider": "firstParty"}

The single derivation rule: `loggedIn` is the source of truth — true → "ok",
anything else → "error".

The probe runs with ``Adapter.cli_env()`` (the env HEARTH jobs actually run with)
so a credential the job would never use cannot read as connected.

Shape mirrors xo-space ``adapters/claude_code/models_status.py``; the model id is
the manifest's default model instead of a fixed ``claude_code/claude``.
"""

from __future__ import annotations

import json
from typing import Any

from services.hearth_agent.adapters.claude_code.cli import claude_binary
from services.hearth_agent.adapters.cli_status import (
    CliStatusError as ClaudeCodeStatusError,
)
from services.hearth_agent.adapters.cli_status import run_cli
from services.hearth_agent.registry.agent_registry import get_agent

DEFAULT_TIMEOUT_SECONDS = 15.0


def _model_id() -> str:
    """``<prefix>/<model>`` for the model HEARTH jobs run on, e.g. ``claude_code/claude-opus-5-5``."""
    agent = get_agent("claude_code")
    return f"{agent.model_prefix}/{agent.model_default or 'claude'}"


def build_status_view(auth_payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a parsed `claude auth status --json` dict into `{default, models}`."""
    model_id = _model_id()
    status = "ok" if bool(auth_payload.get("loggedIn")) else "error"
    return {"default": model_id, "models": [{"id": model_id, "status": status}]}


async def fetch_raw_status(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run `claude auth status --json` (with the jobs' env) and return the parsed JSON dict."""
    from services.hearth_agent.adapters.claude_code.adapter import ClaudeCodeAdapter

    result = await run_cli(claude_binary(), ("auth", "status", "--json"), timeout=timeout, label="claude",
                           env=ClaudeCodeAdapter({}).cli_env())
    out = result.stdout
    if result.returncode != 0:
        raise ClaudeCodeStatusError(
            f"claude exited with code {result.returncode}",
            code="execution_failed",
            detail=result.stderr or out[:300] or None,
        )
    try:
        parsed = json.loads(out) if out else None
    except json.JSONDecodeError as exc:
        raise ClaudeCodeStatusError("claude auth status returned invalid JSON", code="invalid_output",
                                    detail=str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ClaudeCodeStatusError("claude auth status returned empty or non-object output", code="invalid_output")
    return parsed


async def get_models_status(timeout: float | None = None) -> dict[str, Any]:
    """Fetch claude auth status and project it into the common envelope."""
    payload = await fetch_raw_status() if timeout is None else await fetch_raw_status(timeout=timeout)
    return build_status_view(payload)


__all__ = ["ClaudeCodeStatusError", "build_status_view", "get_models_status"]
