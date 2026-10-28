"""
Sign Antigravity in to a Google account from the web app, inside the space.

agy has no ``login`` subcommand. A logged-out ``agy -p`` prints "Authentication required. Please visit the
URL to log in:" and a Google OAuth link, then waits for the authorization code on its terminal (xo-space's
Connect Antigravity, ``routes.py``). So, like ``claude_code/login.py``, the trigger runs under a PTY:

    start()                  run a throwaway ``agy -p`` → {session_id, auth_url, expires_in}
    submit(session_id, code) type the code the owner pastes back
    poll(session_id)         {status: pending | connected | failed | cancelled}
    cancel(session_id)
    status()                 login state from agy's token file, never the token
    logout()                 remove agy's token file (agy has no logout either)

Success is judged on agy's token file (``auth.has_usable_login``), as in xo-space: the moment it is usable
the throwaway run is stopped, so its prompt never costs a model call. The trigger runs in an empty folder
of its own under a read-only HEARTH policy (no edits, no commands), and without
``--dangerously-skip-permissions``. Needs a PTY, so it works inside a space (Linux) and refuses on Windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from services.hearth_agent.adapters.antigravity.auth import auth_method, has_usable_login, login_state
from services.hearth_agent.adapters.antigravity.cli import agy_binary
from services.hearth_agent.adapters.antigravity.hooks import active_policy, ensure_tool_permission, install_hook
from services.hearth_agent.adapters.antigravity.paths import token_path
from services.hearth_agent.policy import ToolPolicy

log = logging.getLogger(__name__)

try:  # POSIX only
    import fcntl
    import pty
    import struct
    import termios

    HAS_PTY = sys.platform != "win32"
except ImportError:  # pragma: no cover - Windows
    HAS_PTY = False

SESSION_TTL_SECONDS = 600
URL_TIMEOUT_SECONDS = 30
OUTCOME_TIMEOUT_SECONDS = 20
FINISHED_KEPT = 8
TRIGGER_PROMPT = "hello"


class LoginError(RuntimeError):
    """Caller-actionable: no PTY here, CLI missing, no sign-in link, empty code."""


class UnknownLogin(LoginError):
    """No running sign-in with that id (finished long ago, cancelled, or replaced)."""


# ── Terminal output → plain text ─────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[\x20-\x2f]+[\x30-\x7e]|\x1b[@-Z\\-_]")
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]*/o/oauth2/[^\s\"'<>\x00-\x1f]+")
_AUTH_URL_FALLBACK_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+redirect_uri=[^\s\"'<>\x00-\x1f]+")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x07", "")


def extract_auth_url(text: str) -> str | None:
    """The Google OAuth link agy prints, with a glued-on redraw cut off, or None."""
    match = _AUTH_URL_RE.search(text) or _AUTH_URL_FALLBACK_RE.search(text)
    if not match:
        return None
    raw = match.group(0)
    second = raw.find("https://", len("https://"))
    return raw[:second] if second != -1 else raw


def normalize_code(raw: str) -> str:
    """Accept the bare code, the redirect URL, or a ``code=…&state=…`` string → the code agy expects."""
    value = (raw or "").strip()
    if value.startswith(("http://", "https://")):
        return (parse_qs(urlparse(value).query).get("code") or [value])[0]
    if "code=" in value:
        return (parse_qs(value).get("code") or [value])[0]
    return value


# ── Session ───────────────────────────────────────────────────────────────────


@dataclass
class _Session:
    id: str
    process: asyncio.subprocess.Process
    master_fd: int | None
    workdir: Path
    policy: contextlib.ExitStack
    started: float = field(default_factory=time.monotonic)
    auth_url: str | None = None
    state: str = "pending"  # pending | connected | failed | cancelled
    error: str | None = None
    last_line: str | None = None
    url_or_exit: asyncio.Event = field(default_factory=asyncio.Event)

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.state, "session_id": self.id}
        if self.auth_url:
            out["auth_url"] = self.auth_url
        if self.error:
            out["error"] = self.error
        return out


_lock = asyncio.Lock()
_active: _Session | None = None
_finished: dict[str, dict[str, Any]] = {}


def _on_output(session: _Session, text: str) -> None:
    line = strip_ansi(text).strip()
    if not line:
        return
    session.last_line = line[:300]
    if session.auth_url is None and (url := extract_auth_url(line)):
        session.auth_url = url
        session.url_or_exit.set()


def _reader(session: _Session, fd: int, loop: asyncio.AbstractEventLoop) -> None:
    """Read the PTY in a plain thread (an asyncio reader dies with the request that started the login, and a
    full PTY buffer would then block agy). The URL line may come without a newline before the code prompt."""
    buf = b""
    try:
        while chunk := os.read(fd, 65536):
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                loop.call_soon_threadsafe(_on_output, session, raw.decode("utf-8", "replace"))
            if buf and session.auth_url is None and extract_auth_url(strip_ansi(buf.decode("utf-8", "replace"))):
                loop.call_soon_threadsafe(_on_output, session, buf.decode("utf-8", "replace"))
    except OSError:  # EIO once agy exits
        pass
    if buf:
        loop.call_soon_threadsafe(_on_output, session, buf.decode("utf-8", "replace"))
    loop.call_soon_threadsafe(_close, session)  # the reader owns the fd: a read never races a close


def _close(session: _Session) -> None:
    fd, session.master_fd = session.master_fd, None
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


def _kill(session: _Session) -> None:
    if session.process.returncode is not None:
        return
    try:
        os.killpg(session.process.pid, signal.SIGKILL)  # start_new_session made it a group leader
    except (ProcessLookupError, PermissionError, AttributeError):
        with contextlib.suppress(ProcessLookupError):
            session.process.kill()


def _finish(session: _Session) -> None:
    _finished[session.id] = session.view()
    while len(_finished) > FINISHED_KEPT:
        _finished.pop(next(iter(_finished)))
    session.policy.close()
    shutil.rmtree(session.workdir, ignore_errors=True)
    session.url_or_exit.set()


async def _watch(session: _Session) -> None:
    """The token file is the success signal; agy's exit settles everything else."""
    while session.process.returncode is None:
        if has_usable_login() and session.state == "pending":
            session.state = "connected"
            _kill(session)  # skip the throwaway prompt
        await asyncio.sleep(1.0)
        if _expired(session) and session.state == "pending":
            session.state, session.error = "failed", "This sign-in expired. Start a new one."
            _kill(session)
    await session.process.wait()
    if session.state == "pending":
        if has_usable_login():
            session.state = "connected"
        else:
            session.state = "failed"
            session.error = session.last_line or "Antigravity sign-in did not finish."
    _finish(session)


