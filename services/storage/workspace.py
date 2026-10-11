"""The space workspace: the folder chat sessions work in, and safe reads of it.

``$HEARTH_WORKSPACE_DIR`` (default ``~/.hearth/workspace``). Inside a space container
it is ``/workspace``, where the template clones its repos. Every path a caller
names is relative to this root; absolute paths, ``..`` and symlinks that lead out
of the root are refused.

The Hearth web app gives each Dot its own folder here (its "computer") and reads
it through ``GET /api/workspace/files`` and ``GET /api/workspace/file``.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from services.storage.paths import hearth_state_dir

# Folders a listing never descends into: VCS internals, dependency trees, caches.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", ".turbo",
})
LIST_LIMIT = 5000
READ_LIMIT = 256 * 1024
_SNIFF_BYTES = 8192


class WorkspacePathError(ValueError):
    """A caller-supplied path is not a plain relative path inside the workspace."""


def workspace_root() -> Path:
    configured = os.environ.get("HEARTH_WORKSPACE_DIR")
    return Path(configured).expanduser() if configured else hearth_state_dir() / "workspace"


def _relative(path: str) -> PurePosixPath:
    if "\\" in path or "\0" in path:
        raise WorkspacePathError(f"use forward slashes and no NUL bytes: {path!r}")
    rel = PurePosixPath(path.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise WorkspacePathError(f"path must stay inside the workspace: {path!r}")
    return rel


def resolve(path: str) -> Path:
    """The absolute path for ``path`` (relative to the root), refusing anything that escapes it."""
    root = workspace_root().resolve()
    target = (root / _relative(path)).resolve()
    if target != root and root not in target.parents:
        raise WorkspacePathError(f"path leads outside the workspace: {path!r}")
    return target


def list_files(directory: str = "", *, limit: int = LIST_LIMIT) -> dict:
    """Every file under ``directory``, as POSIX paths relative to it.

    Empty folders appear with a trailing ``/`` so a tree can still show them.
    Symlinks are listed but never followed.
    """
    base = resolve(directory)
    rel = str(_relative(directory))
    out: dict = {"directory": "" if rel == "." else rel, "exists": base.is_dir(), "files": [], "truncated": False}
    if not out["exists"]:
        return out
    files: list[str] = []
    for current, dirs, names in os.walk(base, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel_dir = Path(current).relative_to(base).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        if prefix and not dirs and not names:
            files.append(prefix)
        for name in sorted(names):
            files.append(prefix + name)
        if len(files) >= limit:
            out["truncated"] = True
            break
    out["files"] = files[:limit]
    return out


def read_file(path: str, *, limit: int = READ_LIMIT) -> dict:
    """One file's text (up to ``limit`` bytes), or ``binary: True`` with no text."""
    target = resolve(path)
    if not target.is_file():
        raise FileNotFoundError(path)
    stat = target.stat()
    with target.open("rb") as fh:
        data = fh.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    binary = b"\0" in data[:_SNIFF_BYTES]
    text: str | None = None
    if not binary:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # A cut through a multi-byte character at the limit is still text.
            if truncated and exc.start >= len(data) - 3:
                text = data[: exc.start].decode("utf-8")
            else:
                binary = True
    return {"path": str(_relative(path)), "size": stat.st_size, "modifiedAt": int(stat.st_mtime * 1000),
            "binary": binary, "truncated": truncated, "text": text}
