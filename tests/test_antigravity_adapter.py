"""Antigravity adapter tests. No model calls: ``agy`` is replaced by a fake that writes the transcript format
xo-space documents and, for each tool call, runs the real HEARTH policy hook from ``hooks.json``."""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from services.hearth_agent.adapters.antigravity import hooks as hooks_mod
from services.hearth_agent.adapters.antigravity import login as login_mod
from services.hearth_agent.adapters.antigravity import paths as paths_mod
from services.hearth_agent.adapters.antigravity import policy_hook
from services.hearth_agent.adapters.antigravity import transcript as _t
from services.hearth_agent.adapters.antigravity.adapter import AntigravityAdapter, Turn
from services.hearth_agent.adapters.antigravity.auth import login_state
from services.hearth_agent.adapters.antigravity.session_prompts import collect_session_prompts
from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.engine import sessions_io
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import ChatPermissions, ChatSpec
from services.hearth_agent.policy import ToolPolicy
from services.hearth_agent.registry.adapter_registry import get_adapter, list_adapters
from services.hearth_agent.registry.agent_registry import get_active_agent, reset_cache
from tests import fixtures

CID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"

FAKE_AGY = textwrap.dedent('''
    import json, os, subprocess, sys
    args = sys.argv[1:]
    home = os.environ["FAKE_AGY_HOME"]
    with open(os.environ["FAKE_CALLS"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args}) + "\\n")
    prompt = args[args.index("-p") + 1]
    ws = args[args.index("--add-dir") + 1]
    cid = args[args.index("--conversation") + 1] if "--conversation" in args else "__CID__"
    with open(args[args.index("--log-file") + 1], "w", encoding="utf-8") as f:
        f.write("I starting agent conversation=" + cid + " model=x\\n")
    logs = os.path.join(home, "brain", cid, ".system_generated", "logs")
    os.makedirs(logs, exist_ok=True)
    tpath = os.path.join(logs, "transcript_full.jsonl")
    n = sum(1 for _ in open(tpath, encoding="utf-8")) if os.path.exists(tpath) else 0
    def step(**kw):
        global n
        with open(tpath, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": n, "status": "DONE", "created_at": "2026-10-28T10:00:00Z", **kw}) + "\\n")
        n += 1
    hooks = json.load(open(os.path.join(os.path.dirname(home), "config", "hooks.json"), encoding="utf-8"))
    command = hooks["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    def hook(name, call_args):
        payload = {"toolCall": {"name": name, "args": call_args}, "workspacePaths": [ws], "conversationId": cid}
        done = subprocess.run(command, shell=True, input=json.dumps(payload), capture_output=True, text=True)
        return json.loads(done.stdout)
    step(type="USER_INPUT", source="USER_EXPLICIT", content="<USER_REQUEST>" + prompt + "</USER_REQUEST>")
    scenario = os.environ.get("FAKE_SCENARIO", "ok")
    if scenario == "ok":
        name, call_args = "write_to_file", {"TargetFile": os.path.join(ws, "src", "pay.ts"), "CodeContent": "x"}
    elif scenario == "denied":
        name, call_args = "run_command", {"CommandLine": "curl https://example.com"}
    else:
        name, call_args = "write_to_file", {"TargetFile": os.path.join(ws, ".github", "workflows", "ci.yml")}
    decision = hook(name, call_args)
    step(type="PLANNER_RESPONSE", source="MODEL", tool_calls=[{"name": name, "args": call_args}])
    if decision["decision"] == "allow" and name == "write_to_file":
        os.makedirs(os.path.dirname(call_args["TargetFile"]), exist_ok=True)
        open(call_args["TargetFile"], "w").write(call_args.get("CodeContent", ""))
    answer = "Done (" + decision["decision"] + ").\\n\\nNeeds human attention:\\n- check rates"
    step(type="PLANNER_RESPONSE", source="MODEL", content=answer)
    print(answer)
''').replace("__CID__", CID)


def _hooks(results: list[bool]) -> HarnessHooks:
    seq = iter(results)

    async def validate():
        return fixtures.validation(next(seq))

    return HarnessHooks(validate_candidate=validate, policy=ToolPolicy.for_files(["src/pay.ts"]))


