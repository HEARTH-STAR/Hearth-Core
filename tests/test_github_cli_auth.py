"""`gh auth login` device-flow tests against a fake gh (a Python one-liner); no network."""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from services.hearth_agent.connectors import token_store
from services.hearth_agent.connectors.github import cli_auth
from services.hearth_agent.connectors.github.gh_api import GhAccount, GhResult

# Prints the device code the way gh 2.x does in non-TTY mode, then "waits for the
# user" and exits with the given status. argv: <seconds> <exit code> [print code?]
FAKE_GH = """
import sys, time
if len(sys.argv) < 4 or sys.argv[3] == "1":
    print("! First copy your one-time code: ABCD-1234", flush=True)
    print("Open this URL to continue in your web browser: https://github.com/login/device", flush=True)
time.sleep(float(sys.argv[1]))
sys.exit(int(sys.argv[2]))
"""

SECRET = "gho_" + "s" * 36
VALID = {"valid": True, "status": "connected", "username": "hearth-bot", "token_kind": "oauth",
         "expires_at": None, "warnings": []}


def fake_login(seconds: float = 0.3, exit_code: int = 0, prints_code: bool = True):
    return lambda: [sys.executable, "-c", FAKE_GH, str(seconds), str(exit_code), "1" if prints_code else "0"]


class DeviceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": self.tmp.name}),
            mock.patch.object(cli_auth, "_lock", asyncio.Lock()),  # fresh lock per event loop
            mock.patch.object(cli_auth, "_gh_available", return_value=True),
            mock.patch.object(cli_auth, "configure_git_identity", mock.AsyncMock()),
        ]
        for p in self.patches:
            p.start()
        cli_auth._active.clear()
        self.gh = mock.AsyncMock()
        self.gh.logout.return_value = GhResult(True)
        self.gh.auth_token.return_value = SECRET
        self.gh.setup_git.return_value = GhResult(True)
        self.gh.accounts.return_value = [GhAccount("hearth-bot", True, "success")]
        self.patches.append(mock.patch.object(cli_auth, "_gh", return_value=self.gh))
        self.patches[-1].start()

    async def asyncTearDown(self):
        for sid in list(cli_auth._active):
            await cli_auth.cancel_login(sid)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def _wait_terminal(self, sid: str) -> dict:
        for _ in range(100):
            out = await cli_auth.connect(sid)
            if out.get("status") != "pending":
                return out
            await asyncio.sleep(0.05)
        self.fail("login never left pending")

    async def test_success_records_metadata_but_never_the_token(self):
        with mock.patch.object(cli_auth, "_login_argv", fake_login()), \
             mock.patch.object(cli_auth, "validate_token", mock.AsyncMock(return_value=dict(VALID))) as validate:
            info = await cli_auth.start_login()
            self.assertEqual(info["user_code"], "ABCD-1234")
            self.assertEqual(info["verification_uri"], cli_auth.VERIFICATION_URI)
            self.assertEqual((await cli_auth.connect(info["session_id"]))["status"], "pending")
            out = await self._wait_terminal(info["session_id"])

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["payload"]["auth_method"], "cli")
        self.assertIn(cli_auth.DEVICE_FLOW_WARNING, out["payload"]["warnings"])
        validate.assert_awaited_once_with(SECRET)
        self.gh.setup_git.assert_awaited_once()
        stored = token_store.token_file().read_text(encoding="utf-8")
        self.assertIn('"auth_method": "cli"', stored)
        self.assertNotIn(SECRET, stored)  # gh holds the token; HEARTH never stores it
        self.assertEqual(await cli_auth.poll_login(info["session_id"]), {"status": "not_found"})  # consumed once

    async def test_one_login_at_a_time_and_cancel(self):
        with mock.patch.object(cli_auth, "_login_argv", fake_login(seconds=30)):
            info = await cli_auth.start_login()
            with self.assertRaises(RuntimeError):
                await cli_auth.start_login()
            self.assertEqual(await cli_auth.cancel_login(info["session_id"]), {"status": "cancelled"})
            self.assertEqual(await cli_auth.cancel_login(info["session_id"]), {"status": "not_found"})
            self.assertEqual((await cli_auth.connect(info["session_id"]))["status"], "not_found")

    async def test_invalid_token_is_logged_out_again(self):
        bad = {"valid": False, "status": "failed", "error": "Token has admin scopes HEARTH must not hold: admin:org."}
        with mock.patch.object(cli_auth, "_login_argv", fake_login()), \
             mock.patch.object(cli_auth, "validate_token", mock.AsyncMock(return_value=bad)):
            info = await cli_auth.start_login()
            out = await self._wait_terminal(info["session_id"])
        self.assertFalse(out["ok"])
        self.assertIn("admin", out["error"])
        self.assertEqual(self.gh.logout.await_count, 2)  # before the flow, and to drop the bad credential
        self.assertFalse(token_store.token_file().exists())

    async def test_gh_failure_is_reported(self):
        with mock.patch.object(cli_auth, "_login_argv", fake_login(exit_code=1)):
            info = await cli_auth.start_login()
            out = await self._wait_terminal(info["session_id"])
        self.assertEqual((out["ok"], out["status"]), (False, "failed"))
        self.assertIn("status 1", out["error"])

    async def test_no_device_code(self):
        with mock.patch.object(cli_auth, "_login_argv", fake_login(seconds=0, prints_code=False)):
            with self.assertRaises(RuntimeError):
                await cli_auth.start_login()
        self.assertEqual(cli_auth._active, {})


class LoginArgvTests(unittest.TestCase):
    def test_uses_only_flags_the_space_images_gh_has(self):
        # gh 2.46 (Ubuntu's, in the space image) exits with "unknown flag" on anything newer, before
        # it prints a device code; --skip-ssh-key (gh 2.49+) once broke the sign-in that way.
        gh_246_login_flags = {"--web", "--hostname", "--git-protocol", "--insecure-storage", "--scopes",
                              "--with-token"}
        argv = cli_auth._login_argv()
        self.assertEqual(argv[:3], [cli_auth.GH_BIN, "auth", "login"])
        self.assertLessEqual({a for a in argv if a.startswith("--")}, gh_246_login_flags)
        self.assertEqual(argv[argv.index("--git-protocol") + 1], "https")  # no SSH key step to skip


class DeviceFlowRouteTests(unittest.TestCase):
    def setUp(self):
        from server import app

        self.client = TestClient(app)

    def test_start_without_gh_is_400(self):
        with mock.patch.object(cli_auth, "_gh_available", return_value=False):
            r = self.client.post("/api/connectors/github/cli/start")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not installed", r.json()["detail"])

    def test_poll_unknown_session_is_404(self):
        r = self.client.post("/api/connectors/github/cli/poll", json={"session_id": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_cancel_unknown_session(self):
        r = self.client.post("/api/connectors/github/cli/cancel", json={"session_id": "nope"})
        self.assertEqual(r.json(), {"status": "not_found"})


if __name__ == "__main__":
    unittest.main()
