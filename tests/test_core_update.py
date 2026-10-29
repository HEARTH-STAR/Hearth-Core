import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import app
from services.update import core


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class CoreUpdateTests(unittest.IsolatedAsyncioTestCase):
    """A Space-like checkout: a depth-1 clone of ``main``, then a new commit lands upstream."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.upstream, self.checkout = root / "upstream", root / "hearth-core"
        self.upstream.mkdir()
        _git("init", "-q", "-b", "main", cwd=self.upstream)
        (self.upstream / "server.py").write_text("v1\n", encoding="utf-8")
        _git("add", "-A", cwd=self.upstream)
        _git("commit", "-q", "-m", "first", cwd=self.upstream)
        _git("clone", "-q", "--depth", "1", "--branch", "main", self.upstream.as_uri(), str(self.checkout), cwd=root)
        for name, value in {"REPO": self.checkout, "_latest": None, "_checked_at": None}.items():
            patcher = mock.patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"HEARTH_SELF_UPDATE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.kill = mock.patch.object(core.os, "kill")
        self.killed = self.kill.start()
        self.addCleanup(self.kill.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def push(self, text, message):
        (self.upstream / "server.py").write_text(text, encoding="utf-8")
        _git("commit", "-q", "-am", message, cwd=self.upstream)
        return _git("rev-parse", "HEAD", cwd=self.upstream).strip()

    async def test_version_then_update_restarts(self):
        v = await core.version()
        self.assertEqual((v["branch"], v["update_available"], v["current"]["subject"]), ("main", False, "first"))

        sha = self.push("v2\n", "second")
        self.assertFalse((await core.version())["update_available"])  # cached for 10 minutes
        v = await core.version(refresh=True)
        self.assertEqual((v["update_available"], v["latest"]["sha"], v["latest"]["subject"]), (True, sha, "second"))

        with mock.patch.object(core, "RESTART_DELAY_SECONDS", 0):
            out = await core.update()
            await asyncio.sleep(0.05)
        self.assertEqual((out["updated"], out["to"]["sha"]), (True, sha))
        self.assertEqual((self.checkout / "server.py").read_text(encoding="utf-8"), "v2\n")
        self.killed.assert_called_once()  # SIGTERM to itself: the container's restart policy brings it back

        again = await core.update()
        self.assertEqual((again["updated"], again["restarting"]), (False, False))

    async def test_refusals(self):
        self.push("v2\n", "second")
        with self.assertRaises(core.UpdateError) as e:
            await core.update(busy="An API watch fix is running.")
        self.assertEqual(e.exception.status, 409)

        (self.checkout / "server.py").write_text("local edit\n", encoding="utf-8")
        with self.assertRaises(core.UpdateError) as e:
            await core.update()
        self.assertEqual(e.exception.status, 409)
        self.assertEqual((self.checkout / "server.py").read_text(encoding="utf-8"), "local edit\n")

        with mock.patch.dict(os.environ, {"HEARTH_SELF_UPDATE": "0"}):
            with self.assertRaises(core.UpdateError) as e:
                await core.update()
            self.assertEqual(e.exception.status, 403)
            v = await core.version()
            self.assertEqual((v["enabled"], v["latest"], v["update_available"]), (False, None, False))
        self.killed.assert_not_called()


class CoreUpdateRouteTests(unittest.TestCase):
    def test_route_refuses_off_a_space(self):
        with mock.patch.dict(os.environ, {"HEARTH_SELF_UPDATE": "0"}):
            client = TestClient(app)
            r = client.post("/api/core/update")
            self.assertEqual(r.status_code, 403)
            self.assertIn("Space containers only", r.json()["detail"])
            v = client.get("/api/core/version").json()
            self.assertEqual((v["enabled"], v["update_available"]), (False, False))
            self.assertRegex(v["current"]["sha"], r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
