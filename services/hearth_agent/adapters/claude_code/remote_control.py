"""claude_code Remote Control lifecycle — OFF by default in HEARTH.

Start / stop / inspect a local ``claude remote-control`` server so a Claude Code
session in this space can be driven from the Claude mobile app or claude.ai/code.
The session runs *here* (the space's filesystem); the phone/browser is only a
window into it.

Why it is gated: HEARTH's security model (TDD §6) is that agent work happens in
sandboxes under the tool-layer policy, with nothing outside the space driving it.
A Remote Control session is an interactive Claude Code session steered from
claude.ai, outside both. So ``start()`` refuses unless the space's
``config/agents/claude_code/capabilities.json`` sets ``remote_control.enabled``
to true — an explicit, reviewable operator decision. ``status()`` and ``stop()``
always work.

Design notes (from xo-space):
  * cwd is ``HEARTH_RC_DIR`` (default: the workspaces root, not HOME, so the
    session cannot reach the engine's state or secrets by default). Trust for that
    dir is seeded in ~/.claude.json.
  * Auth: native login only. Remote Control rejects CLAUDE_CODE_OAUTH_TOKEN / API
    keys, so the three token vars are always stripped and
    ~/.claude/.credentials.json is required.
  * Two non-interactive gates (trust + enable dialog) are pre-seeded.
  * Detached process tracked by a PID file; outlives API restarts; stop() kills the group.
  * start()/stop() are serialized by a file lock (fcntl; a thread lock on Windows dev hosts).
  * The CLI's live dashboard is not recorded; a filter extracts the connect link
    into URL_FILE and discards the rest.

This builds on undocumented CLI internals (the ~/.claude.json gate keys and the
dashboard link), because `claude remote-control` has no headless API. Pin the CLI
version and run a start→url→stop smoke test on upgrades.

Shape mirrors xo-space ``adapters/claude_code/remote_control.py``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.claude_code.cli import claude_binary
from services.hearth_agent.registry.settings import load_agent_capabilities
from services.storage.layout import workspaces_dir
from services.storage.paths import hearth_state_dir

try:  # POSIX (the space image); Windows dev hosts fall back to a process-local lock
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.Lock()

# Auth vars Remote Control can never use — always dropped so the CLI falls back
# to the native ~/.claude/.credentials.json login.
_TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

_LAUNCH_SCRIPT = r'''
"$RC_BIN" remote-control --name "$RC_NAME" 2>"$RC_ERR" \
| grep --line-buffered -aoE 'https://claude\.ai/code\?environment=env_[A-Za-z0-9]+' \
| while IFS= read -r u; do printf '%s\n' "$u" > "$RC_URL"; done
'''


def _state_dir() -> Path:
    d = hearth_state_dir() / "remote-control"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file() -> Path:
    return _state_dir() / "rc.pid"


def _url_file() -> Path:
    return _state_dir() / "rc.url"   # single line: the current connect link


def _err_file() -> Path:
    return _state_dir() / "rc.err"   # the CLI's stderr, for debugging a failed start


def _name_file() -> Path:
    return _state_dir() / "rc.name"


def _lock_file() -> Path:
    return _state_dir() / "rc.lock"


def _claude_home() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def _global_config() -> Path:
    return Path(os.path.expanduser("~/.claude.json"))


def remote_control_enabled() -> bool:
    """The operator's explicit opt-in: ``remote_control.enabled`` in capabilities.json."""
    rc = load_agent_capabilities("claude_code").get("remote_control")
    return bool(isinstance(rc, dict) and rc.get("enabled"))


def _launch_dir() -> Path:
    """``HEARTH_RC_DIR`` if it is an existing directory, else the workspaces root."""
    raw = (os.getenv("HEARTH_RC_DIR", "") or "").strip()
    d = Path(raw).expanduser() if raw else workspaces_dir()
    if not d.is_dir():
        d = workspaces_dir()
        d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


@contextmanager
def _lock() -> Iterator[None]:
    """Exclusive lock serializing start/stop across threads and processes."""
    with _THREAD_LOCK:
        if fcntl is None:
            yield
            return
        f = open(_lock_file(), "w")  # noqa: SIM115 - held for the lock's lifetime
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            finally:
                f.close()


# ── process helpers ──────────────────────────────────────────────────────────

def _read_pid() -> int | None:
    try:
        return int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live process; reaps it if it is our own zombie child."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if hasattr(os, "waitpid") and hasattr(os, "WNOHANG"):
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
            if reaped == pid:
                return False
        except (ChildProcessError, OSError):
            pass
    return True


def _running_pid() -> int | None:
    pid = _read_pid()
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def _session_url() -> str | None:
    try:
        url = _url_file().read_text().strip()
    except OSError:
        return None
    return url or None


