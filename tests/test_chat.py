"""Chat turns from the Hearth web app: the tool-result bridge, the adapter's chat options and turn, and the
SSE route. No model calls: the SDK's ``query`` is replaced with a fake."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_agent_sdk as sdk
from fastapi.testclient import TestClient

from routers.hearth_agent import chat as chat_route
from server import app
from services.hearth_agent.adapters.claude_code import mcp_config as mcp_config_mod
from services.hearth_agent.adapters.claude_code.adapter import ClaudeCodeAdapter, chat_tools
from services.hearth_agent.adapters.claude_code.hooks import check_tool, deny_prompts
from services.hearth_agent.adapters.claude_code.mcp_tools import HANDOFF_ACK, build_client_tools
from services.hearth_agent.adapters.claude_code.prompts import CHAT_RULES
from services.hearth_agent.adapters.claude_code.streaming import parse_partial
from services.hearth_agent.engine import chat_bridge, sessions_io
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.models import ChatPermissions, ChatSpec, ChatTool
from services.hearth_agent.policy import ToolPolicy

MANIFEST_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]


def _spec(**overrides) -> ChatSpec:
    data = {
        "session": "thread-1",
        "message": "Summarize notes.md",
        "system": "You are Scout, a researcher.",
        "directory": "dots/scout",
        "permissions": {"files": True, "shell": False},
        "tools": [
            {"name": "list_space_pages", "description": "List pages.", "input_schema": {"type": "object"}},
            {"name": "review_space_page", "description": "Ask the owner.", "handoff": True},
        ],
    }
    data.update(overrides)
    return ChatSpec.model_validate(data)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_posted_before_the_handler_waits(self):
        bridge = chat_bridge.ToolResultBridge(timeout=1)
        bridge.expect("list_space_pages", "tu1")
        self.assertTrue(bridge.resolve("tu1", "[]"))
        self.assertEqual(bridge.take("list_space_pages"), "tu1")
        self.assertEqual(await bridge.wait("tu1"), ("[]", False))
        self.assertFalse(bridge.resolve("tu1", "again"))  # one result per call

    async def test_result_posted_while_the_handler_waits(self):
        bridge = chat_bridge.ToolResultBridge(timeout=1)
        waiter = asyncio.create_task(bridge.wait("tu2"))
        await asyncio.sleep(0)
        self.assertTrue(bridge.resolve("tu2", "boom", is_error=True))
        self.assertEqual(await waiter, ("boom", True))

    async def test_timeout_and_close(self):
        bridge = chat_bridge.ToolResultBridge(timeout=0.01)
        output, is_error = await bridge.wait("late")
        self.assertTrue(is_error)
        self.assertIn("did not return a result", output)
        self.assertFalse(bridge.resolve("late", "too late"))
        waiter = asyncio.create_task(bridge.wait("pending"))
        await asyncio.sleep(0)
        bridge.close()
        self.assertTrue((await waiter)[1])
        self.assertFalse(bridge.resolve("other", "x"))

    async def test_early_results_are_capped(self):
        bridge = chat_bridge.ToolResultBridge(timeout=1)
        for i in range(chat_bridge.MAX_EARLY_RESULTS):
            self.assertTrue(bridge.resolve(f"id{i}", "x"))
        self.assertFalse(bridge.resolve("one-too-many", "x"))

    def test_one_turn_per_session(self):
        bridge = chat_bridge.open_bridge("s-busy", 1)
        try:
            with self.assertRaises(chat_bridge.SessionBusy):
                chat_bridge.open_bridge("s-busy", 1)
            self.assertIs(chat_bridge.get_bridge("s-busy"), bridge)
        finally:
            chat_bridge.close_bridge("s-busy", bridge)
        self.assertIsNone(chat_bridge.get_bridge("s-busy"))


class ChatToolsTests(unittest.IsolatedAsyncioTestCase):
    def test_permissions_choose_the_built_in_tools(self):
        self.assertEqual(chat_tools(MANIFEST_TOOLS, ChatPermissions()), ["Read", "Glob", "Grep"])
        self.assertEqual(chat_tools(MANIFEST_TOOLS, ChatPermissions(files=True)),
                         ["Read", "Edit", "Write", "Glob", "Grep"])
        self.assertEqual(chat_tools(MANIFEST_TOOLS, ChatPermissions(shell=True)), ["Read", "Glob", "Grep", "Bash"])

    def test_policy_denies_tools_outside_the_turn(self):
        allowed = frozenset(["Read", "Glob", "Grep"])
        ws = Path(".")
        self.assertTrue(check_tool("Read", {}, ToolPolicy(), ws, "client", allowed=allowed).allowed)
        self.assertTrue(check_tool("mcp__client__list_space_pages", {}, ToolPolicy(), ws, "client",
                                   allowed=allowed).allowed)
        denied = check_tool("Bash", {"command": "ls"}, ToolPolicy(), ws, "client", allowed=allowed)
        self.assertFalse(denied.allowed)
        self.assertIn("switched off", denied.reason)
        # the job policy still applies on top: curl stays blocked even with the shell on
        self.assertFalse(check_tool("Bash", {"command": "curl x"}, ToolPolicy(), ws, "client",
                                    allowed=frozenset(["Bash"])).allowed)

    async def test_client_tools(self):
        bridge = chat_bridge.ToolResultBridge(timeout=1)
        tools = {t.name: t for t in build_client_tools(_spec().tools, bridge)}
        handoff = await tools["review_space_page"].handler({"title": "Notes"})
        self.assertEqual(handoff["content"][0]["text"], HANDOFF_ACK)
        bridge.expect("list_space_pages", "tu1")
        bridge.resolve("tu1", '[{"title": "Launch brief"}]')
        result = await tools["list_space_pages"].handler({})
        self.assertEqual(result["content"][0]["text"], '[{"title": "Launch brief"}]')
        self.assertNotIn("is_error", result)

    def test_partial_events(self):
        def event(payload, parent=None):
            return sdk.StreamEvent(uuid="u", session_id="s", event=payload, parent_tool_use_id=parent)

        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}
        self.assertEqual(parse_partial(event(delta)), [se.token("Hi", partial=True)])
        self.assertEqual(parse_partial(event(delta, parent="task-1")), [])  # a subagent's text is not the reply
        thinking = {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "secret"}}
        self.assertEqual(parse_partial(event(thinking)), [])  # thinking text never leaves
        start = {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}}
        self.assertEqual(parse_partial(event(start)), [se.activity(se.RUNNING_COMMAND, partial=True)])
        self.assertEqual(parse_partial(sdk.SystemMessage(subtype="init", data={})), [])

    def test_spec_validation(self):
        with self.assertRaises(ValueError):
            _spec(session="../escape")
        with self.assertRaises(ValueError):
            ChatTool(name="Bad Name", description="")


class AdapterChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_with_fake_sdk(self):
        captured: dict = {}
        real_build = mcp_config_mod.build_client_tools

        def capture(*a, **k):
            tools = real_build(*a, **k)
            captured.update({t.name: t for t in tools})
            return tools

        seen: dict = {}

        async def fake_query(*, prompt, options):
            seen.setdefault("options", []).append(options)
            self.assertEqual(options.setting_sources, [])
            self.assertEqual(options.permission_mode, "default")
            self.assertIs(options.can_use_tool, deny_prompts)
            self.assertEqual(options.tools, ["Read", "Edit", "Write", "Glob", "Grep"])
            self.assertIn("mcp__client__*", options.allowed_tools)
            self.assertIn("WebFetch", options.disallowed_tools)
            self.assertEqual(list(options.mcp_servers), ["client"])
            self.assertTrue(options.system_prompt["append"].startswith(CHAT_RULES))
            self.assertIn("You are Scout", options.system_prompt["append"])
            self.assertTrue(options.cwd.endswith("scout"))
            self.assertTrue(options.include_partial_messages)
            message = [m async for m in prompt][0]
            self.assertEqual(message["message"]["content"], "Summarize notes.md")
            sid = options.resume or options.session_id
            yield sdk.SystemMessage(subtype="init", data={"session_id": sid})
            for part in ("Checking ", "your pages."):
                yield sdk.StreamEvent(uuid="u", session_id=sid, event={
                    "type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}})
            yield sdk.AssistantMessage(content=[
                sdk.TextBlock(text="Checking your pages."),
                sdk.ToolUseBlock(id="tu1", name="mcp__client__list_space_pages", input={}),
            ], model="m")
            pre = options.hooks["PreToolUse"][0].hooks[0]
            self.assertEqual(await pre({"tool_name": "mcp__client__list_space_pages", "tool_input": {}}, "tu1", None),
                             {})
            chat_bridge_ref["bridge"].resolve("tu1", "[]")
            self.assertEqual((await captured["list_space_pages"].handler({}))["content"][0]["text"], "[]")
            denied = await pre({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "tu2", None)
            self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id=sid, total_cost_usd=0.02,
                                    usage={"input_tokens": 50, "output_tokens": 10})

        chat_bridge_ref: dict = {}
        with mock.patch.object(mcp_config_mod, "build_client_tools", capture), \
             mock.patch.object(sdk, "query", fake_query), \
             mock.patch.object(ClaudeCodeAdapter, "_transcript_available", return_value=True), \
             tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": d}):
            adapter = ClaudeCodeAdapter({})
            ws = Path(d) / "dots" / "scout"
            ws.mkdir(parents=True)
            turns = []
            for _ in range(2):
                bridge = chat_bridge_ref["bridge"] = chat_bridge.ToolResultBridge(timeout=1)
                turns.append([e async for e in adapter.chat(_spec(), ws, bridge)])
            row = sessions_io.read_session_row("chat:thread-1")

        first, second = turns
        types = [e.get("type") for e in first]
        tokens = [(e["token"], e.get("partial", False)) for e in first if e.get("type") == se.TOKEN]
        self.assertEqual(tokens, [("Checking ", True), ("your pages.", True), ("Checking your pages.", False)])
        self.assertIn(se.TOOL_CALL, types)
        self.assertIn(se.POLICY_DENIED, types)
        self.assertEqual(first[-1]["result"]["status"], "completed")
        self.assertEqual(first[-1]["result"]["policy_violations"], 1)
        self.assertIsNone(seen["options"][0].resume)  # the first turn starts a session...
        self.assertEqual(seen["options"][1].resume, seen["options"][0].session_id)  # ...the next continues it
        self.assertEqual(second[-1]["native_session_id"], first[-1]["native_session_id"])
        self.assertEqual((row["status"], row["usage"]["input_tokens"]), ("completed", 100))  # summed over turns
        json.dumps(first)


class _FakeDispatcher:
    supports_chat = True

    async def chat(self, spec, directory, bridge):
        assert chat_bridge.get_bridge(spec.session) is bridge
        assert directory.is_dir()
        yield se.token("Hello")
        yield se.tool_call("mcp__client__list_space_pages", "tu1", {})
        yield se.done("native-1", {"session": spec.session, "status": "completed"})


class ChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": self.tmp.name,
                                                "HEARTH_WORKSPACE_DIR": str(Path(self.tmp.name) / "ws")})
        self.env.start()
        self.dispatcher = mock.patch.object(chat_route, "AgentDispatcher", _FakeDispatcher)
        self.dispatcher.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.dispatcher.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_streams_events_as_sse(self):
        body = {"session": "t1", "message": "hi", "directory": "dots/scout"}
        with self.client.stream("POST", "/api/chat", json=body) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]
        self.assertEqual([e.get("type") for e in events], [se.TOKEN, se.TOOL_CALL, None])
        self.assertTrue(events[-1]["done"])
        self.assertTrue((Path(self.tmp.name) / "ws" / "dots" / "scout").is_dir())
        self.assertIsNone(chat_bridge.get_bridge("t1"))  # the turn released its session

    def test_busy_session_and_bad_folder(self):
        bridge = chat_bridge.open_bridge("t2", 1)
        try:
            self.assertEqual(self.client.post("/api/chat", json={"session": "t2", "message": "hi"}).status_code, 409)
        finally:
            chat_bridge.close_bridge("t2", bridge)
        bad = {"session": "t3", "message": "hi", "directory": "../outside"}
        self.assertEqual(self.client.post("/api/chat", json=bad).status_code, 400)

    def test_tool_results(self):
        result = {"tool_use_id": "tu1", "output": "[]"}
        self.assertEqual(self.client.post("/api/chat/t4/tool-results", json=result).status_code, 404)
        bridge = chat_bridge.open_bridge("t4", 1)
        try:
            self.assertEqual(self.client.post("/api/chat/t4/tool-results", json=result).json(), {"ok": True})
            self.assertEqual(self.client.post("/api/chat/t4/tool-results", json=result).status_code, 409)
        finally:
            chat_bridge.close_bridge("t4", bridge)


if __name__ == "__main__":
    unittest.main()
