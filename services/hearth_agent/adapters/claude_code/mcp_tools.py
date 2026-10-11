"""
HEARTH's domain tools, served to Claude Code from an in-process MCP server.

``mcp_config.py`` wraps these tools in the session's in-process MCP server.
Tool names are listed in ``manifest.json`` → ``mcp.tools``. Chat turns get a
second, per-turn set instead: the tools the Hearth web app runs itself.

``finish`` is gated: it is accepted only when the harness's independent
validation passes (TDD §3 items 3-4), at most ``limits.max_validation_rounds``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.adapters.claude_code.prompts import build_fixup_message
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import ChatTool, JobSpec, ValidationResult

Emit = Callable[[dict], Awaitable[None]]


@dataclass
class JobState:
    finished: bool = False
    summary: str = ""
    needs_attention: list[str] = field(default_factory=list)
    validation_rounds: int = 0
    last_validation: ValidationResult | None = None
    out_of_rounds: bool = False
    policy_violations: int = 0


def _text(s: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": s}]}
    if error:
        out["is_error"] = True
    return out


def build_domain_tools(job: JobSpec, hooks: HarnessHooks, state: JobState, emit: Emit) -> list[SdkMcpTool]:
    @tool("get_change_record", "The provider change to apply: kind, symbols before/after, notes, codemod.", {})
    async def get_change_record(_: dict[str, Any]) -> dict[str, Any]:
        return _text(job.change_record.model_dump_json(indent=2, by_alias=True, exclude_none=True))

    @tool("get_impact_report", "Exact call sites (path, line, symbol) affected in this repository.", {})
    async def get_impact_report(_: dict[str, Any]) -> dict[str, Any]:
        return _text(job.impact_report.model_dump_json(indent=2, exclude_none=True))

    @tool("run_validation", "Run the repository's own install/build/lint/typecheck/test commands.", {})
    async def run_validation(_: dict[str, Any]) -> dict[str, Any]:
        v = await hooks.validate_candidate()
        state.last_validation = v
        await emit(se.validation(phase="agent_requested", passed=v.passed, regressions=v.regressions))
        if v.passed and not v.regressions:
            return _text("Validation passed: no new failures compared with the baseline.")
        return _text(build_fixup_message(v).replace("finish rejected: ", ""), error=True)

    @tool(
        "finish",
        "Declare the change complete. Accepted only if an independent validation passes.",
        {"type": "object", "properties": {
            "summary": {"type": "string", "description": "Files changed, why, and anything a human must check."},
            "needs_human_attention": {"type": "array", "items": {"type": "string"}},
        }, "required": ["summary"]},
    )
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        state.validation_rounds += 1
        v = await hooks.validate_candidate()
        state.last_validation = v
        ok = v.passed and not v.regressions
        await emit(se.validation(phase="finish", passed=ok, regressions=v.regressions, round=state.validation_rounds))
        if ok:
            state.finished = True
            state.summary = str(args.get("summary", ""))
            state.needs_attention = [str(x) for x in args.get("needs_human_attention") or []]
            return _text("finish accepted. Stop now; do not make further changes.")
        if state.validation_rounds >= job.limits.max_validation_rounds:
            state.out_of_rounds = True
            return _text("finish rejected and the validation budget is exhausted. Stop now.", error=True)
        return _text(build_fixup_message(v), error=True)

    return [get_change_record, get_impact_report, run_validation, finish]


HANDOFF_ACK = (
    "Shown to the owner in the Hearth web app. Stop here and end your turn without calling more tools; "
    "their decision arrives as the next message."
)


def build_client_tools(tools: list[ChatTool], bridge: ToolResultBridge) -> list[SdkMcpTool]:
    """A chat turn's caller-run tools (``ChatSpec.tools``). The web app runs each call and posts the result
    back through the bridge; ``handoff`` tools are acknowledged at once because the owner answers later."""

    def make(spec: ChatTool) -> SdkMcpTool:
        @tool(spec.name, spec.description, spec.input_schema)
        async def call(_: dict[str, Any]) -> dict[str, Any]:
            if spec.handoff:
                return _text(HANDOFF_ACK)
            tool_use_id = bridge.take(spec.name)
            if tool_use_id is None:
                return _text(f"{spec.name} could not be matched to its call; try again.", error=True)
            output, is_error = await bridge.wait(tool_use_id)
            return _text(output, error=is_error)

        return call

    return [make(spec) for spec in tools]

