"""Cursor adapter tests. No model calls: ``cursor-agent`` is replaced by a fake script that replays the
documented ``stream-json`` wire format."""

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.adapters.cursor import login as login_mod
from services.hearth_agent.adapters.cursor.adapter import CursorAdapter, Turn
from services.hearth_agent.adapters.cursor.hooks import Action, check_tool, classify, permission_rules
from services.hearth_agent.adapters.cursor.mcp_config import project_config
from services.hearth_agent.adapters.cursor.mcp_tools import HANDOFF_ACK, SERVER_SCRIPT, ClientToolRelay
from services.hearth_agent.adapters.cursor.models_status import parse_status
from services.hearth_agent.adapters.cursor.prompts import (
    PROMPT_LIMIT_BYTES,
    build_task_message,
    build_turn_prompt,
    parse_attention,
)
from services.hearth_agent.adapters.cursor.streaming import CursorStream
from services.hearth_agent.engine import sessions_io
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import ChatPermissions, ChatSpec, ChatTool
from services.hearth_agent.policy import ToolPolicy
from services.hearth_agent.registry.adapter_registry import get_adapter, list_adapters
from services.hearth_agent.registry.agent_registry import get_active_agent, reset_cache
from tests import fixtures
from utils.commands import spawn

FAKE_CLI = textwrap.dedent('''
    import json, os, sys, time
    args = sys.argv[1:]
    log = os.environ["FAKE_LOG"]
    ws = args[args.index("--workspace") + 1]
    cfg = json.load(open(os.path.join(ws, ".cursor", "cli.json"), encoding="utf-8"))
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "cli": cfg}) + "\\n")
    scenario = os.environ.get("FAKE_SCENARIO", "ok")
    if scenario == "fail":
        print("not logged in", file=sys.stderr)
        sys.exit(1)
    def out(o):
        print(json.dumps(o), flush=True)
    sid = "chat-123"
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "auto", "cwd": ws,
         "apiKeySource": "login", "permissionMode": "default"})
    def call(cid, kind, a, result):
        out({"type": "tool_call", "subtype": "started", "call_id": cid, "tool_call": {kind: {"args": a}},
             "session_id": sid})
        out({"type": "tool_call", "subtype": "completed", "call_id": cid,
             "tool_call": {kind: {"args": a, "result": result}}, "session_id": sid})
    if scenario == "breach":
        call("c1", "writeToolCall", {"path": ".github/workflows/ci.yml", "fileText": "x"},
             {"success": {"path": ".github/workflows/ci.yml"}})
        time.sleep(5)
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "late"}]}})
    elif scenario == "denied":
        call("c1", "shellToolCall", {"command": "curl https://example.com"}, {"rejected": {"reason": "denied"}})
    else:
        call("c1", "writeToolCall", {"path": "src/pay.ts", "fileText": "x"}, {"success": {"path": "src/pay.ts"}})
    text = "Swapped charges.create.\\n\\nNeeds human attention:\\n- check the webhook\\n"
    if "--stream-partial-output" in args:
        for chunk in (text[:10], text[10:]):
            out({"type": "assistant", "timestamp_ms": 1, "message": {"role": "assistant",
                 "content": [{"type": "text", "text": chunk}]}, "session_id": sid})
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
             "session_id": sid})
    else:
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
             "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "duration_ms": 5,
         "duration_api_ms": 4, "session_id": sid})
''')

FAKE_LOGIN = textwrap.dedent('''
    import sys, time
    print("Open the following link to log in: https://cursor.com/loginDeepControl?uuid=abc&mode=login", flush=True)
    time.sleep(0.5)
    print("Login successful!", flush=True)
''')


def _hooks(results: list[bool]) -> HarnessHooks:
    seq = iter(results)

    async def validate():
        return fixtures.validation(next(seq))

    return HarnessHooks(validate_candidate=validate, policy=ToolPolicy.for_files(["src/pay.ts"]))


