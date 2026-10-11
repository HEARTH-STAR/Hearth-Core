"""Claude Code sign-in from the web app: output parsing, the routes, and the PTY flow against a fake
``claude`` (POSIX only, like the flow itself). No network, no real CLI."""

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from services.hearth_agent.adapters.claude_code import cli, login
from services.hearth_agent.adapters.cli_status import CliResult, CliStatusError

AUTH_URL = ("https://claude.ai/oauth/authorize?code=true&client_id=abc&response_type=code"
            "&redirect_uri=https%3A%2F%2Fconsole.anthropic.com%2Foauth%2Fcode%2Fcallback&state=xyz")

# Behaves like `claude auth login` on a terminal: prints the link, prompts for the code without a
# newline, rejects a bad code and asks again, exits 0 after a good one.
FAKE_CLAUDE = textwrap.dedent(f"""\
    import os, sys
    if not os.isatty(0):
        print("stdin is not a terminal"); sys.exit(3)
    print("\\x1b[1mBrowser didn't open? Use the url below to sign in\\x1b[0m")
    print("If the browser didn't open, visit: \\x1b[4m{AUTH_URL}\\x1b[0m")
    while True:
        sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if code == "good#xyz":
            print("Login successful."); sys.exit(0)
        if code == "quit":
            print("Login failed: cancelled by test"); sys.exit(1)
        print("OAuth error: Invalid code. Please make sure the full code was copied")
""")


class OutputParsingTests(unittest.TestCase):
    def test_strip_ansi(self):
        self.assertEqual(login.strip_ansi("\x1b[1mhi\x1b[0m\x07 \x1b]8;;x\x1b\\there"), "hi there")

    def test_extract_auth_url(self):
        self.assertEqual(login.extract_auth_url(f"visit: {AUTH_URL}"), AUTH_URL)
        self.assertEqual(login.extract_auth_url(f"visit: {AUTH_URL}{AUTH_URL}"), AUTH_URL)  # redraw glued on
        self.assertEqual(login.extract_auth_url(f"go to {AUTH_URL} now"), AUTH_URL)
        self.assertIsNone(login.extract_auth_url("Opening browser to sign in…"))

    def test_normalize_code(self):
        self.assertEqual(login.normalize_code("  abc#xyz \n"), "abc#xyz")
        self.assertEqual(login.normalize_code("https://console.anthropic.com/oauth/code/callback?code=abc&state=xyz"),
                         "abc#xyz")
        self.assertEqual(login.normalize_code("code=abc&state=xyz"), "abc#xyz")

    def test_describe_never_carries_a_credential(self):
        auth = {"loggedIn": True, "authMethod": "claude.ai", "email": "o@example.com", "orgName": "Acme",
                "subscriptionType": "max", "accessToken": "sk-ant-oat-secret"}
        view = login.describe(auth)
        self.assertEqual(view, {"status": "connected", "method": "claude.ai", "email": "o@example.com",
                                "organization": "Acme", "subscription": "max"})
        self.assertNotIn("secret", json.dumps(view))
        self.assertEqual(login.describe({"loggedIn": False}), {"status": "needs_auth"})
        self.assertEqual(login.describe({"loggedIn": True, "apiKeySource": "ANTHROPIC_API_KEY"})["method"],
                         "api_key")


