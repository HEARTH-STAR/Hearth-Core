"""The space workspace: path safety, listings and file reads, directly and over HTTP."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import app
from services.storage import workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ws"
        self.env = mock.patch.dict(os.environ, {"HEARTH_WORKSPACE_DIR": str(self.root),
                                                "HEARTH_STATE_DIR": self.tmp.name})
        self.env.start()
        scout = self.root / "dots" / "scout"
        (scout / "notes").mkdir(parents=True)
        (scout / "notes" / "acme.md").write_text("# Acme\nShipped a runtime.\n", encoding="utf-8")
        (scout / "empty").mkdir()
        (scout / ".git").mkdir()
        (scout / ".git" / "HEAD").write_text("ref: main", encoding="utf-8")
        (scout / "node_modules" / "x").mkdir(parents=True)
        (scout / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
        (Path(self.tmp.name) / "secret.txt").write_text("outside", encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_paths_stay_inside_the_root(self):
        for bad in ["../secret.txt", "/etc/passwd", "dots\\scout", "dots/../../secret.txt"]:
            with self.assertRaises(workspace.WorkspacePathError, msg=bad):
                workspace.resolve(bad)
        self.assertEqual(workspace.resolve(""), self.root.resolve())

    @unittest.skipIf(os.name == "nt", "creating symlinks needs extra rights on Windows")
    def test_symlink_out_of_the_root_is_refused(self):
        (self.root / "dots" / "scout" / "leak").symlink_to(Path(self.tmp.name) / "secret.txt")
        with self.assertRaises(workspace.WorkspacePathError):
            workspace.read_file("dots/scout/leak")

    def test_listing(self):
        out = workspace.list_files("dots/scout")
        self.assertEqual(out["directory"], "dots/scout")
        self.assertTrue(out["exists"])
        self.assertEqual(out["files"], ["logo.png", "empty/", "notes/acme.md"])
        self.assertFalse(out["truncated"])
        self.assertEqual(workspace.list_files("dots/scout", limit=1)["truncated"], True)
        self.assertEqual(workspace.list_files("dots/quill"), {"directory": "dots/quill", "exists": False,
                                                              "files": [], "truncated": False})

    def test_reads(self):
        text = workspace.read_file("dots/scout/notes/acme.md")
        self.assertEqual((text["binary"], text["truncated"]), (False, False))
        self.assertTrue(text["text"].startswith("# Acme"))
        self.assertEqual(workspace.read_file("dots/scout/notes/acme.md", limit=4)["text"], "# Ac")
        binary = workspace.read_file("dots/scout/logo.png")
        self.assertEqual((binary["binary"], binary["text"]), (True, None))
        with self.assertRaises(FileNotFoundError):
            workspace.read_file("dots/scout/missing.md")

    def test_routes(self):
        client = TestClient(app)
        files = client.get("/api/workspace/files", params={"directory": "dots/scout"}).json()
        self.assertIn("notes/acme.md", files["files"])
        self.assertEqual(client.get("/api/workspace/files", params={"directory": "../"}).status_code, 400)
        read = client.get("/api/workspace/file", params={"path": "dots/scout/notes/acme.md"}).json()
        self.assertEqual(read["path"], "dots/scout/notes/acme.md")
        self.assertEqual(client.get("/api/workspace/file", params={"path": "dots/nope.md"}).status_code, 404)
        self.assertEqual(client.get("/api/workspace/file", params={"path": "../secret.txt"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