class _FakeCli(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        (self.ws / "src").mkdir(parents=True)
        self.script = root / "fake_cursor.py"
        self.script.write_text(FAKE_CLI, encoding="utf-8")
        self.log = root / "calls.jsonl"
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": str(root / "state"), "FAKE_LOG": str(self.log),
                                                "CURSOR_HOME": str(root / "cursor-home")})
        self.env.start()
        self.cli = mock.patch.object(CursorAdapter, "_cli", return_value=[sys.executable, str(self.script)])
        self.cli.start()

    def tearDown(self):
        self.cli.stop()
        self.env.stop()
        self.tmp.cleanup()

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        reset_cache()

    def test_discovery_and_default(self):
        reset_cache()
        with mock.patch.dict(os.environ, {"AGENT_NAME": "", "DEFAULT_AGENT": ""}):
            self.assertEqual(get_active_agent().name, "claude_code")  # "default": true
        self.assertEqual(list_adapters(), ["antigravity", "claude_code", "cursor"])
        self.assertIsInstance(get_adapter("cursor", {}), CursorAdapter)
        reset_cache()
        with mock.patch.dict(os.environ, {"AGENT_NAME": "cursor"}):
            self.assertEqual(get_active_agent().name, "cursor")

    def test_refuses_unsafe_flags(self):
        for flags in ({"sandbox": "disabled"}, {"force": "always"}):
            with mock.patch.object(CursorAdapter, "load_commands", return_value={"flags": flags}):
                with self.assertRaises(ValueError):
                    CursorAdapter({})

    def test_command_line(self):
        adapter = CursorAdapter({})
        ws = Path(tempfile.gettempdir())
        read_only = adapter.build_command(Turn(prompt="-hi", workspace=ws, model="auto", policy=ToolPolicy(),
                                               files=False, shell=True))
        self.assertIn("--sandbox", read_only)
        self.assertEqual(read_only[read_only.index("--sandbox") + 1], "enabled")
        self.assertNotIn("--force", read_only)
        self.assertNotIn("--model", read_only)
        self.assertEqual(read_only[-2:], ["--", "-hi"])  # a prompt starting with "-" is never an option
        editing = adapter.build_command(Turn(prompt="hi", workspace=ws, model="gpt-5", policy=ToolPolicy(),
                                             files=True, shell=True, resume="chat-1", partial=True))
        self.assertIn("--force", editing)
        self.assertIn("--resume=chat-1", editing)
        self.assertIn("--stream-partial-output", editing)
        self.assertEqual(editing[editing.index("--model") + 1], "gpt-5")


class StreamingTests(unittest.TestCase):
    def line(self, obj) -> bytes:
        return json.dumps(obj).encode() + b"\n"

    def test_partial_text_then_one_full_token(self):
        s = CursorStream(partial=True)
        events = [*s.feed(self.line({"type": "system", "subtype": "init", "session_id": "c1"})),
                  *s.feed(self.line({"type": "assistant", "timestamp_ms": 1,
                                     "message": {"content": [{"type": "text", "text": "Hel"}]}})),
                  *s.feed(self.line({"type": "assistant", "timestamp_ms": 2,
                                     "message": {"content": [{"type": "text", "text": "lo"}]}})),
                  *s.feed(self.line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}})),
                  *s.feed(b"not json\n")]
        self.assertEqual(events[0], se.session_id("c1"))
        self.assertEqual([e for e in events if e.get("partial")], [se.token("Hel", partial=True),
                                                                    se.token("lo", partial=True)])
        self.assertEqual([e for e in events if e["type"] == "token" and not e.get("partial")], [se.token("Hello")])

    def test_tool_calls_are_audited_and_queued(self):
        s = CursorStream(client_tools=frozenset({"list_space_pages"}))
        started = s.feed(self.line({"type": "tool_call", "subtype": "started", "call_id": "x",
                                    "tool_call": {"shellToolCall": {"args": {"command": "ls -la"}}}}))
        self.assertEqual(started, [se.running("Bash"), se.tool_call("Bash", "x", {"command": "ls -la"})])
        done = s.feed(self.line({"type": "tool_call", "subtype": "completed", "call_id": "x",
                                 "tool_call": {"shellToolCall": {"args": {"command": "ls"},
                                                                 "result": {"success": {"stdout": "a"}}}}}))
        self.assertEqual(done[0]["type"], "tool-result")
        client = s.feed(self.line({"type": "tool_call", "subtype": "started", "call_id": "y",
                                   "tool_call": {"function": {"name": "list_space_pages", "arguments": "{}"}}}))
        self.assertEqual(client, [])  # the relay reports the web app's own tools
        calls = s.pop_calls()
        self.assertEqual([(c.phase, c.action.kind) for c in calls],
                         [("started", "shell"), ("completed", "shell"), ("started", "mcp")])
        self.assertTrue(calls[1].succeeded)

    def test_error_result(self):
        events = CursorStream().feed(self.line({"type": "result", "subtype": "error", "is_error": True,
                                                "result": "quota"}))
        self.assertEqual(events[0], se.error("quota"))


