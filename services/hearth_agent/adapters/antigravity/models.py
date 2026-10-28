"""
antigravity model listing (``/api/models``).

Rows come from the manifest, like claude_code's: the default model plus ``model.allowed`` — names from
agy's own catalog (``agy models``), the only ones ``agy --model`` accepts. Row shape is the shared one.
"""

from __future__ import annotations

from services.hearth_agent.registry.agent_registry import get_agent


def list_models() -> list[dict]:
    """One row per selectable model, the default first, as ``<prefix>/<model>``."""
    agent = get_agent("antigravity")
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
            "pricing": {"prompt": 0, "completion": 0},  # billed by the customer's Google account
            "metadata": {"model": model, "default": model == agent.model_default},
        }
        for model in ids
    ]


__all__ = ["list_models"]
