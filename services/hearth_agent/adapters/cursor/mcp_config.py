"""
Per-run Cursor project config, the counterpart of ``claude_code/mcp_config.py``.

``cursor-agent`` reads its permission rules from ``<workspace>/.cursor/cli.json`` and its MCP servers from
``<workspace>/.cursor/mcp.json`` (on top of the user-level ``~/.cursor/`` files), and ``--trust`` also
lets it run ``<workspace>/.cursor/hooks.json``. HEARTH's run config must be the whole config (what
``setting_sources: []`` does for claude_code), so for the length of one run :func:`project_config` moves the
workspace's own copies of those three files aside, writes HEARTH's, and restores the originals afterwards:
the workspace (a job's diff) carries no trace of the run.

The only MCP server HEARTH offers is ``client``, holding a chat turn's caller-run tools
(``mcp_tools.ClientToolRelay``). External MCP servers are not supported: a sandbox may reach only the
package registries and the LLM provider (TDD §6 Network).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.cursor.hooks import CLIENT_SERVER_NAME

MANAGED_FILES = ("cli.json", "mcp.json", "hooks.json")

__all__ = ["CLIENT_SERVER_NAME", "MANAGED_FILES", "project_config"]


def _write_private(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


@contextmanager
def project_config(
    workspace: Path, permissions: dict[str, list[str]], mcp_servers: dict[str, Any], *, stash_dir: Path,
) -> Iterator[Path]:
    """Install HEARTH's ``.cursor/cli.json`` (and ``mcp.json`` when ``mcp_servers``) for one run.

    ``stash_dir`` (the run's private folder, outside the workspace) holds the workspace's own files until
    they are put back. Refuses a ``.cursor`` that is a symlink or a file, which could point the writes out
    of the workspace.
    """
    cursor_dir = workspace / ".cursor"
    if cursor_dir.is_symlink() or (cursor_dir.exists() and not cursor_dir.is_dir()):
        raise ValueError(f"{cursor_dir} is not a plain folder; HEARTH will not write Cursor's config there")
    created = not cursor_dir.exists()
    cursor_dir.mkdir(exist_ok=True)
    stashed: list[str] = []
    try:
        for name in MANAGED_FILES:
            path = cursor_dir / name
            if path.exists() or path.is_symlink():
                shutil.move(str(path), str(stash_dir / name))
                stashed.append(name)
        _write_private(cursor_dir / "cli.json", {"permissions": permissions})
        if mcp_servers:
            _write_private(cursor_dir / "mcp.json", {"mcpServers": mcp_servers})
        yield cursor_dir
    finally:
        for name in MANAGED_FILES:
            path = cursor_dir / name
            if path.is_symlink() or path.is_file():
                path.unlink()
        for name in stashed:
            shutil.move(str(stash_dir / name), str(cursor_dir / name))
        if created:
            shutil.rmtree(cursor_dir, ignore_errors=True)
