"""Claude Code adapter tests. No model calls: the SDK's ``query`` is replaced with a fake."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_agent_sdk as sdk

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.adapters.claude_code import mcp_config as mcp_config_mod
from services.hearth_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.hearth_agent.adapters.claude_code.hooks import build_hooks
from services.hearth_agent.adapters.claude_code.mcp_tools import JobState, build_domain_tools
from services.hearth_agent.adapters.claude_code.streaming import parse_message
from services.hearth_agent.engine import sessions_io
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.policy import ToolPolicy
from services.hearth_agent.registry.adapter_registry import get_adapter, list_adapters
from services.hearth_agent.registry.agent_registry import get_active_agent, reset_cache
from tests import fixtures


async def _noop(_event):
    return None


def _hooks(results: list[bool]) -> HarnessHooks:
    seq = iter(results)

    async def validate():
        return fixtures.validation(next(seq))

    return HarnessHooks(validate_candidate=validate, policy=ToolPolicy.for_files(["src/pay.ts"]))


class RegistryTests(unittest.TestCase):
    def test_manifest_and_adapter_discovery(self):
        reset_cache()
        agent = get_active_agent()
        self.assertEqual(agent.name, "claude_code")
        self.assertFalse(agent.flag("dangerously_skip_permissions"))
        self.assertEqual(list_adapters(), ["antigravity", "claude_code", "cursor"])
        self.assertIsInstance(get_adapter("claude_code", {}), ClaudeCodeAdapter)
        with self.assertRaises(ValueError):
            get_adapter("opencode", {})

    def test_refuses_permission_bypass(self):
        with mock.patch.object(ClaudeCodeAdapter, "load_commands",
                               return_value={"flags": {"dangerously_skip_permissions": True}}):
            with self.assertRaises(ValueError):
                ClaudeCodeAdapter({})


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_hook(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            state = JobState()
            pre = build_hooks(ws, ToolPolicy.for_files(["src/pay.ts"]), state, _noop, mcp_server="hearth")["PreToolUse"][0].hooks[0]

            async def decide(tool, args):
                return await pre({"tool_name": tool, "tool_input": args}, "t", None)

            self.assertEqual(await decide("Edit", {"file_path": str(ws / "src" / "pay.ts")}), {})
            self.assertEqual(await decide("mcp__hearth__finish", {"summary": "x"}), {})
            self.assertEqual(await decide("Read", {"file_path": "/etc/hosts"}), {})
            for tool, args in (("Edit", {"file_path": str(ws / ".github/workflows/x.yml")}),
                               ("Bash", {"command": "git push origin HEAD"}),
                               ("WebFetch", {"url": "https://x"})):
                with self.subTest(tool=tool):
                    out = await decide(tool, args)
                    self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(state.policy_violations, 3)


class FinishGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_then_accepted(self):
        job, state = fixtures.job(), JobState()
        tools = {t.name: t for t in build_domain_tools(job, _hooks([False, True]), state, _noop)}
        first = await tools["finish"].handler({"summary": "done"})
        self.assertTrue(first.get("is_error"))
        self.assertFalse(state.finished)
        self.assertIn("test_pay", first["content"][0]["text"])
        second = await tools["finish"].handler({"summary": "done", "needs_human_attention": ["check x"]})
        self.assertFalse(second.get("is_error"))
        self.assertTrue(state.finished)
        self.assertEqual((state.needs_attention, state.validation_rounds), (["check x"], 2))

    async def test_stops_after_max_rounds(self):
        job, state = fixtures.job(), JobState()
        job.limits.max_validation_rounds = 2
        tools = {t.name: t for t in build_domain_tools(job, _hooks([False, False]), state, _noop)}
        await tools["finish"].handler({"summary": "a"})
        out = await tools["finish"].handler({"summary": "b"})
        self.assertTrue(out.get("is_error") and state.out_of_rounds and not state.finished)


class StreamingTests(unittest.TestCase):
    def test_parse_message(self):
        self.assertEqual(parse_message(sdk.SystemMessage(subtype="init", data={"session_id": "s1"})),
                         [se.session_id("s1")])
        events = parse_message(sdk.AssistantMessage(content=[
            sdk.TextBlock(text="Migrating."),
            sdk.ToolUseBlock(id="t1", name="Bash", input={"command": "pnpm test"}),
        ], model="m"))
        self.assertEqual([e["type"] for e in events], [se.TOKEN, se.ACTIVITY, se.TOOL_CALL])
        self.assertEqual(events[1]["label"], se.RUNNING_COMMAND)
        self.assertEqual(events[2]["input"], {"command": "pnpm test"})


class AdapterStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_run_with_fake_sdk(self):
        captured: dict = {}
        real_build = mcp_config_mod.build_domain_tools

        def capture(*a, **k):
            tools = real_build(*a, **k)
            captured.update({t.name: t for t in tools})
            return tools

        async def fake_query(*, prompt, options):
            # the options the adapter derives from manifest.json
            self.assertEqual(options.setting_sources, [])
            self.assertEqual(options.permission_mode, "default")
            self.assertEqual(options.model, "claude-opus-5-5")
            self.assertIn("WebFetch", options.disallowed_tools)
            self.assertIn("hearth", options.mcp_servers)
            self.assertEqual(options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
            self.assertIsNone(options.resume)  # a first job starts a fresh session...
            sid = options.session_id  # ...under the id the adapter pre-allocated, as the CLI does
            seen["preliminary"] = sessions_io.read_session_row("job_1")
            yield sdk.SystemMessage(subtype="init", data={"session_id": sid})
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Migrating.")], model="m")
            await captured["finish"].handler({"summary": "Swapped charges for paymentIntents."})
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                    num_turns=1, session_id=sid, total_cost_usd=0.01,
                                    usage={"input_tokens": 100, "output_tokens": 20})

        events: list[dict] = []
        seen: dict = {}
        with mock.patch.object(mcp_config_mod, "build_domain_tools", capture), \
             mock.patch.object(sdk, "query", fake_query), \
             tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": d}):
            result = await ClaudeCodeAdapter({}).run(fixtures.job(), Path(d) / "ws", _hooks([True]),
                                                     on_event=events.append)
            row = sessions_io.read_session_row("job_1")

        # the index row was written before the CLI started, then rolled up at the end
        self.assertEqual(seen["preliminary"]["status"], "running")
        self.assertEqual(seen["preliminary"]["nativeSessionId"], row["nativeSessionId"])
        self.assertEqual((row["status"], row["usage"]["input_tokens"], row["costUsd"]), ("passed", 100, 0.01))
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.native_session_id, row["nativeSessionId"])
        self.assertEqual(result.summary, "Swapped charges for paymentIntents.")
        self.assertEqual((result.usage.input_tokens, result.usage.cost_usd), (100, 0.01))
        self.assertTrue(events[-1]["done"])
        self.assertIn(se.VALIDATION, [e.get("type") for e in events])
        json.dumps(events)  # every event is JSON-serialisable for events.jsonl


if __name__ == "__main__":
    unittest.main()