def _cleanup_state() -> None:
    for f in (_pid_file(), _url_file(), _err_file(), _name_file()):
        try:
            f.unlink()
        except OSError:
            pass


def _kill_group(pid: int, sig: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(pid), sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, OSError):
            pass


# ── auth / gate helpers ──────────────────────────────────────────────────────

def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _TOKEN_VARS:
        env.pop(key, None)
    return env


def native_login_present() -> bool:
    """True when a native claude.ai OAuth session exists — the only credential Remote Control accepts."""
    for name in (".credentials.json", "credentials.json"):
        p = _claude_home() / name
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def ensure_gates_seeded() -> None:
    """Idempotently pre-clear the trust + enable dialogs in ~/.claude.json (atomic, only when missing)."""
    launch_dir = str(_launch_dir())
    config = _global_config()
    try:
        data = json.loads(config.read_text()) if config.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    projects = data.get("projects")
    proj_entry = projects.get(launch_dir) if isinstance(projects, dict) else None
    trust_ok = isinstance(proj_entry, dict) and proj_entry.get("hasTrustDialogAccepted") is True
    if trust_ok and data.get("remoteDialogSeen") is True:
        return

    data["remoteDialogSeen"] = True
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    entry = data["projects"].get(launch_dir)
    if not isinstance(entry, dict):
        entry = {}
    entry["hasTrustDialogAccepted"] = True
    data["projects"][launch_dir] = entry

    tmp = tempfile.NamedTemporaryFile("w", dir=str(config.parent), delete=False, suffix=".hearth-rc.tmp")
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, config)
    except OSError:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ── public API (start / stop / status) ───────────────────────────────────────

def _default_name() -> str:
    return (os.getenv("HEARTH_SPACE_NAME", "") or "").strip() or socket.gethostname()


def _session_name() -> str:
    try:
        n = _name_file().read_text().strip()
    except OSError:
        n = ""
    return n or _default_name()


def status() -> dict[str, Any]:
    login = native_login_present()
    enabled = remote_control_enabled()
    pid = _running_pid()
    if pid is None:
        return {"running": False, "enabled": enabled, "login_present": login, "session_url": None}
    return {
        "running": True,
        "enabled": enabled,
        "login_present": login,
        "pid": pid,
        "name": _session_name(),
        "session_url": _session_url(),
        "working_dir": str(_launch_dir()),
    }


def start(name: str | None = None) -> dict[str, Any]:
    """Launch the Remote Control server (idempotent, race-safe), if the operator enabled it."""
    with _lock():
        if not remote_control_enabled():
            return {
                "ok": False,
                "running": False,
                "enabled": False,
                "error": "disabled",
                "detail": ("Remote Control is off in HEARTH spaces. An operator can enable it by setting "
                           "remote_control.enabled to true in config/agents/claude_code/capabilities.json."),
            }
        if _running_pid() is not None:
            return {"ok": True, "already_running": True, **status()}
        if not native_login_present():
            return {
                "ok": False,
                "running": False,
                "login_present": False,
                "error": "no_native_login",
                "detail": "Remote Control needs a native claude.ai login (~/.claude/.credentials.json).",
            }

        label = (name or "").strip() or _default_name()
        try:
            ensure_gates_seeded()
            binary = claude_binary()
            _cleanup_state()
            env = _child_env()
            # Args/paths pass via env so the label can't break the shell.
            env["RC_BIN"] = binary
            env["RC_NAME"] = label
            env["RC_URL"] = str(_url_file())
            env["RC_ERR"] = str(_err_file())
            proc = subprocess.Popen(  # noqa: S603 - fixed argv; user input travels via env
                ["bash", "-c", _LAUNCH_SCRIPT],
                cwd=str(_launch_dir()),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _pid_file().write_text(str(proc.pid))
            _name_file().write_text(label)
        except Exception as exc:
            logger.exception("Remote Control start failed")
            _cleanup_state()
            return {"ok": False, "running": False, "error": "start_failed", "detail": str(exc)}

        logger.warning("Remote Control started (operator-enabled): pid=%s dir=%s name=%r",
                       proc.pid, _launch_dir(), label)
        return {"ok": True, "already_running": False, **status()}


def stop() -> dict[str, Any]:
    """Stop the Remote Control server (kills the whole process group). Idempotent; works even when disabled."""
    with _lock():
        pid = _read_pid()
        if pid is None:
            _cleanup_state()
            return {"ok": True, "running": False}
        if _pid_alive(pid):
            _kill_group(pid, signal.SIGTERM)
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                _kill_group(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        _cleanup_state()
        logger.info("Remote Control stopped: pid=%s", pid)
        return {"ok": True, "running": False}
