"""
Tool-layer policy for Cursor, the counterpart of ``claude_code/hooks.py``.

Claude Code asks HEARTH about every tool call in an SDK ``PreToolUse`` hook. ``cursor-agent``'s print
mode offers no such callback, so the same :class:`~services.hearth_agent.policy.ToolPolicy` is applied
twice (TDD §6 "Agent guardrails"):

1. **Before the run**, as Cursor's own permission rules (:func:`permission_rules`, written to the
   workspace's ``.cursor/cli.json`` for the length of the run by ``mcp_config.project_config``).
   ``deny`` covers what the policy never allows — CI config, secrets, ``.git/``, ``.cursor/``, network
   tools, ``sudo``, git remotes, publishing, web fetches, other MCP servers — and Cursor honours a deny
   rule even under ``--force``. ``allow`` lists what the turn may do. Print mode applies file edits only
   with ``--force``, which also lets through any command no rule names, so the adapter passes it only on
   turns that may edit (``manifest.json`` ``flags.force: "edit_turns"``).
2. **On the stream**, as :func:`check_tool` for every ``tool_call`` Cursor reports. A forbidden call
   Cursor refused is recorded as ``policy-denied``; a forbidden call that *ran* (``--force`` let a command
   through that HEARTH's allowlist does not name) stops the turn, and a job ends as ``policy_violation``.

The sandbox (``--sandbox enabled``: no network, writes only in the workspace) and the space container
are the second line; none of them trusts the agent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.hearth_agent.policy import (
    ALWAYS_DENIED_PATHS,
    DENIED_BINARIES,
    DENIED_GIT_SUBCOMMANDS,
    TEST_PATH_GLOBS,
    Decision,
    ToolPolicy,
)

CLIENT_SERVER_NAME = "client"

# Cursor's tool_call kinds → (HEARTH action, the name the audit log and the web app show).
# The read and write shapes are documented (cursor.com/docs/cli/reference/output-format); the rest
# follow the same ``<kind>ToolCall: {args, result}`` pattern.
_KINDS: dict[str, tuple[str, str]] = {
    "readToolCall": ("read", "Read"),
    "lsToolCall": ("read", "LS"),
    "grepToolCall": ("read", "Grep"),
    "globToolCall": ("read", "Glob"),
    "writeToolCall": ("edit", "Write"),
    "editToolCall": ("edit", "Edit"),
    "deleteToolCall": ("edit", "Delete"),
    "shellToolCall": ("shell", "Bash"),
    "todoToolCall": ("allowed", "TodoWrite"),
    "updateTodosToolCall": ("allowed", "TodoWrite"),
    "mcpToolCall": ("mcp", "mcp"),
    "function": ("mcp", "function"),
}
_PATH_KEYS = ("path", "filePath", "file_path", "targetFile", "target_file")
_SERVER_KEYS = ("providerIdentifier", "serverName", "server", "mcpServer")
_TOOL_KEYS = ("toolName", "tool", "name")
_CLIENT_PREFIXES = (f"mcp__{CLIENT_SERVER_NAME}__", f"mcp_{CLIENT_SERVER_NAME}_", f"{CLIENT_SERVER_NAME}-")
# Package-manager subcommands HEARTH's policy refuses (``policy._check_segment``).
_DENIED_PUBLISH = ("publish", "login", "adduser")


@dataclass(frozen=True)
class Action:
    """One Cursor tool call, as HEARTH's policy sees it."""

    kind: str  # read | edit | shell | mcp | allowed | unknown
    name: str  # shown in audit events, e.g. "Write", "Bash", "mcp__client__list_space_pages"
    input: dict[str, Any] = field(default_factory=dict)
    path: str | None = None
    command: str | None = None
    server: str | None = None
    tool: str | None = None


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}
    return {}


