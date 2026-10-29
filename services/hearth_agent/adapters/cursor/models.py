"""
cursor model listing (``/api/models``).

Rows come from the manifest, like claude_code's: the default model plus ``model.allowed`` — what a job
(``JobSpec.model``), a chat turn (``ChatSpec.model``) or ``HEARTH_CURSOR_MODEL`` may select. ``auto`` lets
Cursor choose (no ``--model`` flag); any other id is passed to ``cursor-agent --model`` as is, and
``cursor-agent models`` lists what the signed-in account offers. Row shape is the shared one.
"""

from __future__ import annotations

from services.hearth_agent.registry.agent_registry import get_agent


def list_models() -> list[dict]:
    """One row per selectable model, the default first, as ``<prefix>/<model>``."""
    agent = get_agent("cursor")
    model_cfg = agent.raw.get("model") or {}
    ids: list[str] = []
    for model in [agent.model_default, *(model_cfg.get("allowed") or [])]:
        if model and model not in ids:
            ids.append(model)
    return [
        {
            "id": f"{agent.model_prefix}/{model}",
            "name": model,
            "provider_id": agent.name,
            "capabilities": dict(agent.model_capabilities),
            "pricing": {"prompt": 0, "completion": 0},  # billed by the customer's Cursor account
            "metadata": {"model": model, "default": model == agent.model_default},
        }
        for model in ids
    ]


__all__ = ["list_models"]
