"""
Tool-layer policy for Claude Code, enforced in SDK hooks.

``PreToolUse`` fires for *every* tool call, whereas the SDK's ``can_use_tool``
only fires for calls that would otherwise prompt — so the hook is the
enforcement point. ``PostToolUse`` records each tool's output for the audit log.
Where xo-space runs the CLI with ``--dangerously-skip-permissions``, HEARTH runs
it with permissions on and this policy in front (TDD §6 "Agent guardrails").
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultDeny

from services.hearth_agent.adapters.claude_code.mcp_tools import JobState
from services.hearth_agent.adapters.claude_code.streaming import trim_output
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.policy import Decision, ToolPolicy

Emit = Callable[[dict], Awaitable[None]]

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Glob", "Grep"}


def _deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def check_tool(
    name: str, args: dict[str, Any], policy: ToolPolicy, workspace: Path, mcp_server: str,
    *, allowed: frozenset[str] | None = None,
) -> Decision:
    """``allowed``, when given, is the turn's built-in tool set (a chat turn's permissions); anything
    outside it is denied even if the policy would let it through."""
    if name.startswith(f"mcp__{mcp_server}__"):
        return Decision.allow()
    if allowed is not None and name not in allowed:
        return Decision.deny(f"tool {name} is switched off for this conversation")
    if name in READ_TOOLS:
        return Decision.allow()
    if name in EDIT_TOOLS:
        path = args.get("file_path") or args.get("notebook_path") or ""
        return policy.check_edit(path, workspace_root=str(workspace))
    if name == "Bash":
        return policy.check_command(args.get("command", ""))
    return Decision.deny(f"tool {name} is not available to HEARTH jobs")


def build_hooks(
    workspace: Path, policy: ToolPolicy, state: JobState, emit: Emit, *, mcp_server: str,
    allowed: frozenset[str] | None = None, on_allow: Callable[[str, str | None], None] | None = None,
) -> dict:
    """``on_allow(tool_name, tool_use_id)`` runs for each allowed call just before it executes."""

    async def pre_tool_use(inp: dict[str, Any], tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
        name = inp.get("tool_name", "")
        args = inp.get("tool_input") or {}
        d = check_tool(name, args, policy, workspace, mcp_server, allowed=allowed)
        if d.allowed:
            if on_allow is not None:
                on_allow(name, tool_use_id)
            return {}
        state.policy_violations += 1
        await emit(se.policy_denied(name, tool_use_id, args, d.reason))
        return _deny(d.reason)

    async def post_tool_use(inp: dict[str, Any], tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
        await emit(se.tool_result(inp.get("tool_name"), tool_use_id, trim_output(inp.get("tool_response"))))
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_use])],
    }


async def deny_prompts(name: str, _input: dict[str, Any], _ctx: Any) -> PermissionResultDeny:
    """``can_use_tool``: reached only for calls the CLI would otherwise prompt for. Never approve those."""
    return PermissionResultDeny(message=f"{name} needs approval, which HEARTH jobs never grant")