class PolicyTests(unittest.TestCase):
    def test_check_tool(self):
        ws = Path("/w")
        policy = ToolPolicy.for_files(["src/pay.ts"])
        write = classify({"writeToolCall": {"args": {"path": "/w/src/pay.ts"}}})

        def check(action, files=True, shell=True, client=frozenset()):
            return check_tool(action, policy, ws, files=files, shell=shell, client_tools=client).allowed

        self.assertTrue(check(write))
        self.assertFalse(check(write, files=False))
        self.assertFalse(check(classify({"editToolCall": {"args": {"path": ".github/workflows/ci.yml"}}})))
        self.assertFalse(check(classify({"deleteToolCall": {"args": {"path": "src/other.ts"}}})))
        self.assertTrue(check(classify({"shellToolCall": {"args": {"command": "pnpm test"}}})))
        self.assertFalse(check(classify({"shellToolCall": {"args": {"command": "git push origin main"}}})))
        self.assertFalse(check(classify({"shellToolCall": {"args": {"command": "ls"}}}), shell=False))
        self.assertTrue(check(classify({"readToolCall": {"args": {"path": "README.md"}}}), files=False, shell=False))
        mcp = classify({"function": {"name": "mcp__client__read_space_page", "arguments": '{"id": 1}'}},
                       frozenset({"read_space_page"}))
        self.assertEqual((mcp.server, mcp.tool, mcp.input), ("client", "read_space_page", {"id": 1}))
        self.assertTrue(check(mcp, client=frozenset({"read_space_page"})))
        self.assertFalse(check(classify({"mcpToolCall": {"args": {"providerIdentifier": "github",
                                                                   "toolName": "create_issue"}}})))
        self.assertFalse(check(Action("unknown", "webFetch")))

    def test_permission_rules(self):
        chat = permission_rules(ToolPolicy(), files=False, shell=False, client_tools=False)
        self.assertEqual(chat["allow"], ["Read(**)"])
        for rule in ("Shell(*)", "Mcp(*:*)", "WebFetch(*)", "Write(.github/workflows/**)", "Write(.cursor/**)",
                     "Shell(curl)", "Shell(git:push*)", "Shell(npm:publish*)"):
            self.assertIn(rule, chat["deny"])
        job = permission_rules(ToolPolicy.for_files(["src/pay.ts"]), files=True, shell=True, client_tools=True,
                               other_servers=["github", "client"])
        self.assertIn("Write(src/pay.ts)", job["allow"])
        self.assertIn("Write(**/tests/**)", job["allow"])
        self.assertIn("Shell(pnpm)", job["allow"])
        self.assertIn("Mcp(client:*)", job["allow"])
        self.assertIn("Mcp(github:*)", job["deny"])
        self.assertNotIn("Mcp(client:*)", job["deny"])
        self.assertNotIn("Shell(*)", job["deny"])


class ProjectConfigTests(unittest.TestCase):
    def test_swaps_and_restores_the_workspace_config(self):
        with tempfile.TemporaryDirectory() as d:
            ws, stash = Path(d) / "ws", Path(d) / "stash"
            (ws / ".cursor").mkdir(parents=True)
            stash.mkdir()
            (ws / ".cursor" / "cli.json").write_text('{"permissions": {"allow": ["Shell(*)"]}}', encoding="utf-8")
            (ws / ".cursor" / "hooks.json").write_text("{}", encoding="utf-8")
            with project_config(ws, {"allow": ["Read(**)"], "deny": []}, {"client": {"command": "x"}},
                                stash_dir=stash) as cursor_dir:
                self.assertEqual(json.loads((cursor_dir / "cli.json").read_text())["permissions"]["allow"], ["Read(**)"])
                self.assertFalse((cursor_dir / "hooks.json").exists())  # the repo's hooks never run
                self.assertIn("client", json.loads((cursor_dir / "mcp.json").read_text())["mcpServers"])
            self.assertIn("Shell(*)", (ws / ".cursor" / "cli.json").read_text())
            self.assertTrue((ws / ".cursor" / "hooks.json").exists())
            self.assertFalse((ws / ".cursor" / "mcp.json").exists())

    def test_leaves_no_trace_when_there_was_no_config(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with project_config(ws, {"allow": [], "deny": []}, {}, stash_dir=Path(tempfile.mkdtemp(dir=d))):
                self.assertTrue((ws / ".cursor" / "cli.json").exists())
            self.assertFalse((ws / ".cursor").exists())

    def test_refuses_a_cursor_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".cursor").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                with project_config(Path(d), {"allow": [], "deny": []}, {}, stash_dir=Path(d)):
                    pass


