from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import AgentResult, ChatSpec, JobSpec
from services.hearth_agent.registry.adapter_registry import get_adapter
from services.hearth_agent.registry.agent_registry import get_active_agent
from services.hearth_agent.registry.settings import load_agent_config


class AgentDispatcher:
    """
    Thin orchestration layer used by routers and by Hearth-backend's harness.
    Callers import AgentDispatcher, not individual adapters.

    Shape mirrors xo-space ``engine/dispatcher.py``.
    """

    def __init__(self, agent_name: str | None = None):
        self.agent_name = agent_name or get_active_agent().name
        config = load_agent_config(self.agent_name)
        self.adapter = get_adapter(self.agent_name, config)

    async def run(self, job: JobSpec, workspace: Path, hooks: HarnessHooks, **kwargs: Any) -> AgentResult:
        return await self.adapter.run(job, workspace, hooks, **kwargs)

    async def stream(self, job: JobSpec, workspace: Path, hooks: HarnessHooks) -> AsyncIterator[dict[str, Any]]:
        async for event in self.adapter.stream(job, workspace, hooks):
            yield event

    @property
    def supports_chat(self) -> bool:
        return self.adapter.supports_chat

    async def chat(self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge) -> AsyncIterator[dict[str, Any]]:
        async for event in self.adapter.chat(spec, workspace, bridge):
            yield event

    async def health(self) -> dict[str, Any]:
        return await self.adapter.health()