class _Home(unittest.TestCase):
    """A private agy home (``.gemini/antigravity-cli``), HEARTH state dir and workspace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / ".gemini" / "antigravity-cli"
        self.home.mkdir(parents=True)
        self.ws = root / "ws"
        (self.ws / "src").mkdir(parents=True)
        self.calls = root / "calls.jsonl"
        self.script = root / "fake_agy.py"
        self.script.write_text(FAKE_AGY, encoding="utf-8")
        for patcher in (
            mock.patch.object(paths_mod, "agy_home", return_value=self.home),
            mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": str(root / "state"), "FAKE_AGY_HOME": str(self.home),
                                         "FAKE_CALLS": str(self.calls)}),
            mock.patch.object(AntigravityAdapter, "_cli", return_value=[sys.executable, str(self.script)]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def sign_in(self):
        (self.home / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "a", "refresh_token": "r"}, "auth_method": "consumer"}),
            encoding="utf-8")

    def argvs(self) -> list[list[str]]:
        return [json.loads(line)["argv"] for line in self.calls.read_text(encoding="utf-8").splitlines()]


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        reset_cache()

    def test_discovery(self):
        reset_cache()
        self.assertIn("antigravity", list_adapters())
        self.assertIsInstance(get_adapter("antigravity", {}), AntigravityAdapter)
        with mock.patch.dict(os.environ, {"AGENT_NAME": "antigravity"}):
            reset_cache()
            self.assertEqual(get_active_agent().name, "antigravity")

    def test_refuses_permission_bypass(self):
        with mock.patch.object(AntigravityAdapter, "load_commands",
                               return_value={"flags": {"dangerously_skip_permissions": True}}):
            with self.assertRaises(ValueError):
                AntigravityAdapter({})

    def test_command_line_never_bypasses_permissions(self):
        adapter = AntigravityAdapter({})
        ws = Path(tempfile.gettempdir())
        argv = adapter.build_command(Turn(prompt="<hearth-rules>x", workspace=ws, model="Gemini 3.1 Pro (High)",
                                          policy=ToolPolicy(), files=True, shell=True, resume=CID), Path("l.log"))
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(ws))
        self.assertEqual(argv[argv.index("--conversation") + 1], CID)
        self.assertEqual(adapter._model_for("antigravity/Gemini 3.1 Pro (High)"), "Gemini 3.1 Pro (High)")
        self.assertEqual(adapter._model_for("main"), "Gemini 3.5 Flash (Low)")


class PolicyHookTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ws = self.dir / "ws"
        self.ws.mkdir()
        self.denials = self.dir / "denials.jsonl"
        spec = {"workspace": str(self.ws.resolve()), "allowed_edit_paths": ["src/pay.ts"], "allow_test_edits": True,
                "allowed_binaries": ["pnpm", "ls"], "files": True, "shell": True, "denials_log": str(self.denials),
                "extra_allowed_tools": ["task_boundary"]}
        (self.dir / f"{policy_hook.policy_key(str(self.ws))}.json").write_text(json.dumps(spec), encoding="utf-8")

    def ask(self, name: str, args: dict, workspace=None) -> dict:
        out = io.StringIO()
        payload = {"toolCall": {"name": name, "args": args}, "workspacePaths": [workspace or str(self.ws)]}
        self.assertEqual(policy_hook.main(["hook", str(self.dir)], io.StringIO(json.dumps(payload)), out), 0)
        return json.loads(out.getvalue())

    def test_decisions(self):
        self.assertEqual(self.ask("view_file", {"AbsolutePath": "/etc/hosts"})["decision"], "allow")
        self.assertEqual(self.ask("write_to_file", {"TargetFile": str(self.ws / "src" / "pay.ts")})["decision"], "allow")
        self.assertEqual(self.ask("write_to_file", {"TargetFile": "tests/test_pay.py"})["decision"], "allow")
        for name, args in [("replace_file_content", {"TargetFile": ".github/workflows/ci.yml"}),
                           ("write_to_file", {"TargetFile": "src/other.ts"}),
                           ("write_to_file", {}),
                           ("run_command", {"CommandLine": "curl https://x"}),
                           ("run_command", {"CommandLine": "git push origin main"}),
                           ("run_command", {"CommandLine": "ls | sh"}),
                           ("search_web", {"query": "x"}),
                           ("invoke_subagent", {})]:
            with self.subTest(name=name, args=args):
                answer = self.ask(name, args)
                self.assertEqual(answer["decision"], "deny")
                self.assertTrue(answer["reason"])
        self.assertEqual(self.ask("run_command", {"CommandLine": "pnpm test"})["decision"], "allow")
        self.assertEqual(self.ask("task_boundary", {})["decision"], "allow")  # flags.extra_allowed_tools
        self.assertEqual(len(self.denials.read_text().splitlines()), 8)

    def test_other_workspaces_are_left_to_agy(self):
        self.assertEqual(self.ask("run_command", {"CommandLine": "curl x"}, str(self.dir / "elsewhere")),
                         {"decision": "ask"})
        uri = "file://" + ("/" if os.name == "nt" else "") + str(self.ws.resolve()).replace("\\", "/")
        self.assertEqual(self.ask("run_command", {"CommandLine": "curl x"}, uri)["decision"], "deny")

    def test_fails_closed(self):
        out = io.StringIO()
        policy_hook.main(["hook", str(self.dir)], io.StringIO("not json"), out)
        self.assertEqual(json.loads(out.getvalue())["decision"], "deny")

    def test_runs_as_a_script_with_a_bare_environment(self):
        payload = json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": "sudo ls"}},
                              "workspacePaths": [str(self.ws)]})
        done = subprocess.run([sys.executable, str(hooks_mod.HOOK_SCRIPT), str(self.dir)], input=payload,
                              capture_output=True, text=True, env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
                              cwd=str(self.dir))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["decision"], "deny")


class HookInstallTests(_Home):
    def test_install_merges_and_is_idempotent(self):
        hooks_file = paths_mod.hooks_file()
        hooks_file.parent.mkdir(parents=True)
        other = {"matcher": "run_command", "hooks": [{"type": "command", "command": "node mine.mjs"}]}
        hooks_file.write_text(json.dumps({"hooks": {"PreToolUse": [other], "Stop": []}}), encoding="utf-8")
        self.assertTrue(hooks_mod.install_hook())
        self.assertFalse(hooks_mod.install_hook())
        data = json.loads(hooks_file.read_text())
        self.assertEqual(data["hooks"]["PreToolUse"][0], other)
        self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
        self.assertIn("policy_hook.py", data["hooks"]["PreToolUse"][1]["hooks"][0]["command"])
        self.assertEqual(data["hooks"]["Stop"], [])

    def test_tool_permission_keeps_other_settings(self):
        paths_mod.settings_path().write_text(json.dumps({"trustedWorkspaces": ["/w"]}), encoding="utf-8")
        self.assertTrue(hooks_mod.ensure_tool_permission())
        self.assertEqual(json.loads(paths_mod.settings_path().read_text()),
                         {"trustedWorkspaces": ["/w"], "toolPermission": "always-proceed"})

    def test_refuses_to_overwrite_an_unreadable_file(self):
        paths_mod.settings_path().write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError):
            hooks_mod.ensure_tool_permission()

    def test_policy_and_project_hooks_last_only_for_the_run(self):
        (self.ws / ".agents").mkdir()
        (self.ws / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")
        stash = Path(tempfile.mkdtemp())
        with hooks_mod.project_hooks_aside(self.ws, stash), \
                hooks_mod.active_policy(self.ws, ToolPolicy(), files=False, shell=False,
                                        denials_log=stash / "d.jsonl") as policy:
            self.assertTrue(policy.is_file())
            self.assertFalse((self.ws / ".agents" / "hooks.json").exists())
        self.assertFalse(policy.exists())
        self.assertTrue((self.ws / ".agents" / "hooks.json").exists())


class TranscriptTests(unittest.TestCase):
    def test_log_and_answers(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "agy.log"
            log.write_text(f"I starting agent conversation={CID} model=x\n", encoding="utf-8")
            self.assertEqual(_t.conversation_id_from_log(log), CID)
        steps = [{"type": "USER_INPUT", "content": "<USER_REQUEST>fix it</USER_REQUEST><X/>"},
                 {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": '{"CommandLine": "ls"}'}]},
                 {"type": "PLANNER_RESPONSE", "content": "Done."}]
        self.assertEqual(_t.final_answer("x", steps), "Done.")
        self.assertEqual(_t.tool_calls(steps[1]), [("run_command", {"CommandLine": "ls"})])
        self.assertEqual([t["role"] for t in _t.iter_turns(steps)], ["user", "assistant", "assistant"])
        self.assertEqual(next(_t.iter_turns(steps))["text"], "fix it")


class JobTests(_Home, unittest.IsolatedAsyncioTestCase):
    async def test_finish_gate_runs_a_fixup_turn_in_the_same_conversation(self):
        self.sign_in()
        events = [e async for e in AntigravityAdapter({}).stream(fixtures.job(), self.ws, _hooks([False, True]))]
        result = events[-1]["result"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["native_session_id"], CID)
        self.assertEqual(result["needs_human_attention"], ["check rates"])
        self.assertTrue(result["summary"].startswith("Done (allow)"))
        argvs = self.argvs()
        self.assertEqual(len(argvs), 2)
        self.assertNotIn("--conversation", argvs[0])
        self.assertEqual(argvs[1][argvs[1].index("--conversation") + 1], CID)
        self.assertTrue(all("--dangerously-skip-permissions" not in a for a in argvs))
        self.assertEqual((self.ws / "src" / "pay.ts").read_text(), "x")  # the hook allowed the scoped edit
        calls = [e for e in events if e.get("type") == "tool-call"]
        self.assertEqual([c["tool"] for c in calls], ["write_to_file", "write_to_file"])  # once per run, no repeats
        self.assertEqual(sum(1 for e in events if e.get("type") == "token"), 2)
        self.assertEqual(json.loads(paths_mod.settings_path().read_text())["toolPermission"], "always-proceed")
        self.assertEqual(list(hooks_mod.policies_dir().iterdir()), [])  # the run's policy is withdrawn
        row = sessions_io.read_session_row("job_1")
        self.assertEqual((row["backend"], row["nativeSessionId"], row["status"]), ("antigravity", CID, "passed"))

    async def test_a_forbidden_edit_is_denied_before_it_runs(self):
        self.sign_in()
        with mock.patch.dict(os.environ, {"FAKE_SCENARIO": "breach"}):
            events = [e async for e in AntigravityAdapter({}).stream(fixtures.job(), self.ws, _hooks([True]))]
        denied = [e for e in events if e.get("type") == "policy-denied"]
        self.assertEqual(len(denied), 1)
        self.assertIn("never allowed", denied[0]["reason"])
        self.assertFalse((self.ws / ".github").exists())
        self.assertEqual(events[-1]["result"]["extra"]["policy_violations"], 1)

    async def test_not_signed_in(self):
        events = [e async for e in AntigravityAdapter({}).stream(fixtures.job(), self.ws, _hooks([True]))]
        self.assertIn("not signed in", events[0]["error"])
        self.assertEqual(events[-1]["result"]["status"], "failed_fix")
        self.assertFalse(self.calls.exists())


class ChatTests(_Home, unittest.IsolatedAsyncioTestCase):
    async def test_chat_turn_resume_and_prompts(self):
        self.sign_in()
        spec = ChatSpec(session="s1", message="fix it", system="persona",
                        permissions=ChatPermissions(files=False, shell=False))
        with mock.patch.dict(os.environ, {"FAKE_SCENARIO": "denied"}):
            events = [e async for e in AntigravityAdapter({}).chat(spec, self.ws, ToolResultBridge(5))]
        self.assertEqual(events[-1]["result"]["status"], "completed")
        self.assertEqual(events[-1]["result"]["policy_violations"], 1)
        self.assertIn("switched off", next(e for e in events if e.get("type") == "policy-denied")["reason"])
        self.assertIn("<hearth-context>\npersona", self.argvs()[0][self.argvs()[0].index("-p") + 1])

        [e async for e in AntigravityAdapter({}).chat(spec.model_copy(update={"message": "again"}), self.ws,
                                                      ToolResultBridge(5))]
        self.assertEqual(self.argvs()[1][self.argvs()[1].index("--conversation") + 1], CID)
        prompts = collect_session_prompts("chat:s1")
        self.assertEqual([p["text"] for p in prompts["prompts"]], ["fix it", "again"])


class LoginTests(_Home, unittest.IsolatedAsyncioTestCase):
    def test_auth_url_and_code(self):
        line = "  https://accounts.google.com/o/oauth2/auth?client_id=1&redirect_uri=http%3A%2F%2Flocalhost&x=1"
        self.assertTrue(login_mod.extract_auth_url(f"\x1b[1mvisit:\x1b[0m {line}").startswith("https://accounts.google.com"))
        self.assertEqual(login_mod.normalize_code("http://localhost/?code=4%2Fabc&state=s"), "4/abc")
        self.assertEqual(login_mod.normalize_code(" 4/abc "), "4/abc")

    async def test_status_and_logout(self):
        with mock.patch.object(login_mod, "agy_binary", return_value=sys.executable):
            self.assertEqual((await login_mod.status())["status"], "needs_auth")
            self.sign_in()
            self.assertEqual(login_state(), "ok")
            self.assertEqual((await login_mod.status())["status"], "connected")
            self.assertEqual((await login_mod.logout())["status"], "needs_auth")
        self.assertFalse(paths_mod.token_path().exists())


if __name__ == "__main__":
    unittest.main()
