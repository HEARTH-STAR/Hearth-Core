import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from routers.hearth_agent import watch as watch_route
from server import app
from services.hearth_agent.connectors.github.gh_api import GhErrorKind, GhResult
from services.hearth_agent.connectors.github.repo import RepoMirror
from services.hearth_agent.models import AgentResult
from services.hearth_agent.watch import check, impact, review, validation
from services.hearth_agent.watch.fixer import Fixer
from services.hearth_agent.watch.models import Watch
from services.hearth_agent.watch.scheduler import WatchScheduler
from services.hearth_agent.watch.store import get_store

OLD_SPEC = {
    "openapi": "3.0.0", "info": {"version": "2025-01-01"},
    "paths": {
        "/v1/charges": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"amount": {"type": "integer"}, "source": {"type": "string"}}}}}}}},
        "/v1/customers/{id}": {"get": {}},
    },
}
NEW_SPEC = {  # source removed from POST /v1/charges: breaking
    "openapi": "3.0.0", "info": {"version": "2025-02-01"},
    "paths": {
        "/v1/charges": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "properties": {"amount": {"type": "integer"}}}}}}}},
        "/v1/customers/{id}": {"get": {}},
    },
}
ADDITIVE_SPEC = {**OLD_SPEC, "info": {"version": "2025-01-15"},
                 "paths": {**OLD_SPEC["paths"], "/v1/refunds": {"post": {}}}}