class ClaudeBinaryTests(unittest.TestCase):
    def test_override_then_bundled_then_path(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": "/opt/claude"}):
            self.assertEqual(cli.claude_binary(), "/opt/claude")
        with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": ""}):
            with mock.patch.object(cli, "bundled_cli", return_value=Path("/sdk/_bundled/claude")):
                self.assertEqual(cli.claude_binary(), str(Path("/sdk/_bundled/claude")))
            with mock.patch.object(cli, "bundled_cli", return_value=None), \
                    mock.patch.object(cli.shutil, "which", return_value=None):
                self.assertEqual(cli.claude_binary(), "claude")


class RouteTests(unittest.TestCase):
    def setUp(self):
        from server import app

        self.client = TestClient(app)

    def test_status_uses_the_turns_env(self):
        result = CliResult(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "o@example.com"}), "")
        with mock.patch.object(login, "run_cli", mock.AsyncMock(return_value=result)) as run:
            body = self.client.get("/api/connectors/claude-code/status").json()
        self.assertEqual((body["status"], body["email"]), ("connected", "o@example.com"))
        self.assertEqual(run.call_args.args[1], ["auth", "status", "--json"])
        self.assertIsInstance(run.call_args.kwargs["env"], dict)

    def test_status_when_the_cli_is_missing(self):
        boom = CliStatusError("claude binary unavailable: claude", code="binary_not_found")
        with mock.patch.object(login, "run_cli", mock.AsyncMock(side_effect=boom)):
            self.assertEqual(self.client.get("/api/connectors/claude-code/status").json()["status"], "unavailable")

    def test_start_refuses_without_a_pty(self):
        with mock.patch.object(login, "HAS_PTY", False):
            response = self.client.post("/api/connectors/claude-code/login/start")
        self.assertEqual(response.status_code, 400)
        self.assertIn("inside a Hearth space", response.json()["detail"])

    def test_unknown_sessions_and_bad_bodies(self):
        self.assertEqual(self.client.post("/api/connectors/claude-code/login/poll",
                                          json={"session_id": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/api/connectors/claude-code/login/code",
                                          json={"session_id": "nope", "code": "a#b"}).status_code, 404)
        self.assertEqual(self.client.post("/api/connectors/claude-code/login/code",
                                          json={"session_id": "nope"}).status_code, 422)
        self.assertEqual(self.client.post("/api/connectors/claude-code/login/cancel",
                                          json={"session_id": "nope"}).json()["status"], "cancelled")

    def test_logout_drops_env_tokens_then_reports_status(self):
        results = [CliResult(0, "", ""), CliResult(0, json.dumps({"loggedIn": False}), "")]
        with mock.patch.object(login, "run_cli", mock.AsyncMock(side_effect=results)) as run:
            body = self.client.post("/api/connectors/claude-code/logout").json()
        self.assertEqual(body["status"], "needs_auth")
        self.assertEqual(run.call_args_list[0].args[1], ["auth", "logout"])
        self.assertEqual(run.call_args_list[0].kwargs["env"], {key: "" for key in login.TOKEN_VARS})


@unittest.skipIf(sys.platform == "win32", "the sign-in runs under a PTY, inside a Linux space")
class PtyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        binary = self.tmp / "claude"
        binary.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}", encoding="utf-8")
        binary.chmod(0o755)
        self.lines: list[str] = []
        original = login._on_output

        def record(session, text):
            self.lines.append(text)
            original(session, text)

        self.patches = [
            mock.patch.object(login, "claude_binary", return_value=str(binary)),
            mock.patch.object(login, "_lock", asyncio.Lock()),  # fresh lock per event loop
            mock.patch.object(login, "_active", None),
            mock.patch.object(login, "_on_output", record),
            mock.patch.dict(os.environ, {"HOME": str(self.tmp), "ANTHROPIC_API_KEY": "sk-ant-api-test"}),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        if login._active is not None:
            await login.cancel(login._active.id)
        for p in reversed(self.patches):
            p.stop()

    async def test_link_then_bad_code_then_good_code(self):
        started = await login.start()
        self.assertEqual(started["auth_url"], AUTH_URL)
        sid = started["session_id"]
        self.assertEqual((await login.poll(sid))["status"], "pending")

        rejected = await login.submit(sid, "bad#xyz")
        self.assertEqual(rejected["status"], "invalid_code")
        self.assertIn("Invalid code", rejected["error"])

        accepted = await login.submit(sid, "https://console.anthropic.com/oauth/code/callback?code=good&state=xyz")
        self.assertEqual(accepted["status"], "connected")
        for _ in range(50):  # the exit and the onboarding write land just after
            if sid in login._finished and (self.tmp / ".claude.json").exists():
                break
            await asyncio.sleep(0.05)
        self.assertEqual((await login.poll(sid))["status"], "connected")
        self.assertTrue(json.loads((self.tmp / ".claude.json").read_text())["hasCompletedOnboarding"])
        self.assertFalse(any("good#xyz" in line or "bad#xyz" in line for line in self.lines))  # no echo

    async def test_a_failed_login_and_a_cancelled_one(self):
        sid = (await login.start())["session_id"]
        failed = await login.submit(sid, "quit")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Login failed", failed["error"])

        sid = (await login.start())["session_id"]
        process = login._active.process
        self.assertEqual((await login.cancel(sid))["status"], "cancelled")
        await asyncio.wait_for(process.wait(), 5)
        for _ in range(50):
            if sid in login._finished:
                break
            await asyncio.sleep(0.05)
        self.assertEqual((await login.poll(sid))["status"], "cancelled")
        self.assertEqual((await login.submit(sid, "good#xyz"))["status"], "cancelled")
        with self.assertRaises(login.UnknownLogin):
            await login.poll("never-started")

    async def test_env_tokens_do_not_reach_the_login(self):
        self.assertNotIn("ANTHROPIC_API_KEY", login._login_env())
        self.assertEqual(login._login_env()["TERM"], os.environ.get("TERM", "xterm-256color"))


if __name__ == "__main__":
    unittest.main()