def _first(args: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _mcp_target(server: str | None, tool: str, client_tools: frozenset[str]) -> tuple[str | None, str]:
    """``(server, tool)`` of an MCP call. A name Cursor reports with the client prefix, or with no server at
    all, is the web app's tool only when the turn offered a tool of exactly that name."""
    for prefix in _CLIENT_PREFIXES:
        if tool.startswith(prefix):
            server, tool = server or CLIENT_SERVER_NAME, tool[len(prefix):]
            break
    if server is None and tool in client_tools:
        server = CLIENT_SERVER_NAME
    return server, tool


def classify(tool_call: dict[str, Any], client_tools: frozenset[str] = frozenset()) -> Action:
    """Turn a stream event's ``tool_call`` object (``{"<kind>": {"args": ..., "result": ...}}``) into an :class:`Action`."""
    kind_key = next(iter(tool_call), "") if isinstance(tool_call, dict) else ""
    body = tool_call.get(kind_key) if kind_key else None
    body = body if isinstance(body, dict) else {}
    kind, name = _KINDS.get(kind_key, ("unknown", kind_key.removesuffix("ToolCall") or "unknown"))

    if kind_key == "function":
        args = _arguments(body.get("arguments"))
        server, tool = _mcp_target(None, str(body.get("name") or ""), client_tools)
        return Action("mcp", f"mcp__{server or 'unknown'}__{tool}", args, server=server, tool=tool)

    args = _arguments(body.get("args"))
    if kind == "mcp":
        server, tool = _mcp_target(_first(args, _SERVER_KEYS), _first(args, _TOOL_KEYS) or "", client_tools)
        inner = _arguments(args.get("args") or args.get("arguments"))
        return Action("mcp", f"mcp__{server or 'unknown'}__{tool or 'unknown'}", inner or args,
                      server=server, tool=tool)
    if kind == "edit":
        return Action(kind, name, args, path=_first(args, _PATH_KEYS))
    if kind == "shell":
        command = args.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return Action(kind, name, args, command=command if isinstance(command, str) else None)
    return Action(kind, name, args)


def check_tool(
    action: Action, policy: ToolPolicy, workspace: Path, *,
    files: bool, shell: bool, client_tools: frozenset[str] = frozenset(),
) -> Decision:
    """``files`` / ``shell``: what this turn allows (a chat turn's permissions; always both for a job)."""
    if action.kind in {"read", "allowed"}:
        return Decision.allow()
    if action.kind == "edit":
        if not files:
            return Decision.deny("file edits are switched off for this conversation")
        if not action.path:
            return Decision.deny(f"could not tell which file {action.name} changes")
        return policy.check_edit(action.path, workspace_root=str(workspace))
    if action.kind == "shell":
        if not shell:
            return Decision.deny("shell commands are switched off for this conversation")
        return policy.check_command(action.command or "")
    if action.kind == "mcp":
        if action.server == CLIENT_SERVER_NAME and action.tool in client_tools:
            return Decision.allow()
        return Decision.deny(f"MCP server {action.server or 'unknown'} is not available to HEARTH agents")
    return Decision.deny(f"tool {action.name} is not available to HEARTH jobs")


def permission_rules(
    policy: ToolPolicy, *, files: bool, shell: bool, client_tools: bool, other_servers: list[str] | None = None,
) -> dict[str, list[str]]:
    """Cursor's ``permissions`` object (``{"allow": [...], "deny": [...]}``) for one run. Deny wins in Cursor."""
    allow = ["Read(**)"]
    if files:
        scope = list(policy.allowed_edit_paths)
        if scope and policy.allow_test_edits:
            scope += TEST_PATH_GLOBS
        allow += [f"Write({glob})" for glob in scope] or ["Write(**)"]
    if shell:
        allow += [f"Shell({binary})" for binary in sorted(policy.allowed_binaries)]
    if client_tools:
        allow.append(f"Mcp({CLIENT_SERVER_NAME}:*)")

    deny = [f"Write({glob})" for glob in [*ALWAYS_DENIED_PATHS, ".cursor/**"]]
    deny += [f"Shell({binary})" for binary in sorted(DENIED_BINARIES)]
    deny += [f"Shell(git:{sub}*)" for sub in sorted(DENIED_GIT_SUBCOMMANDS)]
    deny += [f"Shell({pm}:{sub}*)" for pm in ("npm", "pnpm", "yarn", "bun") for sub in _DENIED_PUBLISH]
    if not shell:
        deny.append("Shell(*)")
    deny.append("WebFetch(*)")
    if client_tools:
        deny += [f"Mcp({name}:*)" for name in other_servers or [] if name != CLIENT_SERVER_NAME]
    else:
        deny.append("Mcp(*:*)")
    return {"allow": allow, "deny": deny}