def fetcher(*texts):
    """A fake fetch_spec returning each text in turn (None = 304); an Exception instance is raised."""
    queue = list(texts)

    async def fetch(url, *, etag=None, last_modified=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return check.Fetched(None if item is None else json.dumps(item) if isinstance(item, dict) else item)
    return fetch


class StateDirTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": self.tmp.name})
        self.env.start()
        self.store = get_store()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add_watch(self, **kw) -> Watch:
        fields = {"id": "w_000000000001", "name": "acme", "spec_url": "https://api.example.com/openapi.json",
                  "repo": "acme/shop", "validate_commands": ["true"], "created_at": datetime.now(UTC), **kw}
        w = Watch(**fields)
        self.store.watches[w.id] = w
        self.store.save()
        return w


class CheckTests(StateDirTest):
    async def test_baseline_then_breaking_change_then_dedupe(self):
        w = self.add_watch()
        fetch = fetcher(OLD_SPEC, OLD_SPEC, None, NEW_SPEC, OLD_SPEC, NEW_SPEC)
        self.assertIsNone(await check.run_check(self.store, w, fetch=fetch))  # baseline
        self.assertEqual((w.history[0].result, w.status.spec_version), ("baseline", "2025-01-01"))
        self.assertIsNone(await check.run_check(self.store, w, fetch=fetch))  # same text
        self.assertIsNone(await check.run_check(self.store, w, fetch=fetch))  # 304
        self.assertEqual([c.result for c in w.history], ["unchanged", "unchanged", "baseline"])

        d = await check.run_check(self.store, w, fetch=fetch)
        self.assertIsNotNone(d)
        self.assertEqual((d.from_version, d.to_version, d.breaking_count), ("2025-01-01", "2025-02-01", 1))
        self.assertEqual(d.changes[0]["description"], "POST /v1/charges: param removed (source)")
        self.assertTrue(d.id.startswith("cr_") and d.fix.status == "detected")
        self.assertEqual(w.history[0].detection_id, d.id)
        self.assertEqual(self.store.read_spec(w.id, d.from_sha), json.dumps(OLD_SPEC))  # kept for the comparison

        # the spec flips back and forth: the same breaking diff never makes a second detection
        await check.run_check(self.store, w, fetch=fetch)
        self.assertIsNone(await check.run_check(self.store, w, fetch=fetch))
        self.assertEqual(list(self.store.detections), [d.id])
        self.assertEqual(w.history[0].detection_id, d.id)

        reloaded = type(self.store)(self.store.root)  # survives a restart
        self.assertEqual(reloaded.detections[d.id].changes, d.changes)

    async def test_additive_change_is_not_a_detection(self):
        w = self.add_watch()
        fetch = fetcher(OLD_SPEC, ADDITIVE_SPEC)
        await check.run_check(self.store, w, fetch=fetch)
        self.assertIsNone(await check.run_check(self.store, w, fetch=fetch))
        self.assertEqual((w.history[0].result, w.history[0].breaking, w.history[0].total), ("changed", 0, 1))
        self.assertFalse(self.store.detections)

    async def test_errors_keep_the_baseline(self):
        w = self.add_watch()
        fetch = fetcher("not a spec", OLD_SPEC, check.FetchError("GET x answered 500"), "openapi: [1")
        await check.run_check(self.store, w, fetch=fetch)
        self.assertEqual((w.status.state, w.status.spec_sha), ("error", None))  # a broken first fetch is no baseline
        await check.run_check(self.store, w, fetch=fetch)
        sha = w.status.spec_sha
        for _ in range(2):
            await check.run_check(self.store, w, fetch=fetch)
            self.assertEqual((w.status.state, w.status.spec_sha), ("error", sha))
        self.assertIn("500", w.history[1].error)
        self.assertIsNotNone(w.status.next_check_at)

    async def test_private_addresses_are_refused(self):
        with self.assertRaisesRegex(check.FetchError, "private address"):
            await check._check_public("http://127.0.0.1:5010/openapi.json")
        with mock.patch.dict(os.environ, {"HEARTH_WATCH_ALLOW_PRIVATE": "1"}):
            await check._check_public("http://127.0.0.1:5010/openapi.json")


class ImpactTests(unittest.TestCase):
    def test_scan(self):
        changes = [
            {"type": "param_removed", "symbol": "POST /v1/charges", "kind": "operation", "detail": "source",
             "breaking": True},
            {"type": "param_removed", "symbol": "GET /v1/customers/{}", "kind": "operation",
             "detail": "expand.payment_method_types", "breaking": True},
            {"type": "added", "symbol": "POST /v1/refunds", "kind": "operation", "breaking": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "pay.ts").write_text(
                "const r = await api.post('/v1/charges', { amount, source });\n"
                "api.post('/v1/charges/ch_1/capture');\n"  # another operation: not a match
                "const url = `/customers/${id}`;\n"  # version kept in the base URL
                "send({ payment_method_types: ['card'] });\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "x.js").write_text("post('/v1/charges')", encoding="utf-8")
            (root / "openapi.json").write_text('"/v1/charges"', encoding="utf-8")  # specs are not call sites
            got = [(m.path, m.line, m.symbol) for m in impact.scan(root, changes)]
        self.assertEqual(got, [
            ("src/pay.ts", 1, "POST /v1/charges"),
            ("src/pay.ts", 1, "source"),
            ("src/pay.ts", 3, "GET /v1/customers/{}"),
            ("src/pay.ts", 4, "payment_method_types"),
        ])


@unittest.skipIf(shutil.which("sh") is None, "sh not installed")
class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_regressions_are_against_the_baseline(self):
        commands = ["test -f ok", "exit 3"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ok").touch()
            base = await validation.run_commands(commands, Path(tmp), "baseline")
            self.assertTrue(validation.usable_baseline(base))
            self.assertEqual([c.exit_code for c in base.commands], [0, 3])
            same = validation.compare(base, await validation.run_commands(commands, Path(tmp), "candidate"))
            self.assertEqual((same.passed, same.regressions), (True, []))  # "exit 3" already failed: ignored
            (Path(tmp) / "ok").unlink()
            worse = validation.compare(base, await validation.run_commands(commands, Path(tmp), "candidate"))
            self.assertEqual((worse.passed, worse.regressions), (False, ["test -f ok"]))
            self.assertFalse(validation.usable_baseline(await validation.run_commands(["false"], Path(tmp), "baseline")))


class ReviewTests(unittest.TestCase):
    def test_review_decision(self):
        def r(user, state, at):
            return {"user": {"login": user}, "state": state, "submitted_at": at}
        self.assertEqual(review.review_decision([]), "pending")
        self.assertEqual(review.review_decision([r("a", "COMMENTED", "1")]), "commented")
        self.assertEqual(review.review_decision([r("a", "CHANGES_REQUESTED", "1"), r("a", "APPROVED", "2")]),
                         "approved")
        self.assertEqual(review.review_decision([r("a", "APPROVED", "1"), r("b", "CHANGES_REQUESTED", "2")]),
                         "changes_requested")

    def test_errors_keep_githubs_message_and_status(self):
        with self.assertRaises(review.GitHubError) as e:
            review._ok(GhResult(False, kind=GhErrorKind.CONFLICT, status=422,
                                message="gh: Can not approve your own pull request (HTTP 422)"))
        self.assertEqual((e.exception.status, e.exception.detail), (422, "Can not approve your own pull request"))
        with self.assertRaises(review.GitHubError) as e:
            review._ok(GhResult(False, kind=GhErrorKind.NOT_AUTHENTICATED, message="run gh auth login"))
        self.assertEqual((e.exception.status, e.exception.code), (409, "github_not_connected"))


def _git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class FakeDispatcher:
    """Edits the export like an agent would, then asks for validation like ``finish`` does."""

    agent_name = "claude_code"

    def __init__(self, edit=True, status="passed"):
        self.edit, self.status, self.jobs = edit, status, []

    async def run(self, job, workspace, hooks):
        self.jobs.append(job)
        if self.edit:
            pay = workspace / "src" / "pay.js"
            pay.write_text(pay.read_text().replace(", source", ""), encoding="utf-8")
        v = await hooks.validate_candidate()
        status = self.status if v.passed else "failed_fix"
        return AgentResult(job_id=job.job_id, intelligence="claude_code", status=status, summary="dropped source",
                           validation=v)


@unittest.skipIf(shutil.which("git") is None or shutil.which("sh") is None, "git and sh needed")
class FixerTests(StateDirTest):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        work, self.remote = root / "work", root / "remote.git"
        work.mkdir()
        (work / "src").mkdir()
        (work / "src" / "pay.js").write_text("api.post('/v1/charges', { amount, source });\n", encoding="utf-8")
        _git("init", "-q", "-b", "main", cwd=work)
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "init", cwd=work)
        _git("clone", "-q", "--bare", str(work), str(self.remote))
        url = mock.patch.object(RepoMirror, "url", new_callable=mock.PropertyMock, return_value=str(self.remote))
        url.start()
        self.addCleanup(url.stop)
        # baseline: the first command passes, the second already fails (ignored); the fix must keep the first
        self.watch = self.add_watch(validate_commands=["test -f src/pay.js", "exit 7"])
        self.detection = None

    async def detect(self):
        fetch = fetcher(OLD_SPEC, NEW_SPEC)
        await check.run_check(self.store, self.watch, fetch=fetch)
        self.detection = await check.run_check(self.store, self.watch, fetch=fetch)
        return self.detection

    def gh(self, existing=None):
        gh = mock.AsyncMock()
        gh.api.return_value = GhResult(True, data=[existing] if existing else [])
        gh.create_pull_request.return_value = GhResult(True, data={
            "number": 7, "html_url": "https://github.com/acme/shop/pull/7", "title": "t", "state": "open",
            "head": {"sha": "abc"}})
        return gh

    async def test_validated_fix_opens_one_pr(self):
        d = await self.detect()
        gh, agent = self.gh(), FakeDispatcher()
        await Fixer(self.store, gh=gh, dispatcher_factory=lambda: agent).run(d.id)

        self.assertEqual(d.fix.status, "pr_open", d.fix.message)
        self.assertEqual((d.fix.pr.number, d.fix.agent, d.fix.attempts), (7, "claude_code", 1))
        self.assertEqual([m.path for m in d.fix.impact], ["src/pay.js", "src/pay.js"])
        self.assertEqual([c.exit_code for c in d.fix.baseline.commands], [0, 7])
        self.assertTrue(d.fix.validation.passed)
        job = agent.jobs[0]
        self.assertEqual((job.impact_report.repository, job.allowed_paths), ("acme/shop", ["src/pay.js"]))

        kwargs = gh.create_pull_request.await_args.kwargs
        self.assertEqual((kwargs["head"], kwargs["base"]), (d.fix.branch, "main"))
        self.assertIn("## Breaking changes in the spec", kwargs["body"])
        self.assertNotIn("/hearth revise", kwargs["body"])
        pushed = _git("--git-dir", str(self.remote), "show", f"{d.fix.branch}:src/pay.js")
        self.assertEqual(pushed, "api.post('/v1/charges', { amount });\n")
        self.assertFalse(any((Path(self.tmp.name) / "workspaces").iterdir()))  # workspaces cleaned up

        scheduler = WatchScheduler(store_factory=lambda: self.store)
        self.assertFalse(scheduler.enqueue(d.id))  # its PR is open: no second fix, no second PR

    async def test_existing_pr_on_github_is_linked_not_duplicated(self):
        d = await self.detect()
        gh = self.gh(existing={"number": 3, "html_url": "u", "title": "t", "state": "open", "head": {"sha": "s"}})
        agent = FakeDispatcher()
        await Fixer(self.store, gh=gh, dispatcher_factory=lambda: agent).run(d.id)
        self.assertEqual((d.fix.status, d.fix.pr.number), ("pr_open", 3))
        self.assertFalse(agent.jobs)
        gh.create_pull_request.assert_not_awaited()

    async def test_unvalidated_fix_opens_nothing(self):
        d = await self.detect()
        gh = self.gh()
        self.watch.validate_commands = ["grep -q source src/pay.js"]  # the fix removes it: a regression
        await Fixer(self.store, gh=gh, dispatcher_factory=lambda: FakeDispatcher()).run(d.id)
        self.assertEqual(d.fix.status, "failed")
        self.assertEqual(d.fix.validation.regressions, ["grep -q source src/pay.js"])
        gh.create_pull_request.assert_not_awaited()
        self.assertNotIn(d.fix.branch, _git("--git-dir", str(self.remote), "branch"))

    async def test_no_change_and_no_impact(self):
        d = await self.detect()
        gh = self.gh()
        await Fixer(self.store, gh=gh, dispatcher_factory=lambda: FakeDispatcher(edit=False)).run(d.id)
        self.assertEqual((d.fix.status, d.fix.message), ("failed", "The agent finished without changing any file."))

        (Path(self.tmp.name) / "work" / "src" / "pay.js").unlink()
        work = Path(self.tmp.name) / "work"
        (work / "src" / "other.js").write_text("hello()\n", encoding="utf-8")
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", "no calls", cwd=work)
        _git("push", "-q", str(self.remote), "HEAD:main", cwd=work)
        await Fixer(self.store, gh=gh, dispatcher_factory=lambda: FakeDispatcher()).run(d.id)
        self.assertEqual((d.fix.status, d.fix.attempts), ("no_impact", 2))
        gh.create_pull_request.assert_not_awaited()


class SchedulerTests(StateDirTest):
    async def test_recover_and_queue(self):
        self.add_watch()
        fetch = fetcher(OLD_SPEC, NEW_SPEC)
        w = self.store.watches["w_000000000001"]
        await check.run_check(self.store, w, fetch=fetch)
        d = await check.run_check(self.store, w, fetch=fetch)
        d.fix.status = "fixing"
        w.status.state = "checking"
        s = WatchScheduler(store_factory=lambda: self.store)
        s._recover()
        self.assertEqual((d.fix.status, w.status.state), ("failed", "idle"))
        self.assertTrue(s.enqueue(d.id))
        self.assertFalse(s.enqueue(d.id))
        self.assertEqual(s.status()["queue"], [d.id])

        d.fix.status = "queued"
        s2 = WatchScheduler(store_factory=lambda: self.store)
        s2._recover()
        self.assertEqual(s2._queue, [d.id])

    async def test_pr_sync_moves_to_merged(self):
        self.add_watch()
        w = self.store.watches["w_000000000001"]
        fetch = fetcher(OLD_SPEC, NEW_SPEC)
        await check.run_check(self.store, w, fetch=fetch)
        d = await check.run_check(self.store, w, fetch=fetch)
        from services.hearth_agent.watch.fixer import link_pull
        link_pull(d, {"number": 7, "html_url": "u", "title": "t", "state": "open", "head": {"sha": "a"}})
        d.fix.pr.checked_at = datetime.now(UTC) - timedelta(minutes=5)
        gh = mock.AsyncMock()
        gh.api.side_effect = [
            GhResult(True, data={"number": 7, "html_url": "u", "title": "t", "state": "closed", "merged": True,
                                 "head": {"sha": "b"}}),
            GhResult(True, data=[{"user": {"login": "x"}, "state": "APPROVED", "submitted_at": "1"}]),
        ]
        await WatchScheduler(store_factory=lambda: self.store).sync_pulls(self.store, datetime.now(UTC), gh=gh)
        self.assertEqual((d.fix.status, d.fix.pr.state, d.fix.pr.review_decision), ("merged", "merged", "approved"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HEARTH_STATE_DIR": self.tmp.name, "HEARTH_API_WATCH": "0"})
        self.env.start()
        self.client = TestClient(app)
        self.scheduler = WatchScheduler()
        patcher = mock.patch.object(watch_route, "get_scheduler", return_value=self.scheduler)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add(self, **kw):
        body = {"name": "stripe", "spec_url": "https://example.com/spec.json", "repo": "acme/shop",
                "validate_commands": ["npm test"], **kw}
        with mock.patch.object(self.scheduler, "check_soon") as soon:
            r = self.client.post("/api/watch", json=body)
        return r, soon

    def detection(self):
        store = get_store()
        w = next(iter(store.watches.values()))
        import asyncio
        fetch = fetcher(OLD_SPEC, NEW_SPEC)
        asyncio.run(check.run_check(store, w, fetch=fetch))
        return asyncio.run(check.run_check(store, w, fetch=fetch))

    def test_watch_crud(self):
        r, soon = self.add(validate_commands=["  npm ci && npm test  ", ""])
        self.assertEqual(r.status_code, 201)
        watch = r.json()
        self.assertRegex(watch["id"], r"^w_[0-9a-f]{12}$")
        self.assertEqual((watch["validate_commands"], watch["interval_minutes"], watch["auto_pr"]),
                         (["npm ci && npm test"], 60, True))
        soon.assert_called_once()  # the baseline check starts at once

        for bad in ({"repo": "nope"}, {"spec_url": "file:///etc/passwd"}, {"validate_commands": [" "]},
                    {"interval_minutes": 1}, {"surprise": 1}):
            self.assertEqual(self.add(**bad)[0].status_code, 422, bad)

        listed = self.client.get("/api/watch").json()
        self.assertEqual([w["id"] for w in listed["watches"]], [watch["id"]])
        self.assertEqual(listed["poller"]["queue"], [])

        r = self.client.patch(f"/api/watch/{watch['id']}", json={"enabled": False, "base_branch": "release"})
        self.assertEqual((r.json()["enabled"], r.json()["base_branch"], r.json()["status"]["next_check_at"]),
                         (False, "release", None))
        self.assertEqual(self.client.patch("/api/watch/w_nope", json={}).status_code, 422)
        self.assertEqual(self.client.patch("/api/watch/w_000000000000", json={}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/watch/{watch['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/watch").json()["watches"], [])

    def test_detection_fix_and_pull_routes(self):
        self.add()
        d = self.detection()
        got = self.client.get(f"/api/watch/detections/{d.id}").json()
        self.assertEqual(got["changes"][0]["breaking"], True)
        self.assertNotIn("change_record", got)
        self.assertEqual(self.client.get("/api/watch").json()["detections"][0]["id"], d.id)

        self.assertEqual(self.client.post(f"/api/watch/detections/{d.id}/fix", json={}).status_code, 202)
        self.assertEqual(self.client.post(f"/api/watch/detections/{d.id}/fix", json={}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/watch/detections/{d.id}/pull").status_code, 404)  # no PR yet

        from services.hearth_agent.watch.fixer import link_pull
        link_pull(d, {"number": 7, "html_url": "u", "title": "t", "state": "open", "head": {"sha": "a"}})
        get_store().save()
        own = review.GitHubError(422, "Can not approve your own pull request")
        with mock.patch.object(review, "submit_review", mock.AsyncMock(side_effect=own)):
            r = self.client.post(f"/api/watch/detections/{d.id}/pull/reviews", json={"event": "APPROVE"})
        self.assertEqual((r.status_code, r.json()), (422, {"detail": "Can not approve your own pull request"}))
        self.assertEqual(self.client.post(f"/api/watch/detections/{d.id}/pull/reviews",
                                          json={"event": "REQUEST_CHANGES"}).status_code, 422)  # needs a body
        self.assertEqual(self.client.post(f"/api/watch/detections/{d.id}/pull/comments",
                                          json={"body": "x", "line": 3}).status_code, 422)  # line needs path

        nogh = review.GitHubError(409, "GitHub is not connected", "github_not_connected")
        with mock.patch.object(review, "pull_files", mock.AsyncMock(side_effect=nogh)):
            r = self.client.get(f"/api/watch/detections/{d.id}/pull/files")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "github_not_connected"))

        with mock.patch.object(review, "merge", mock.AsyncMock(return_value={"merged": True, "sha": "m", "message": "ok"})):
            r = self.client.post(f"/api/watch/detections/{d.id}/pull/merge", json={"method": "squash"})
        self.assertEqual(r.json()["merged"], True)
        self.assertEqual(get_store().detections[d.id].fix.status, "merged")
        self.assertEqual(self.client.post(f"/api/watch/detections/{d.id}/fix", json={}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