def _login_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(TERM="xterm-256color", BROWSER="true", DISPLAY="", AGY_CLI_DISABLE_AUTO_UPDATE="1")
    return env


async def _spawn() -> _Session:
    from services.hearth_agent.adapters.antigravity.adapter import AntigravityAdapter

    install_hook()
    ensure_tool_permission()
    workdir = Path(tempfile.mkdtemp(prefix="hearth-agy-login-"))
    policy = contextlib.ExitStack()
    policy.enter_context(active_policy(workdir, ToolPolicy(), files=False, shell=False,
                                       denials_log=workdir / "denials.jsonl"))
    master, slave = pty.openpty()
    try:
        # A wide terminal keeps the long URL on one line; no echo keeps the pasted code out of the output.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 1000, 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        adapter = AntigravityAdapter({})
        process = await asyncio.create_subprocess_exec(
            agy_binary(), "-p", TRIGGER_PROMPT, "--model", adapter._model_for(None), "--add-dir", str(workdir),
            stdin=slave, stdout=slave, stderr=slave, env=_login_env(), cwd=str(workdir), start_new_session=True,
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        policy.close()
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    os.close(slave)
    return _Session(id=uuid.uuid4().hex, process=process, master_fd=master, workdir=workdir, policy=policy)


def _current(session_id: str) -> _Session:
    if _active is None or _active.id != session_id:
        raise UnknownLogin("Unknown or expired Antigravity sign-in. Start a new one.")
    return _active


def _expired(session: _Session) -> bool:
    return time.monotonic() - session.started > SESSION_TTL_SECONDS


async def start() -> dict[str, Any]:
    """Start a login and return the Google link the owner opens. A running login is replaced."""
    global _active
    if has_usable_login():
        return {"session_id": None, "auth_url": None, "expires_in": 0, "status": "connected"}
    if not HAS_PTY:
        raise LoginError("Antigravity sign-in runs inside a Hearth space (a Linux container). "
                         "On this machine, run `agy` once in a terminal instead.")
    async with _lock:
        if _active is not None:
            _active.state = "cancelled"
            _kill(_active)
            _active = None
        try:
            session = await _spawn()
        except FileNotFoundError as exc:
            raise LoginError(f"Antigravity CLI not found ({agy_binary()}).") from exc
        _active = session
        threading.Thread(target=_reader, args=(session, session.master_fd, asyncio.get_running_loop()),
                         daemon=True, name=f"agy-login-{session.id[:8]}").start()
        asyncio.create_task(_watch(session))
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(session.url_or_exit.wait(), URL_TIMEOUT_SECONDS)
    if session.state == "connected":
        return {"session_id": session.id, "auth_url": None, "expires_in": 0, "status": "connected"}
    if session.auth_url is None:
        _kill(session)
        raise LoginError(session.error or session.last_line or "Antigravity did not print a sign-in link.")
    log.info("antigravity login session %s started", session.id)
    return {"session_id": session.id, "auth_url": session.auth_url, "expires_in": SESSION_TTL_SECONDS}


async def submit(session_id: str, code: str) -> dict[str, Any]:
    """Type the code Google showed; wait briefly for agy to accept it."""
    if session_id in _finished:
        return _finished[session_id]
    session = _current(session_id)
    value = normalize_code(code)
    if not value or any(ch in value for ch in "\r\n\x00"):
        raise LoginError("Paste the code Google showed after you approved.")
    if session.state != "pending" or session.master_fd is None:
        return session.view()
    os.write(session.master_fd, value.encode("utf-8") + b"\r")
    deadline = time.monotonic() + OUTCOME_TIMEOUT_SECONDS
    while session.state == "pending" and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    return session.view()


async def poll(session_id: str) -> dict[str, Any]:
    if session_id in _finished:
        return _finished[session_id]
    return _current(session_id).view()


async def cancel(session_id: str) -> dict[str, Any]:
    global _active
    async with _lock:
        if _active is not None and _active.id == session_id:
            if _active.state == "pending":
                _active.state = "cancelled"
            _kill(_active)
            _active = None
    return {"status": "cancelled", "session_id": session_id}


# ── Status and logout ─────────────────────────────────────────────────────────


def describe(state: str, method: str | None = None) -> dict[str, Any]:
    """agy's login state → what the web app shows. Never includes a credential."""
    if state != "ok":
        return {"status": "needs_auth", "reason": state}
    return {"status": "connected", "method": method or "google"}


async def status() -> dict[str, Any]:
    if shutil.which(agy_binary()) is None and not os.path.isfile(agy_binary()):
        return {"status": "unavailable", "error": f"Antigravity CLI not found ({agy_binary()})",
                "login_supported": HAS_PTY}
    view = describe(login_state(), auth_method())
    view["login_supported"] = HAS_PTY
    return view


async def logout() -> dict[str, Any]:
    """Sign agy out of this space: its credential is the token file, so remove it."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LoginError(f"could not remove the Antigravity login: {exc}") from exc
    return await status()
