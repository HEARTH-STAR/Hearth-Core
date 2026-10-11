from __future__ import annotations

import json
import pathlib
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import AgentResult, ChatSpec, JobSpec, ValidationResult
from services.hearth_agent.policy import ToolPolicy

# Repo root, resolved from this file so manifest lookups are independent of the
# process CWD (adapters → hearth_agent → services → repo root).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

ValidateFn = Callable[[], Awaitable[ValidationResult]]


@dataclass
class HarnessHooks:
    """What the harness lends the agent for one job.

    ``validate_candidate`` re-checks the agent's current diff in a *clean* sandbox
    against the baseline, and is the only path to a successful ``finish``
    (HEARTH TDD §3 items 3-4: the agent's own claim of success is never trusted).
    """

    validate_candidate: ValidateFn
    policy: ToolPolicy = field(default_factory=ToolPolicy)
    llm_base_url: str | None = None  # the space's LLM gateway
    llm_env: dict[str, str] = field(default_factory=dict)  # scoped provider keys
    run_as_user: str | None = None  # sandbox runner user


class BaseAgentAdapter(ABC):
    """
    All agent adapters must subclass this.
    ``config`` is a plain dict loaded by ``registry.settings.load_agent_config(adapter_name)``.

    Shape mirrors xo-space ``adapters/base.py``; HEARTH's unit of work is a
    remediation job rather than a chat question, so ``run``/``stream`` take a
    :class:`JobSpec`, the sandbox workspace and the :class:`HarnessHooks`.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config

    # ── Abstract (must implement) ──────────────────────────────────────────────

    @abstractmethod
    def stream(self, job: JobSpec, workspace: Path, hooks: HarnessHooks) -> AsyncIterator[dict[str, Any]]:
        """
        Streaming execution.
        Must yield ``engine.stream_events`` dicts and end with exactly one
        ``{"done": True, "native_session_id": str | None, "result": <AgentResult dict>}``.
        """

    # ── Concrete (override when needed) ───────────────────────────────────────

    async def run(
        self,
        job: JobSpec,
        workspace: Path,
        hooks: HarnessHooks,
        on_event: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> AgentResult:
        """Non-streaming execution: drain :meth:`stream` and return the :class:`AgentResult`."""
        result: AgentResult | None = None
        async for event in self.stream(job, workspace, hooks):
            if on_event is not None:
                maybe = on_event(event)
                if maybe is not None:
                    await maybe
            if event.get("done"):
                result = AgentResult.model_validate(event["result"])
        if result is None:
            result = AgentResult(job_id=job.job_id, intelligence=self.adapter_name, status="error",
                                 summary="adapter ended without a done event")
        return result

    supports_chat: bool = False

    def chat(self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge) -> AsyncIterator[dict[str, Any]]:
        """One chat turn from the Hearth web app (set ``supports_chat`` when implemented).

        Same event vocabulary as :meth:`stream`, run in ``workspace`` with the owner's permissions
        (``spec.permissions``) and the caller's tools (``spec.tools``, answered through ``bridge``).
        Turns with the same ``spec.session`` continue one agent session. Ends with exactly one
        ``{"done": True, "native_session_id", "result"}``.
        """
        raise NotImplementedError(f"{self.adapter_name} does not support chat")

    async def setup(self) -> bool:
        """One-time credential or gateway setup. Return True when ready."""
        return True

    async def health(self) -> dict[str, Any]:
        """Lightweight liveness check surfaced by /health."""
        return {"ok": True}

    def load_commands(self) -> dict[str, Any]:
        """Read ``config/agents/{adapter_name}/manifest.json``. Returns {} if absent."""
        p = _REPO_ROOT / "config" / "agents" / self.adapter_name / "manifest.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    # ── Required class attribute ───────────────────────────────────────────────

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Snake-case name matching the config/agents/ directory, e.g. 'claude_code'."""
