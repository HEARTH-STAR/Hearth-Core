"""Update this Hearth-Core to the newest commit of the branch it was cloned from, then restart.

In a Space, Hearth-Core is not in the image: Hearth-backend clones ``main`` into ``/workspace/hearth-core``
once, at creation (``git clone --depth 1 --branch main``), and ``hearth-space-start`` runs it. An update
therefore fetches that branch, moves the checkout to it (``reset --hard``) and stops this process; the
container's restart policy (Docker ``unless-stopped``, Azure ``Always``) starts ``hearth-space-start`` again,
which reinstalls the requirements if they changed and runs the new code.

Only a Space container sets ``HEARTH_SELF_UPDATE=1`` (Hearth-backend's space template): on a laptop checkout
an update would discard local work, so it is refused there, as it is when tracked files have local changes.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from utils.commands import run

REPO = Path(__file__).resolve().parents[2]
CHECK_TTL = timedelta(minutes=10)
RESTART_DELAY_SECONDS = 1.0

_latest: dict[str, Any] | None = None
_checked_at: datetime | None = None


class UpdateError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def enabled() -> bool:
    return os.environ.get("HEARTH_SELF_UPDATE", "").lower() in ("1", "true", "yes")


async def _git(*args: str, timeout: float = 120) -> str:
    r = await run(["git", "-C", str(REPO), *args], timeout=timeout, separate_stderr=True)
    if not r.ok:
        raise UpdateError(502, f"git {args[0]} failed: {(r.stderr or r.output).strip()[:300] or r.returncode}")
    return r.stdout.strip()


async def _commit(ref: str) -> dict[str, Any]:
    sha, subject, date = (await _git("log", "-1", "--format=%H%x00%s%x00%cI", ref)).split("\x00")
    return {"sha": sha, "subject": subject, "date": date}


async def _branch() -> str:
    branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise UpdateError(409, "This Hearth-Core is not on a branch, so there is nothing to update to.")
    return branch


async def _fetch(branch: str) -> dict[str, Any]:
    global _latest, _checked_at
    await _git("fetch", "--quiet", "--depth", "1", "--no-tags", "origin", branch, timeout=300)
    _latest, _checked_at = await _commit("FETCH_HEAD"), datetime.now(UTC)
    return _latest


async def version(*, refresh: bool = False) -> dict[str, Any]:
    """The running commit and, in a Space, the newest one on its branch (fetched at most every 10 minutes)."""
    out: dict[str, Any] = {"enabled": enabled(), "branch": None, "current": None, "latest": None,
                           "update_available": False, "checked_at": None, "error": None}
    try:
        out["current"] = await _commit("HEAD")
        out["branch"] = await _branch()
        if not out["enabled"]:
            return out
        stale = _checked_at is None or datetime.now(UTC) - _checked_at > CHECK_TTL
        latest = await _fetch(out["branch"]) if refresh or stale or _latest is None else _latest
    except UpdateError as exc:
        out["error"] = exc.detail
        return out
    out["latest"], out["checked_at"] = latest, _checked_at.isoformat() if _checked_at else None
    out["update_available"] = latest["sha"] != out["current"]["sha"]
    return out


async def update(*, busy: str | None = None) -> dict[str, Any]:
    """Move to the newest commit and schedule a restart. ``busy`` names work a restart would cut off."""
    if not enabled():
        raise UpdateError(403, "Self-update is off here (HEARTH_SELF_UPDATE); it runs in Space containers only.")
    if busy:
        raise UpdateError(409, f"{busy} Update when it has finished.")
    if await _git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(409, "This Hearth-Core has local changes to its files; an update would discard them.")
    branch = await _branch()
    before = await _commit("HEAD")
    latest = await _fetch(branch)
    if latest["sha"] == before["sha"]:
        return {"updated": False, "restarting": False, "from": before, "to": latest}
    await _git("reset", "--quiet", "--hard", "FETCH_HEAD")
    asyncio.get_running_loop().call_later(RESTART_DELAY_SECONDS, os.kill, os.getpid(), signal.SIGTERM)
    return {"updated": True, "restarting": True, "from": before, "to": latest}
