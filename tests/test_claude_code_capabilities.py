"""Every claude_code capability module (the full xo-space adapter shape), plus the routes that serve them.

No model, network or real Claude config: transcripts live in a temporary
CLAUDE_CONFIG_DIR, state in a temporary HEARTH_STATE_DIR, and `claude auth status`
is mocked.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_agent_sdk as sdk
from claude_agent_sdk import project_key_for_directory
from fastapi.testclient import TestClient

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.adapters.claude_code import (
    _project_encoding,
    agents,
    channels_status,
    models,
    models_status,
    providers_status,
    remote_control,
    session_prompts,
    session_telemetry,
    sessions,
    streaming,
    usage,
    visualizer_source,
)
from services.hearth_agent.adapters.claude_code import adapter as adapter_mod
from services.hearth_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.hearth_agent.adapters.cli_status import CliResult, CliStatusError
from services.hearth_agent.adapters.loader import list_capability_providers, load_capability
from services.hearth_agent.engine import sessions_io
from services.hearth_agent.policy import ToolPolicy
from tests import fixtures

NATIVE = "6f1c2b9e-3d4a-4e8b-9c7d-2a5e8f0b1c3d"  # Claude session ids are UUIDs

XO_SPACE_MODULES = (
    "_project_encoding", "adapter", "agents", "channels_status", "mcp_config", "models", "models_status",
    "providers_status", "remote_control", "routes", "session_prompts", "session_telemetry", "sessions",
    "streaming", "usage", "visualizer_source",
)


class _Env(unittest.IsolatedAsyncioTestCase):
    """Temporary HEARTH_STATE_DIR and CLAUDE_CONFIG_DIR for every test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.claude = root / "claude"
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": str(self.state),
                                                "CLAUDE_CONFIG_DIR": str(self.claude)})
        self.env.start()
        os.environ.pop("CLAUDE_PROJECTS_DIR", None)
        adapter_mod._native_map.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_transcript(self, native_id: str, directory: Path, records: list[dict]) -> Path:
        folder = self.claude / "projects" / project_key_for_directory(str(directory))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{native_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return path


def _records(native_id: str) -> list[dict]:
    return [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": native_id, "timestamp": "2026-10-08T10:00:00Z",
         "message": {"role": "user", "content": "<system-reminder>ignore</system-reminder>Job job_1: apply cr_test1."}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": native_id,
         "timestamp": "2026-10-08T10:00:05Z",
         "message": {"role": "assistant", "id": "msg_1", "model": "claude-opus-5-5",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": {}}],
                     "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 5}}},
        # a second streaming chunk of the same API call: must be counted once
        {"type": "assistant", "uuid": "a1b", "parentUuid": "a1", "sessionId": native_id,
         "timestamp": "2026-10-08T10:00:06Z",
         "message": {"role": "assistant", "id": "msg_1", "model": "claude-opus-5-5",
                     "content": [{"type": "text", "text": "Done."}],
                     "usage": {"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 5}}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1b", "sessionId": native_id, "timestamp": "2026-10-08T10:00:07Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "user", "uuid": "u3", "parentUuid": "u2", "sessionId": native_id, "timestamp": "2026-10-08T10:01:00Z",
         "message": {"role": "user", "content": "finish rejected: independent validation did not pass."}},
    ]


class ShapeTests(unittest.TestCase):
    def test_every_xo_space_module_exists_and_loads_through_the_seam(self):
        for name in XO_SPACE_MODULES:
            with self.subTest(module=name):
                self.assertIn("claude_code", list_capability_providers(name))
                load_capability(name, agent="claude_code")


class StatusCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def test_models_status_view(self):
        self.assertEqual(models_status.build_status_view({"loggedIn": True}),
                         {"default": "claude_code/claude-opus-5-5",
                          "models": [{"id": "claude_code/claude-opus-5-5", "status": "ok"}]})
        self.assertEqual(models_status.build_status_view({"loggedIn": False})["models"][0]["status"], "error")

    async def test_models_status_cli_failures(self):
        cases = [(CliResult(1, "", "boom"), "execution_failed"), (CliResult(0, "not json", ""), "invalid_output"),
                 (CliResult(0, "[]", ""), "invalid_output")]
        for result, code in cases:
            with self.subTest(code=code), mock.patch.object(models_status, "run_cli", mock.AsyncMock(return_value=result)):
                with self.assertRaises(CliStatusError) as ctx:
                    await models_status.get_models_status()
                self.assertEqual(ctx.exception.code, code)

    async def test_channels_status_is_empty(self):
        self.assertEqual(await channels_status.get_channels_status(), {"channels": []})

    def test_models_from_manifest_default_first(self):
        ids = [m["id"] for m in models.list_models()]
        self.assertEqual(ids[0], "claude_code/claude-opus-5-5")
        self.assertIn("claude_code/claude-sonnet-5-5", ids)
        self.assertTrue(models.list_models()[0]["metadata"]["default"])

    async def test_providers_status_routes_by_resolved_auth(self):
        async def status_for(auth: dict, gateway: str | None = None):
            env = {"ANTHROPIC_BASE_URL": gateway} if gateway else {}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(providers_status, "claude_auth_status", mock.AsyncMock(return_value=auth)):
                if not gateway:
                    os.environ.pop("ANTHROPIC_BASE_URL", None)
                return await providers_status.get_providers_status()

        oauth = await status_for({"loggedIn": True})
        self.assertEqual((oauth["oauth"]["claude_code"]["connected"], oauth["api_keys"]["anthropic"]["connected"]),
                         (True, False))
        key = await status_for({"loggedIn": True, "apiKeySource": "ANTHROPIC_API_KEY"})
        self.assertEqual((key["oauth"]["claude_code"]["connected"], key["api_keys"]["anthropic"]["connected"]),
                         (False, True))
        gw = await status_for({"loggedIn": True}, gateway="http://127.0.0.1:4000")
        self.assertTrue(gw["api_keys"]["llm_gateway"]["connected"])
        self.assertFalse(gw["oauth"]["claude_code"]["connected"])
        out = await status_for({"loggedIn": False})
        self.assertFalse(any(v["connected"] for group in ("oauth", "api_keys") for v in out[group].values()))


class AgentsAndStubTests(unittest.TestCase):
    def test_agents_contract(self):
        self.assertEqual([a["name"] for a in agents.list_agents()], ["migrator"])
        self.assertEqual(agents.create_agent({}).status_code, 405)
        self.assertEqual(agents.get_detail("migrator")["model"], "claude-opus-5-5")
        self.assertIsNone(agents.get_detail("someone-else"))
        self.assertEqual(agents.patch("migrator", {}).status_code, 405)
        self.assertIsNone(agents.delete("someone-else"))

    def test_unsupported_capabilities_say_so(self):
        out = session_telemetry.collect_session_telemetry()
        self.assertFalse(out["supported"])
        session_telemetry.start_daemon()
        session_telemetry.stop_daemon()
        src = visualizer_source.Source()
        self.assertEqual((list(src.poll_events()), src.poll_presence(), src.supported), ([], [], False))

    def test_parse_stream_line(self):
        self.assertEqual(streaming.parse_stream_line(b'{"type":"system","subtype":"init","session_id":"s1"}'),
                         {"type": "session_id", "session_id": "s1"})
        delta = b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}}'
        self.assertEqual(streaming.parse_stream_line(delta), {"type": "token", "token": "hi", "partial": True})
        self.assertIsNone(streaming.parse_stream_line(b"not json"))


class RemoteControlTests(_Env):
    def test_disabled_by_default(self):
        self.assertFalse(remote_control.remote_control_enabled())
        self.assertFalse(remote_control.status()["enabled"])
        out = remote_control.start()
        self.assertEqual((out["ok"], out["error"]), (False, "disabled"))
        self.assertEqual(remote_control.stop(), {"ok": True, "running": False})

    def test_enabled_still_needs_a_native_login(self):
        with mock.patch.object(remote_control, "remote_control_enabled", return_value=True), \
             mock.patch.object(remote_control, "native_login_present", return_value=False):
            self.assertEqual(remote_control.start()["error"], "no_native_login")


class SessionIndexTests(_Env):
    def _hooks(self):
        async def validate():
            return fixtures.validation(True)
        return HarnessHooks(validate_candidate=validate, policy=ToolPolicy.for_files(["src/pay.ts"]))

    def _fake_query(self, captured: dict):
        async def fake_query(*, prompt, options):
            captured["options"] = options
            yield sdk.SystemMessage(subtype="init", data={"session_id": options.session_id})
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Working.")], model="m")
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                    session_id=options.session_id, total_cost_usd=0.02,
                                    usage={"input_tokens": 10, "output_tokens": 5})
        return fake_query

    async def test_revise_forks_the_previous_session_when_its_transcript_is_reachable(self):
        adapter_mod.write_preliminary_entry("job_1", "job_1", "/ws", "prev-native")
        job = fixtures.job()
        job.job_id, job.resume_job_id = "job_2", "job_1"
        captured: dict = {}
        with mock.patch.object(sdk, "query", self._fake_query(captured)), \
             mock.patch.object(ClaudeCodeAdapter, "_transcript_available", return_value=True):
            result = await ClaudeCodeAdapter({}).run(job, self.state / "ws", self._hooks())
        opts = captured["options"]
        self.assertEqual((opts.resume, opts.fork_session), ("prev-native", True))
        self.assertNotEqual(opts.session_id, "prev-native")  # the fork gets its own pre-allocated id
        row = sessions_io.read_session_row("job_2")
        self.assertEqual((row["resumedFrom"], row["status"]), ("job_1", "failed_fix"))
        self.assertIn("forked from job_1", result.extra["session"])

    async def test_revise_starts_fresh_when_the_transcript_is_elsewhere(self):
        adapter_mod.write_preliminary_entry("job_1", "job_1", "/elsewhere", "prev-native")
        job = fixtures.job()
        job.job_id, job.resume_job_id = "job_2", "job_1"
        captured: dict = {}
        with mock.patch.object(sdk, "query", self._fake_query(captured)), \
             mock.patch.object(ClaudeCodeAdapter, "_transcript_available", return_value=False):
            result = await ClaudeCodeAdapter({}).run(job, self.state / "ws", self._hooks())
        self.assertIsNone(captured["options"].resume)
        self.assertIsNone(sessions_io.read_session_row("job_2")["resumedFrom"])
        self.assertIn("fresh session", result.extra["session"])

    async def test_a_stream_the_caller_abandons_is_recorded_as_cancelled(self):
        captured: dict = {}
        with mock.patch.object(sdk, "query", self._fake_query(captured)):
            stream = ClaudeCodeAdapter({}).stream(fixtures.job(), self.state / "ws", self._hooks())
            await anext(stream)
            await stream.aclose()
        self.assertEqual(sessions_io.read_session_row("job_1")["status"], "cancelled")

    def test_patch_native_session_id_never_clobbers(self):
        adapter_mod.write_preliminary_entry("job_9", "job_9", "/ws")
        self.assertTrue(adapter_mod._patch_native_session_id("job_9", "n1"))
        self.assertTrue(adapter_mod._patch_native_session_id("job_9", "n1"))
        self.assertFalse(adapter_mod._patch_native_session_id("job_9", "n2"))
        self.assertEqual(adapter_mod.get_native_session_id("job_9"), "n1")
        self.assertEqual(adapter_mod.find_session_key_for_session_id("n1"), "job_9")


class TranscriptTests(_Env):
    def setUp(self):
        super().setUp()
        self.ws = self.state / "workspaces" / "job_1" / "fix"
        self.ws.mkdir(parents=True)
        self.path = self.write_transcript(NATIVE, self.ws, _records(NATIVE))
        adapter_mod.write_preliminary_entry("job_1", "job_1", str(self.ws), NATIVE)

    def test_project_encoding_matches_the_cli_and_reverses_by_longest_job(self):
        (self.state / "workspaces" / "job_1_revise" / "fix").mkdir(parents=True)
        self.assertEqual(_project_encoding.transcript_dir(self.ws), self.path.parent)
        self.assertEqual(_project_encoding.job_id_for_encoded_cwd(self.path.parent.name), "job_1")
        revise = project_key_for_directory(str(self.state / "workspaces" / "job_1_revise" / "fix"))
        self.assertEqual(_project_encoding.job_id_for_encoded_cwd(revise), "job_1_revise")
        self.assertIsNone(_project_encoding.job_id_for_encoded_cwd(project_key_for_directory("/somewhere/else")))

    def test_sessions_capability(self):
        self.assertTrue(sessions.owns_session("job_1"))
        self.assertFalse(sessions.owns_session("job_404"))
        self.assertEqual(sessions.resolve_native_file(sessions_io.read_session_row("job_1"), "job_1"), self.path)
        msgs = sessions.get_messages("job_1")
        self.assertTrue(msgs)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(sessions.set_session_directory("job_1", "/new")["directory"], "/new")
        self.assertEqual(sessions_io.read_session_row("job_1")["directoryHistory"][-1]["directory"], "/new")
        self.assertIsNone(sessions.set_session_directory("job_404", "/x"))

    def test_session_prompts_by_job_id(self):
        out = session_prompts.collect_session_prompts("job_1")
        texts = [p["text"] for p in out["prompts"]]
        self.assertEqual(texts, ["Job job_1: apply cr_test1.",
                                 "finish rejected: independent validation did not pass."])
        self.assertEqual((out["prompts"][0]["responses"], out["prompts"][0]["tool_uses"]), (2, 1))
        with self.assertRaises(FileNotFoundError):
            session_prompts.collect_session_prompts("no-such-native")

    def test_usage(self):
        meta, entries = usage.parse_file(str(self.path))
        assistants = [e for e in entries if e["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)  # deduped by message.id
        self.assertEqual(assistants[0]["usage"]["totalTokens"], 65)
        self.assertEqual(assistants[0]["toolNames"], ["Edit"])
        self.assertEqual(len([e for e in entries if e["role"] == "user"]), 2)  # tool_result-only is not a turn
        adapter_mod._roll_up("job_1", NATIVE, {"input_tokens": 50, "output_tokens": 10}, 0.03, 2, "passed")
        dash = usage.dashboard(window={"days": 3650})
        self.assertEqual((dash["total_sessions"], dash["total_cost"]), (1, 0.03))
        self.assertEqual(dash["by_model"][0]["model"], "claude-opus-5-5")
        self.assertEqual(usage.get_session("job_1")["tools"], {"Edit": 1})
        self.assertEqual(usage.summary_card(window={"days": 3650})["cost"], 0.03)
        self.assertNotIn("job_1", json.dumps(usage.aggregate_for_sync()))  # nothing identifying leaves


class RouteTests(_Env):
    def setUp(self):
        super().setUp()
        from server import app

        self.client = TestClient(app)

    def test_status_routes(self):
        with mock.patch.object(models_status, "fetch_raw_status", mock.AsyncMock(return_value={"loggedIn": True})):
            self.assertEqual(self.client.get("/models/status").json()["models"][0]["status"], "ok")
        boom = CliStatusError("missing", code="binary_not_found")
        with mock.patch.object(models_status, "fetch_raw_status", mock.AsyncMock(side_effect=boom)):
            self.assertEqual(self.client.get("/models/status").status_code, 503)
        self.assertEqual(self.client.get("/channels/status").json(), {"channels": []})
        with mock.patch.object(providers_status, "claude_auth_status", mock.AsyncMock(return_value={})):
            self.assertEqual(self.client.get("/providers/status").json()["agent"], "claude_code")

    def test_agent_models_sessions_usage_and_remote_control_routes(self):
        self.assertEqual(self.client.get("/api/agents/migrator").json()["id"], "migrator")
        self.assertEqual(self.client.get("/api/agents/nobody").status_code, 404)
        self.assertEqual(self.client.post("/api/agents", json={}).status_code, 405)
        self.assertEqual(self.client.get("/api/models").json()[0]["id"], "claude_code/claude-opus-5-5")

        adapter_mod.write_preliminary_entry("job_1", "job_1", "/ws", "native-x")
        self.assertEqual([s["key"] for s in self.client.get("/api/sessions").json()], ["job_1"])
        self.assertEqual(self.client.get("/api/sessions/job_1").json()["nativeSessionId"], "native-x")
        self.assertEqual(self.client.get("/api/sessions/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/messages/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/sessions/job_1/prompts").status_code, 404)  # no transcript yet
        self.assertEqual(self.client.patch("/api/sessions/job_1", json={"directory": "/d"}).json()["directory"], "/d")
        self.assertEqual(self.client.get("/api/usage?days=7").json()["total_sessions"], 1)
        self.assertEqual(self.client.get("/api/usage/sessions/nope").status_code, 404)

        self.assertFalse(self.client.get("/api/remote-control/status").json()["enabled"])
        self.assertEqual(self.client.post("/api/remote-control/start", json={}).status_code, 403)
        self.assertEqual(self.client.post("/api/remote-control/stop").json(), {"ok": True, "running": False})


if __name__ == "__main__":
    unittest.main()