class PromptTests(unittest.TestCase):
    def test_task_message_inlines_the_records(self):
        text = build_task_message(fixtures.job())
        self.assertIn('"id": "cr_test1"', text)
        self.assertIn('"path": "src/pay.ts"', text)
        self.assertIn("HEARTH validates with: pnpm test", text)

    def test_attention_items(self):
        self.assertEqual(parse_attention("Done.\n\nNeeds human attention:\n- a\n* b\n\nbye"), ["a", "b"])
        self.assertEqual(parse_attention("Done."), [])

    def test_prompt_fits_one_argument(self):
        prompt = build_turn_prompt("rules", "c" * 300_000, "hello")
        self.assertLessEqual(len(prompt.encode()), PROMPT_LIMIT_BYTES)
        self.assertTrue(prompt.endswith("hello"))
        with self.assertRaises(ValueError):
            build_turn_prompt("rules", "", "x" * (PROMPT_LIMIT_BYTES + 1))

    def test_status_text(self):
        self.assertEqual(parse_status("\x1b[32m✓\x1b[0m Logged in as me@example.com\n", 0),
                         {"logged_in": True, "email": "me@example.com", "method": "login"})
        self.assertFalse(parse_status("Not logged in", 0)["logged_in"])
        self.assertFalse(parse_status("Logged in as me@example.com", 1)["logged_in"])


class JobTests(_FakeCli):
    async def test_finish_gate_runs_a_fixup_turn_in_the_same_chat(self):
        adapter = CursorAdapter({})
        events = [e async for e in adapter.stream(fixtures.job(), self.ws, _hooks([False, True]))]
        result = events[-1]["result"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["needs_human_attention"], ["check the webhook"])
        self.assertEqual(result["native_session_id"], "chat-123")
        self.assertEqual(result["extra"]["validation_rounds"], 2)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--resume=chat-123", calls[0]["argv"])
        self.assertIn("--resume=chat-123", calls[1]["argv"])
        self.assertIn("independent validation did not pass", calls[1]["argv"][-1])
        self.assertIn("--force", calls[0]["argv"])
        self.assertIn("Write(src/pay.ts)", calls[0]["cli"]["permissions"]["allow"])
        self.assertEqual([e["passed"] for e in events if e.get("type") == "validation"], [False, True])
        self.assertFalse((self.ws / ".cursor").exists())
        row = sessions_io.read_session_row("job_1")
        self.assertEqual((row["backend"], row["nativeSessionId"], row["status"]), ("cursor", "chat-123", "passed"))

    async def test_a_forbidden_call_that_ran_stops_the_job(self):
        with mock.patch.dict(os.environ, {"FAKE_SCENARIO": "breach"}):
            events = [e async for e in CursorAdapter({}).stream(fixtures.job(), self.ws, _hooks([True]))]
        result = events[-1]["result"]
        self.assertEqual(result["status"], "policy_violation")
        self.assertIn(".github/workflows/ci.yml", result["extra"]["breach"])
        self.assertFalse(any(e.get("type") == "validation" for e in events))
        self.assertFalse(any(e.get("token") == "late" for e in events))

    async def test_cli_failure(self):
        with mock.patch.dict(os.environ, {"FAKE_SCENARIO": "fail"}):
            events = [e async for e in CursorAdapter({}).stream(fixtures.job(), self.ws, _hooks([True]))]
        self.assertEqual(events[-1]["result"]["status"], "failed_fix")
        errors = [e["error"] for e in events if e.get("type") == "error"]
        self.assertTrue(errors and "not logged in" in errors[0])


