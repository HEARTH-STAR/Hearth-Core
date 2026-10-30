"""Read-only browsing of a watched repo: summary, tree, files, commits and branches, from its bare mirror.

Only repos with an API watch can be browsed (their mirror is what the fixer clones from). The mirror is
re-fetched from GitHub at most every :data:`SYNC_TTL`; every read is a ``git`` command on the mirror, with
the ref resolved to a commit first and paths passed after ``--`` (or as ``<commit>:<path>``), never a shell.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from services.hearth_agent.connectors.github.gh_api import GhCli
from services.hearth_agent.connectors.github.repo import RepoMirror
from services.hearth_agent.watch import checkout
from services.storage.paths import hearth_state_dir
from utils.commands import CommandResult, run, safe_arg

SYNC_TTL = timedelta(minutes=2)
MAX_BLOB = 512 * 1024
MAX_PATCH = 200 * 1024
MAX_ENTRIES = 1000
LOG_CONCURRENCY = 8
SEP = "\x1f"
COMMIT_FORMAT = "%H%x1f%h%x1f%s%x1f%an%x1f%cI"
REF_RE = re.compile(r"^[^\x00-\x20\x7f~^:?*\[\\-][^\x00-\x20\x7f~^:?*\[\\]{0,254}$")

_synced: dict[str, datetime] = {}
_descriptions: dict[str, str | None] = {}
_locks: dict[str, asyncio.Lock] = {}


class BrowseError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _watched_branch(repo: str) -> str | None:
    from services.hearth_agent.watch.store import get_store

    return next((w.base_branch for w in get_store().watches.values() if w.repo == repo and w.base_branch), None)


def check_path(path: str) -> str:
    """A repo-relative path: ``/`` separators, no leading ``/``, no ``..``, no NUL. ``""`` is the root."""
    path = path.strip().strip("/")
    if "\x00" in path or "\\" in path or any(part in ("..", ".") for part in path.split("/") if path):
        raise BrowseError(400, "That is not a path in the repo.")
    return path


def _commit(line: str) -> dict[str, Any]:
    sha, short, subject, author, date = line.split(SEP)[:5]
    return {"sha": sha, "short": short, "subject": subject, "author": author, "date": date}


class RepoBrowser:
    def __init__(self, repo: str):
        self.repo = repo
        self.mirror = RepoMirror(hearth_state_dir(), repo)

    async def _git(self, *args: str, timeout: float = 120) -> CommandResult:
        return await run(["git", "--git-dir", str(self.mirror.mirror_dir), *args], timeout=timeout,
                         separate_stderr=True)

    async def _ok(self, *args: str, timeout: float = 120) -> str:
        r = await self._git(*args, timeout=timeout)
        if not r.ok:
            raise BrowseError(502, f"git {args[0]} failed: {(r.stderr or r.output).strip()[:300]}")
        return r.stdout

    async def sync(self, *, force: bool = False) -> datetime:
        """Fetch the mirror when it is missing or older than SYNC_TTL; returns when it last fetched."""
        lock = _locks.setdefault(self.repo, asyncio.Lock())
        async with lock:
            last = _synced.get(self.repo)
            fresh = last and datetime.now(UTC) - last < SYNC_TTL and (self.mirror.mirror_dir / "HEAD").exists()
            if force or not fresh:
                try:
                    await self.mirror.sync()
                except Exception as exc:  # GitError: keep serving the old mirror when there is one
                    if not (self.mirror.mirror_dir / "HEAD").exists():
                        raise BrowseError(502, f"Could not fetch {self.repo}: {str(exc)[:300]}") from exc
                    return last or datetime.now(UTC)
                _synced[self.repo] = datetime.now(UTC)
                await checkout.refresh(self.repo, _watched_branch(self.repo))
            return _synced[self.repo]

    async def resolve(self, ref: str | None) -> tuple[str, str]:
        """(ref, commit sha): the default branch when ``ref`` is empty."""
        ref = ref or await self.mirror.default_branch()
        if not REF_RE.match(ref):
            raise BrowseError(400, "That is not a branch or commit.")
        r = await self._git("rev-parse", "--verify", "--quiet", safe_arg(ref) + "^{commit}")
        if not r.ok or not r.stdout.strip():
            raise BrowseError(404, f"No branch or commit {ref} in {self.repo}.")
        return ref, r.stdout.strip()

    async def last_commit(self, commit: str, path: str = "") -> dict[str, Any] | None:
        args = ["log", "-1", f"--format={COMMIT_FORMAT}", commit]
        if path:
            args += ["--", path]
        out = (await self._ok(*args)).strip()
        return _commit(out) if out else None

    async def _description(self) -> str | None:
        if self.repo not in _descriptions:
            r = await GhCli().api(f"repos/{self.repo}")
            _descriptions[self.repo] = (r.data or {}).get("description") if r.ok else None
        return _descriptions[self.repo]

    async def summary(self, ref: str | None) -> dict[str, Any]:
        fetched = await self.sync()
        default = await self.mirror.default_branch()
        ref, commit = await self.resolve(ref)
        heads = (await self._ok("for-each-ref", "--format=%(refname)", "refs/heads")).split()
        size_kb = 0
        for line in (await self._ok("count-objects", "-v")).splitlines():
            key, _, value = line.partition(":")
            if key in ("size", "size-pack"):
                size_kb += int(value.strip() or 0)
        head = await self.last_commit(commit)
        return {
            "repo": self.repo,
            "description": await self._description(),
            "html_url": f"https://github.com/{self.repo}",
            "clone_url": f"https://github.com/{self.repo}.git",
            "ssh_url": f"git@github.com:{self.repo}.git",
            "default_branch": default,
            "ref": ref,
            "commit": commit,
            "branch_count": len(heads),
            "size_bytes": size_kb * 1024,
            "head": head,
            "fetched_at": fetched.isoformat(),
        }

    async def tree(self, ref: str | None, path: str = "") -> dict[str, Any]:
        await self.sync()
        path = check_path(path)
        ref, commit = await self.resolve(ref)
        target = f"{commit}:{path}" if path else commit
        r = await self._git("ls-tree", "-l", target)
        if not r.ok:
            raise BrowseError(404, f"No folder {path} in {self.repo}@{ref}.")
        entries: list[dict[str, Any]] = []
        for line in r.stdout.splitlines()[:MAX_ENTRIES]:
            meta, _, name = line.partition("\t")
            parts = meta.split()
            if len(parts) < 4:
                continue
            kind, size = parts[1], parts[3]
            full = f"{path}/{name}" if path else name
            entries.append({"name": name, "path": full, "type": kind,
                            "size": int(size) if size.isdigit() else None, "last_commit": None})
        gate = asyncio.Semaphore(LOG_CONCURRENCY)

        async def stamp(entry: dict[str, Any]) -> None:
            async with gate:
                entry["last_commit"] = await self.last_commit(commit, entry["path"])

        await asyncio.gather(*(stamp(e) for e in entries))
        entries.sort(key=lambda e: (e["type"] != "tree", e["name"].lower()))
        return {"ref": ref, "commit": commit, "path": path, "entries": entries}

    async def blob(self, ref: str | None, path: str) -> dict[str, Any]:
        await self.sync()
        path = check_path(path)
        if not path:
            raise BrowseError(400, "Name a file.")
        ref, commit = await self.resolve(ref)
        kind = await self._git("cat-file", "-t", f"{commit}:{path}")
        if not kind.ok or kind.stdout.strip() != "blob":
            raise BrowseError(404, f"No file {path} in {self.repo}@{ref}.")
        size = int((await self._ok("cat-file", "-s", f"{commit}:{path}")).strip() or 0)
        text = (await self._ok("cat-file", "blob", f"{commit}:{path}")) if size <= 8 * MAX_BLOB else ""
        binary = "\x00" in text[:8000] or (size > 8 * MAX_BLOB)
        truncated = not binary and len(text) > MAX_BLOB
        return {"ref": ref, "commit": commit, "path": path, "size": size, "binary": binary, "truncated": truncated,
                "text": None if binary else text[:MAX_BLOB], "last_commit": await self.last_commit(commit, path)}

    async def commits(self, ref: str | None, path: str = "", limit: int = 30, skip: int = 0) -> dict[str, Any]:
        await self.sync()
        path = check_path(path)
        ref, commit = await self.resolve(ref)
        limit = max(1, min(limit, 100))
        args = ["log", f"--format={COMMIT_FORMAT}", f"--max-count={limit + 1}", f"--skip={max(0, skip)}", commit]
        if path:
            args += ["--", path]
        rows = [_commit(line) for line in (await self._ok(*args)).splitlines() if line.strip()]
        return {"commits": rows[:limit], "has_more": len(rows) > limit}

    async def commit_detail(self, sha: str) -> dict[str, Any]:
        await self.sync()
        if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            raise BrowseError(400, "That is not a commit.")
        r = await self._git("show", "-s", f"--format={COMMIT_FORMAT}%x1f%P%x1f%b", sha)
        if not r.ok or not r.stdout.strip():
            raise BrowseError(404, f"No commit {sha} in {self.repo}.")
        fields = r.stdout.split(SEP)
        head = _commit(SEP.join(fields[:5]))
        head["parents"] = fields[5].split() if len(fields) > 5 else []
        head["body"] = SEP.join(fields[6:]).strip() if len(fields) > 6 else ""
        counts: dict[str, tuple[int, int]] = {}
        for line in (await self._ok("show", "--format=", "--numstat", "-M", "--no-color", head["sha"])).splitlines():
            add, dele, name = (line.split("\t", 2) + ["", "", ""])[:3]
            if name:
                counts[name] = (int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0)
        patch = await self._ok("show", "--format=", "--patch", "-M", "--no-color", "--no-ext-diff", head["sha"])
        return {"commit": head, "files": _split_patch(patch, counts)}

    async def branches(self) -> dict[str, Any]:
        await self.sync()
        default = await self.mirror.default_branch()
        fmt = "%(refname:short)%1f%(objectname)%1f%(objectname:short=7)%1f%(subject)%1f%(authorname)%1f%(committerdate:iso-strict)"
        out = await self._ok("for-each-ref", "--sort=-committerdate", f"--format={fmt}", "refs/heads")
        rows = []
        for line in out.splitlines():
            parts = line.split(SEP)
            if len(parts) < 6:
                continue
            name, sha, short, subject, author, date = parts[:6]
            rows.append({"name": name, "sha": sha, "is_default": name == default,
                         "last_commit": {"sha": sha, "short": short, "subject": subject, "author": author,
                                         "date": date}})
        rows.sort(key=lambda b: not b["is_default"])  # stable: default first, then newest
        return {"default": default, "branches": rows}


_DIFF_HEAD = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def _split_patch(patch: str, counts: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    """One entry per file of ``git show --patch`` output, with its hunks (from the first ``@@``)."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunks: list[str] = []
    size = 0

    def close() -> None:
        if current is not None:
            body = "\n".join(hunks)
            current["patch"] = body if hunks and not current.pop("_binary", False) and size <= MAX_PATCH else None
            current.pop("_binary", None)
            files.append(current)

    for line in patch.splitlines():
        m = _DIFF_HEAD.match(line)
        if m:
            close()
            new = m.group(2)
            current = {"path": new, "previous_path": None, "status": "modified", "additions": 0, "deletions": 0,
                       "patch": None, "_binary": False}
            hunks, size = [], 0
            continue
        if current is None:
            continue
        if not hunks:
            if line.startswith("new file mode"):
                current["status"] = "added"
            elif line.startswith("deleted file mode"):
                current["status"] = "deleted"
            elif line.startswith("rename from "):
                current["status"], current["previous_path"] = "renamed", line[len("rename from "):]
            elif line.startswith("Binary files"):
                current["_binary"] = True
            elif line.startswith("@@"):
                hunks.append(line)
            continue
        hunks.append(line)
        size += len(line) + 1
    close()
    for f in files:
        key = f["path"] if f["status"] != "renamed" else next(
            (k for k in counts if k.endswith(f["path"]) or "=>" in k), f["path"])
        f["additions"], f["deletions"] = counts.get(key, counts.get(f["path"], (0, 0)))
    return files


def watched_repos(store) -> dict[str, list]:
    """``owner/name`` -> its watches, for every repo with at least one watch."""
    out: dict[str, list] = {}
    for w in store.watches.values():
        out.setdefault(w.repo, []).append(w)
    return out

