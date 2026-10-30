"""The API watch's background work: due checks, the fix queue, and keeping open PRs in sync.

One scheduler per process, started from ``server.py``'s lifespan (``HEARTH_API_WATCH=0`` turns it off).
Every ``TICK_SECONDS`` it runs the checks that are due, queues a fix for each new breaking detection of an
``auto_pr`` watch, and every ``PR_SYNC_SECONDS`` re-reads open PRs from GitHub (merged / closed / review
decision). Fixes run one at a time: an agent run is heavy and they share the Space's repo mirrors.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from services.hearth_agent.connectors.github.gh_api import GhCli
from services.hearth_agent.watch import checkout, review
from services.hearth_agent.watch.check import run_check
from services.hearth_agent.watch.fixer import Fixer, link_pull
from services.hearth_agent.watch.models import RUNNING, Watch
from services.hearth_agent.watch.store import WatchStore, get_store

log = logging.getLogger(__name__)

TICK_SECONDS = 30
PR_SYNC_SECONDS = 120


def enabled() -> bool:
    return os.environ.get("HEARTH_API_WATCH", "1").lower() not in ("0", "false", "no")


def _now() -> datetime:
    return datetime.now(UTC)


class WatchScheduler:
    def __init__(self, store_factory: Callable[[], WatchStore] = get_store,
                 fixer_factory: Callable[[WatchStore], Fixer] = Fixer):
        self.store_factory = store_factory
        self.fixer_factory = fixer_factory
        self.tick_at: datetime | None = None
        self._checking: set[str] = set()
        self._queue: list[str] = []
        self._active: str | None = None
        self._wake: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()

    # -- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    def start(self) -> None:
        if self.running:
            return
        self._recover()
        self._wake = asyncio.Event()
        if self._queue:
            self._wake.set()
        self._tasks = [asyncio.create_task(self._tick_loop(), name="api-watch-tick"),
                       asyncio.create_task(self._fix_loop(), name="api-watch-fix")]

    async def stop(self) -> None:
        tasks = [*self._tasks, *self._background]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks, self._background = [], set()

    def _recover(self) -> None:
        """After a restart: re-queue queued fixes, fail the ones a restart cut off, unstick checks."""
        store = self.store_factory()
        for w in store.watches.values():
            if w.status.state == "checking":
                w.status.state = "idle"
        for d in sorted(store.detections.values(), key=lambda d: d.detected_at):
            if d.fix.status == "queued":
                self._queue.append(d.id)
            elif d.fix.status in RUNNING:
                d.fix.status, d.fix.message, d.fix.updated_at = "failed", "Interrupted by a restart; retry the fix.", _now()
        store.save()

    # -- status ----------------------------------------------------------------

    def status(self) -> dict:
        store = self.store_factory()
        active = store.detections.get(self._active) if self._active else None
        return {
            "running": self.running,
            "tick_at": self.tick_at.isoformat() if self.tick_at else None,
            "active": {"detection_id": active.id, "status": active.fix.status} if active else None,
            "queue": list(self._queue),
        }

    def is_checking(self, watch_id: str) -> bool:
        return watch_id in self._checking

    # -- checks ----------------------------------------------------------------

    async def check(self, watch: Watch) -> None:
        if watch.id in self._checking:
            return
        self._checking.add(watch.id)
        store = self.store_factory()
        try:
            detection = await run_check(store, watch)
        finally:
            self._checking.discard(watch.id)
        if detection is not None and watch.auto_pr and watch.id in store.watches:
            self.enqueue(detection.id)

    def check_soon(self, watch: Watch) -> bool:
        """Start a check in the background (route: "Check now"). False when one is already running."""
        if watch.id in self._checking:
            return False
        task = asyncio.create_task(self.check(watch))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return True

    async def tick(self) -> None:
        self.tick_at = now = _now()
        store = self.store_factory()
        for watch in list(store.watches.values()):
            due = watch.status.next_check_at is None or watch.status.next_check_at <= now
            if watch.enabled and due and watch.id not in self._checking:
                try:
                    await self.check(watch)
                except Exception:
                    log.exception("check of watch %s failed", watch.id)
        await self.sync_pulls(store, now)
        for repo, branch in {w.repo: w.base_branch for w in store.watches.values()}.items():
            if not (checkout.checkout_dir(repo) / ".git").exists():
                await checkout.refresh(repo, branch)

    async def sync_pulls(self, store: WatchStore, now: datetime, *, gh: GhCli | None = None) -> None:
        stale = now - timedelta(seconds=PR_SYNC_SECONDS)
        for d in list(store.detections.values()):
            pr = d.fix.pr
            if d.fix.status != "pr_open" or pr is None or (pr.checked_at and pr.checked_at > stale):
                continue
            watch = store.watches.get(d.watch_id)
            if watch is None:
                continue
            await self.sync_pull(store, d.id, gh=gh)

    async def sync_pull(self, store: WatchStore, detection_id: str, *, gh: GhCli | None = None) -> None:
        """Re-read one detection's PR from GitHub; errors leave it as it was (the next sync retries)."""
        d = store.detections.get(detection_id)
        watch = store.watches.get(d.watch_id) if d else None
        if d is None or watch is None or d.fix.pr is None:
            return
        gh = gh or GhCli()
        try:
            pull = review._ok(await gh.api(f"repos/{watch.repo}/pulls/{d.fix.pr.number}"))
            reviews = await review._pages(gh, f"repos/{watch.repo}/pulls/{d.fix.pr.number}/reviews")
        except review.GitHubError as exc:
            log.info("PR sync for %s skipped: %s", d.id, exc.detail)
            return
        link_pull(d, pull, reviews)
        store.save()

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("api watch tick failed")
            await asyncio.sleep(TICK_SECONDS)

    # -- fixes -----------------------------------------------------------------

    def enqueue(self, detection_id: str) -> bool:
        """Queue a fix. False when it is already queued or running, or its PR is open / merged."""
        store = self.store_factory()
        d = store.detections.get(detection_id)
        if d is None or detection_id in self._queue or detection_id == self._active:
            return False
        if d.fix.status in RUNNING or d.fix.status in ("pr_open", "merged"):
            return False
        d.fix.status, d.fix.message, d.fix.updated_at = "queued", "Waiting for the fixer", _now()
        store.save()
        self._queue.append(detection_id)
        if self._wake:
            self._wake.set()
        return True

    async def _fix_loop(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                self._active = self._queue.pop(0)
                store = self.store_factory()
                try:
                    await self.fixer_factory(store).run(self._active)
                except Exception:
                    log.exception("fix %s crashed", self._active)
                finally:
                    self._active = None


_scheduler: WatchScheduler | None = None


def get_scheduler() -> WatchScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = WatchScheduler()
    return _scheduler
