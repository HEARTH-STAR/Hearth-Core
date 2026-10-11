import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from server import app


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": self.tmp.name})
        self.env.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_health_and_agents(self):
        self.assertEqual(self.client.get("/health").json()["agent"], "claude_code")
        runtime = self.client.get("/api/agents/runtime").json()
        self.assertEqual((runtime["active"], runtime["adapters"]), ("claude_code", ["claude_code"]))
        agents = self.client.get("/api/agents").json()
        self.assertEqual([a["name"] for a in agents], ["migrator"])
        caps = self.client.get("/api/agents/capabilities").json()
        self.assertTrue(caps["data"]["github"]["enabled"])

    def test_apidiff_route(self):
        body = {"language": "python", "module": "sdk",
                "old": "def a(x: int) -> int: ...\n", "new": "def a(x: int, y: int) -> int: ...\n",
                "provider": "acme", "package": {"ecosystem": "pypi", "name": "acme", "from": "1", "to": "2"}}
        out = self.client.post("/api/apidiff", json=body).json()
        self.assertEqual(out["breaking"], 1)
        self.assertEqual(out["changes"][0]["type"], "param_added_required")
        self.assertEqual(out["change_record"]["kind"], "breaking")

    def test_github_token_route_rejects_garbage(self):
        self.assertEqual(self.client.post("/api/connectors/github/token", json={"token": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/api/connectors/github/token", json={"token": "  "}).status_code, 400)


if __name__ == "__main__":
    unittest.main()


class EventLoopTests(unittest.TestCase):
    def test_windows_keeps_a_loop_that_can_start_subprocesses(self):
        from server import event_loop

        self.assertEqual(event_loop("linux"), "auto")
        self.assertEqual(event_loop("win32"), "asyncio:ProactorEventLoop")

    @unittest.skipUnless(sys.platform == "win32", "the proactor loop exists only on Windows")
    def test_uvicorn_builds_the_proactor_loop_even_with_reload(self):
        import asyncio

        import uvicorn

        from server import event_loop

        config = uvicorn.Config("server:app", reload=True, loop=event_loop())
        loop = config.get_loop_factory()()
        try:
            self.assertIsInstance(loop, asyncio.ProactorEventLoop)
        finally:
            loop.close()
