import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import app
from services.hearth_agent.connectors.github.repo import RepoMirror
from services.hearth_agent.watch import browse
from services.hearth_agent.watch.models import Watch
from services.hearth_agent.watch.store import get_store


def _git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Rose", "GIT_AUTHOR_EMAIL": "r@x", "GIT_COMMITTER_NAME": "Rose",
           "GIT_COMMITTER_EMAIL": "r@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class RepoBrowseTests(unittest.TestCase):
    """A small repo: two commits on main (the second renames, deletes and edits), plus a feature branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": str(root / "state"), "HEARTH_API_WATCH": "0"})
        self.env.start()
        work = root / "work"
        (work / "src").mkdir(parents=True)
        (work / "README.md").write_text("# Shop\n", encoding="utf-8")
        (work / "src" / "pay.js").write_text("charge({ source })\n", encoding="utf-8")
        (work / "old.txt").write_text("bye\n", encoding="utf-8")
        (work / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
        _git("init", "-q", "-b", "main", cwd=work)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "First", cwd=work)
        (work / "src" / "pay.js").write_text("charge({ amount })\n", encoding="utf-8")
        (work / "old.txt").unlink()
        _git("mv", "README.md", "README2.md", cwd=work)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "Second", "-m", "Body line", cwd=work)
        _git("checkout", "-q", "-b", "feature", cwd=work)
        (work / "new.md").write_text("x\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "Feature work", cwd=work)
        _git("checkout", "-q", "main", cwd=work)
        self.remote = root / "remote.git"
        _git("clone", "-q", "--bare", str(work), str(self.remote), cwd=root)
        url = mock.patch.object(RepoMirror, "url", new_callable=mock.PropertyMock, return_value=str(self.remote))
        url.start()
        self.addCleanup(url.stop)
        mock.patch.dict(browse._synced, clear=True).start()
        mock.patch.dict(browse._descriptions, {"acme/shop": "A shop"}).start()
        self.addCleanup(mock.patch.stopall)
        store = get_store()
        store.watches["w_000000000001"] = Watch(id="w_000000000001", name="Stripe", spec_url="https://x/s.json",
                                                repo="acme/shop", validate_commands=["true"],
                                                created_at=datetime.now(UTC))
        self.client = TestClient(app)
        self.base = "/api/repos/acme/shop"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def get(self, route, **params):
        r = self.client.get(self.base + route, params=params)
        return r.status_code, r.json()

    def test_list_and_summary(self):
        self.assertEqual(self.client.get("/api/repos").json()["repos"], [
            {"repo": "acme/shop", "providers": ["Stripe"], "watch_ids": ["w_000000000001"], "branches": [None]}])
        status, s = self.get("")
        self.assertEqual(status, 200)
        self.assertEqual((s["default_branch"], s["ref"], s["branch_count"], s["description"]),
                         ("main", "main", 2, "A shop"))
        self.assertEqual((s["head"]["subject"], s["head"]["author"]), ("Second", "Rose"))
        self.assertEqual(s["clone_url"], "https://github.com/acme/shop.git")
        self.assertGreater(s["size_bytes"], 0)
        self.assertEqual(self.get("", ref="feature")[1]["head"]["subject"], "Feature work")
        self.assertEqual(self.get("", ref="nope")[0], 404)
        self.assertEqual(self.get("", ref="-x")[0], 400)
        self.assertEqual(self.client.get("/api/repos/acme/other").status_code, 404)  # not watched

    def test_tree_and_blob(self):
        status, t = self.get("/tree")
        self.assertEqual(status, 200)
        self.assertEqual([(e["name"], e["type"]) for e in t["entries"]],
                         [("src", "tree"), ("logo.png", "blob"), ("README2.md", "blob")])
        self.assertEqual([e["last_commit"]["subject"] for e in t["entries"]], ["Second", "First", "Second"])
        self.assertEqual(t["entries"][2]["size"], 7)
        self.assertEqual([e["path"] for e in self.get("/tree", path="src")[1]["entries"]], ["src/pay.js"])
        self.assertEqual(self.get("/tree", path="nope")[0], 404)
        self.assertEqual(self.get("/tree", path="../etc")[0], 400)

        status, b = self.get("/blob", path="src/pay.js")
        self.assertEqual((status, b["text"], b["binary"], b["last_commit"]["subject"]),
                         (200, "charge({ amount })\n", False, "Second"))
        self.assertEqual(self.get("/blob", path="src/pay.js", ref="feature")[1]["text"], "charge({ amount })\n")
        png = self.get("/blob", path="logo.png")[1]
        self.assertEqual((png["binary"], png["text"], png["size"]), (True, None, 7))
        self.assertEqual(self.get("/blob", path="src")[0], 404)  # a folder
        self.assertEqual(self.get("/blob", path="src/../README2.md")[0], 400)

    def test_commits_commit_and_branches(self):
        status, c = self.get("/commits", limit=1)
        self.assertEqual((status, [x["subject"] for x in c["commits"]], c["has_more"]), (200, ["Second"], True))
        self.assertEqual([x["subject"] for x in self.get("/commits", skip=1)[1]["commits"]], ["First"])
        self.assertEqual([x["subject"] for x in self.get("/commits", path="logo.png")[1]["commits"]], ["First"])

        sha = c["commits"][0]["short"]
        status, d = self.get(f"/commits/{sha}")
        self.assertEqual((status, d["commit"]["body"], len(d["commit"]["parents"])), (200, "Body line", 1))
        files = {f["path"]: f for f in d["files"]}
        self.assertEqual(files["old.txt"]["status"], "deleted")
        self.assertEqual((files["README2.md"]["status"], files["README2.md"]["previous_path"]), ("renamed", "README.md"))
        pay = files["src/pay.js"]
        self.assertEqual((pay["status"], pay["additions"], pay["deletions"]), ("modified", 1, 1))
        self.assertTrue(pay["patch"].startswith("@@"))
        self.assertIn("+charge({ amount })", pay["patch"])
        self.assertEqual(self.get("/commits/abcdef1")[0], 404)

        status, br = self.get("/branches")
        self.assertEqual((status, br["default"]), (200, "main"))
        self.assertEqual([(b["name"], b["is_default"]) for b in br["branches"]], [("main", True), ("feature", False)])
        self.assertEqual(br["branches"][1]["last_commit"]["subject"], "Feature work")


if __name__ == "__main__":
    unittest.main()
