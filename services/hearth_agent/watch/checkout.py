"""A readable checkout of every watched repo in the workspace: ``<workspace>/repos/<owner>/<name>``.

The Space's Files tree shows the workspace, and chat agents read the watched code from there. The checkout
is cloned from the repo's bare mirror (no credential, no network) on the watch's branch, and moved to the
mirror's newest commit whenever the mirror is fetched — unless it has local edits, which are left alone.
Fixes never happen here: they run in throwaway exports and reach the repo as pull requests.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from pathlib import Path

from services.hearth_agent.connectors.github.repo import RepoMirror
from services.storage.paths import hearth_state_dir
from services.storage.workspace import workspace_root
from utils.commands import run, safe_arg

log = logging.getLogger(__name__)

_locks: dict[str, asyncio.Lock] = {}


def _rmtree(path: Path) -> None:
    def retry(func, p, _exc) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=retry)


def checkout_dir(repo: str) -> Path:
    owner, name = repo.split("/", 1)
    return workspace_root() / "repos" / owner / name


def relative_path(repo: str) -> str:
    """The checkout's path relative to the workspace root, as the Files tree shows it."""
    return f"repos/{repo}"


async def _git(*args: str, cwd: Path | None = None) -> bool:
    r = await run(["git", *args], cwd=cwd, timeout=600, separate_stderr=True)
    if not r.ok:
        log.info("checkout: git %s failed: %s", args[0], r.stderr.strip()[:300])
    return r.ok


async def refresh(repo: str, branch: str | None = None) -> Path | None:
    """Create or update the checkout from the mirror (which the caller has synced). Never raises."""
    lock = _locks.setdefault(repo, asyncio.Lock())
    async with lock:
        mirror = RepoMirror(hearth_state_dir(), repo)
        if not (mirror.mirror_dir / "HEAD").exists():
            return None
        try:
            branch = branch or await mirror.default_branch()
        except Exception:
            return None
        dest = checkout_dir(repo)
        if not (dest / ".git").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            _rmtree(dest)
            ok = await _git("clone", "-q", "--branch", safe_arg(branch), "--", str(mirror.mirror_dir), str(dest))
            return dest if ok else None
        status = await run(["git", "status", "--porcelain"], cwd=dest, timeout=60, separate_stderr=True)
        if not status.ok or status.stdout.strip():
            return dest  # someone edited it: keep their work
        if await _git("fetch", "-q", "origin", cwd=dest):
            await _git("checkout", "-q", "-B", safe_arg(branch), f"origin/{branch}", cwd=dest)
        return dest


def remove(repo: str) -> None:
    """Drop a repo's checkout (its last watch was deleted), and the owner folder when it empties."""
    dest = checkout_dir(repo)
    _rmtree(dest)
    try:
        dest.parent.rmdir()
    except OSError:
        pass
