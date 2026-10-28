"""
Tool-layer policy for Antigravity, the counterpart of ``claude_code/hooks.py``.

xo-space runs ``agy`` with ``--dangerously-skip-permissions``; HEARTH never does (that flag also switches
agy's hooks off). Instead, the same :class:`~services.hearth_agent.policy.ToolPolicy` that guards Claude Code
runs in agy's own ``PreToolUse`` hook, before every tool call:

- :func:`install_hook` registers ``policy_hook.py`` once in the user-level ``~/.gemini/config/hooks.json``
  (merged with whatever else is there; HEARTH's entry is found again by its script path);
- :func:`active_policy` writes the run's policy (edit scope, command allowlist, what the turn allows) where
  the hook finds it by workspace, for the length of the run;
- :func:`ensure_tool_permission` sets agy's ``toolPermission`` to ``always-proceed``: headless ``agy -p``
  cannot ask, and auto-denies a call no rule approves even when the hook allowed it. With it, a call runs
  unless HEARTH's hook (or an agy deny rule) refuses it — the posture claude_code has with its
  ``allowed_tools`` plus its ``PreToolUse`` hook;
- :func:`project_hooks_aside` sets the workspace's own ``.agents/hooks.json`` aside for the run, so the
  run config is HEARTH's alone (``setting_sources: []`` for claude_code).

A denial lands in the run's denials log; the adapter turns each line into a ``policy-denied`` event.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.antigravity.paths import hooks_file, settings_path
from services.hearth_agent.adapters.antigravity.policy_hook import policy_key
from services.hearth_agent.policy import ToolPolicy
from services.storage.paths import hearth_state_dir

HOOK_SCRIPT = Path(__file__).resolve().with_name("policy_hook.py")
HOOK_TIMEOUT_SECONDS = 30
TOOL_PERMISSION = "always-proceed"


def policies_dir() -> Path:
    return hearth_state_dir() / "antigravity" / "policies"


def hook_command() -> str:
    """The command line agy runs (a shell string in its hooks file)."""
    argv = [sys.executable, str(HOOK_SCRIPT), str(policies_dir())]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path} is not valid JSON; HEARTH will not overwrite it") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object; HEARTH will not overwrite it")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".hearth-tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_hook() -> bool:
    """Register HEARTH's PreToolUse hook (idempotent). True when the file changed."""
    path = hooks_file()
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    groups = [g for g in hooks.get("PreToolUse") or []
              if not any(HOOK_SCRIPT.name in str(h.get("command", "")) for h in (g.get("hooks") or [])
                         if isinstance(h, dict))]
    entry = {"matcher": ".*", "hooks": [{"type": "command", "command": hook_command(),
                                          "timeout": HOOK_TIMEOUT_SECONDS}]}
    updated = [*groups, entry]
    if hooks.get("PreToolUse") == updated:
        return False
    hooks["PreToolUse"] = updated
    _write_json(path, data)
    return True


def ensure_tool_permission(mode: str = TOOL_PERMISSION) -> bool:
    """Set agy's ``toolPermission`` (other settings kept). True when the file changed."""
    path = settings_path()
    data = _read_json(path)
    if data.get("toolPermission") == mode:
        return False
    data["toolPermission"] = mode
    _write_json(path, data)
    return True


@contextmanager
def active_policy(
    workspace: Path, policy: ToolPolicy, *, files: bool, shell: bool, denials_log: Path,
    extra_allowed_tools: list[str] | None = None,
) -> Iterator[Path]:
    """Publish the run's policy for the hook, and withdraw it afterwards."""
    directory = policies_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{policy_key(str(workspace))}.json"
    spec = {
        "workspace": str(workspace.resolve()),
        "allowed_edit_paths": list(policy.allowed_edit_paths),
        "allow_test_edits": policy.allow_test_edits,
        "allowed_binaries": sorted(policy.allowed_binaries),
        "files": files,
        "shell": shell,
        "extra_allowed_tools": list(extra_allowed_tools or []),
        "denials_log": str(denials_log),
    }
    _write_json(path, spec)
    try:
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def project_hooks_aside(workspace: Path, stash_dir: Path) -> Iterator[None]:
    """Move ``<workspace>/.agents/hooks.json`` aside for the run and put it back afterwards."""
    agents_dir = workspace / ".agents"
    if agents_dir.is_symlink():
        raise ValueError(f"{agents_dir} is a symlink; HEARTH will not run agy in this workspace")
    source = agents_dir / "hooks.json"
    stashed = stash_dir / "project-hooks.json"
    moved = source.exists() or source.is_symlink()
    if moved:
        shutil.move(str(source), str(stashed))
    try:
        yield
    finally:
        if source.is_symlink() or source.is_file():
            source.unlink()  # whatever was written there during the run is not kept
        if moved:
            shutil.move(str(stashed), str(source))


def read_denials(path: Path, offset: int) -> tuple[list[dict], int]:
    """Denials the hook logged since ``offset`` (bytes), and the new offset."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1  # only whole lines; the hook may be mid-write
    records = []
    for line in data[:end].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, offset + end