class ChatTests(_FakeCli):
    def spec(self, **kw) -> ChatSpec:
        return ChatSpec(session="s1", message="fix it", system="persona", **kw)

    async def test_chat_turn_and_resume(self):
        adapter = CursorAdapter({})
        bridge = ToolResultBridge(5)
        events = [e async for e in adapter.chat(self.spec(permissions=ChatPermissions(files=True)), self.ws, bridge)]
        self.assertEqual(events[-1]["result"]["status"], "completed")
        self.assertTrue(any(e.get("partial") for e in events))
        full = [e["token"] for e in events if e.get("type") == "token" and not e.get("partial")]
        self.assertEqual(len(full), 1)
        first = self.calls()[0]
        self.assertIn("<hearth-context>\npersona", first["argv"][-1])
        self.assertIn("Shell(*)", first["cli"]["permissions"]["deny"])  # shell is off for this assistant
        with mock.patch("services.hearth_agent.adapters.cursor.adapter.find_chat", return_value=self.ws):
            [e async for e in adapter.chat(self.spec(), self.ws, ToolResultBridge(5))]
        self.assertIn("--resume=chat-123", self.calls()[1]["argv"])
        self.assertNotIn("--force", self.calls()[1]["argv"])  # no file edits on the second turn

    async def test_refused_call_is_recorded(self):
        with mock.patch.dict(os.environ, {"FAKE_SCENARIO": "denied"}):
            events = [e async for e in CursorAdapter({}).chat(self.spec(), self.ws, ToolResultBridge(5))]
        denied = [e for e in events if e.get("type") == "policy-denied"]
        self.assertEqual(len(denied), 1)
        self.assertIn("switched off", denied[0]["reason"])
        self.assertEqual(events[-1]["result"]["policy_violations"], 1)
        self.assertEqual(events[-1]["result"]["status"], "completed")


class ClientToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_server_relays_calls_to_the_web_app(self):
        bridge = ToolResultBridge(5)
        seen: list[dict] = []

        async def emit(event):
            seen.append(event)
            if event["type"] == "tool-call" and event["tool"] == "mcp__client__list_space_pages":
                bridge.resolve(event["tool_use_id"], "3 pages")

        tools = [ChatTool(name="list_space_pages", description="List pages"),
                 ChatTool(name="review_page", description="Review", handoff=True)]
        with tempfile.TemporaryDirectory() as d:
            async with ClientToolRelay(tools, bridge, emit, Path(d)) as relay:
                requests = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "list_space_pages", "arguments": {}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "review_page"}},
                ]
                entry = relay.server_entry()
                self.assertEqual(entry["args"][0], str(SERVER_SCRIPT))
                proc = await spawn([entry["command"], *entry["args"]],
                                   input="".join(json.dumps(r) + "\n" for r in requests))
                replies = {}
                async with proc:
                    async for line in proc.lines():
                        msg = json.loads(line)
                        replies[msg["id"]] = msg
                        if len(replies) == 4:
                            break
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([t["name"] for t in replies[2]["result"]["tools"]], ["list_space_pages", "review_page"])
        self.assertEqual(replies[3]["result"], {"content": [{"type": "text", "text": "3 pages"}], "isError": False})
        self.assertEqual(replies[4]["result"]["content"][0]["text"], HANDOFF_ACK)
        self.assertEqual(sum(e["type"] == "tool-call" for e in seen), 2)

    async def test_relay_refuses_a_wrong_token(self):
        bridge = ToolResultBridge(1)
        seen: list[dict] = []

        async def emit(event):
            seen.append(event)

        with tempfile.TemporaryDirectory() as d:
            async with ClientToolRelay([ChatTool(name="t", description="t")], bridge, emit, Path(d)) as relay:
                spec = json.loads(relay.spec_path.read_text())
                reader, writer = await asyncio.open_connection(spec["host"], spec["port"])
                writer.write(json.dumps({"token": "nope", "name": "t"}).encode() + b"\n")
                await writer.drain()
                self.assertEqual(await reader.read(), b"")
                writer.close()
        self.assertEqual(seen, [])


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_login(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "fake_login.py"
            script.write_text(FAKE_LOGIN, encoding="utf-8")
            with mock.patch.object(login_mod, "login_command", return_value=[sys.executable, str(script)]):
                started = await login_mod.start()
                self.assertTrue(started["auth_url"].startswith("https://cursor.com/loginDeepControl"))
                for _ in range(50):
                    status = await login_mod.poll(started["session_id"])
                    if status["status"] != "pending":
                        break
                    await asyncio.sleep(0.1)
        self.assertEqual(status["status"], "connected")


if __name__ == "__main__":
    unittest.main()
