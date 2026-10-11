"""
Per-session MCP configuration for Claude Code.

xo-space materialises a Composio MCP proxy URL into a JSON file per session and
passes ``--mcp-config <file>`` to the CLI, deleting the file afterwards. HEARTH's
only MCP server is its own in-process one (the domain tools in ``mcp_tools.py``),
handed to the SDK as an object, so there is no file to write or clean up and no
network endpoint for the sandbox to reach.

External MCP servers are deliberately not supported: a sandbox may reach only the
package registries and the LLM gateway (TDD §6 Network). ``manifest.json``
``mcp.server_name`` names the in-process server (default ``hearth``). A chat turn
gets its own in-process server, ``client``, holding the tools the web app runs.

Shape mirrors xo-space ``adapters/claude_code/mcp_config.py``.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.adapters.claude_code.mcp_tools import Emit, JobState, build_client_tools, build_domain_tools
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import ChatSpec, JobSpec

DEFAULT_SERVER_NAME = "hearth"


def server_name(manifest: dict[str, Any]) -> str:
    return (manifest.get("mcp") or {}).get("server_name") or DEFAULT_SERVER_NAME


def build_server(tools: list, name: str = DEFAULT_SERVER_NAME):
    return create_sdk_mcp_server(name, tools=tools)


def session_mcp_servers(
    job: JobSpec, hooks: HarnessHooks, state: JobState, emit: Emit, *, manifest: dict[str, Any],
) -> dict[str, Any]:
    """The ``mcp_servers`` mapping for one job's session: just HEARTH's in-process server."""
    name = server_name(manifest)
    return {name: build_server(build_domain_tools(job, hooks, state, emit), name)}


# The in-process server holding a chat turn's caller-run tools. Its tools reach the agent as
# ``mcp__client__<name>``; the web app strips that prefix when it shows them.
CLIENT_SERVER_NAME = "client"


def chat_mcp_servers(spec: ChatSpec, bridge: ToolResultBridge) -> dict[str, Any]:
    """The ``mcp_servers`` mapping for one chat turn: the web app's tools, when it offered any."""
    if not spec.tools:
        return {}
    return {CLIENT_SERVER_NAME: build_server(build_client_tools(spec.tools, bridge), CLIENT_SERVER_NAME)}
