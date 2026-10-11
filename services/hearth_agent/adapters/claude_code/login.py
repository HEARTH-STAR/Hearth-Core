"""
Sign Claude Code in to a Claude account from the web app, inside the space.

Drives ``claude auth login`` the way xo-space's Connect Claude does
(``routers/auth/claude_setup_token.py``): the CLI runs under a pseudo-terminal
(its code prompt ignores a plain stdin pipe), prints an authorize URL, and waits
for the ``code#state`` that Claude's page shows once the owner approves. On
success the CLI writes its own self-refreshing ``~/.claude/.credentials.json``;
nothing here stores or logs a credential, and terminal echo is off so the pasted
code never comes back through the PTY.

    start()                  spawn the CLI → {session_id, auth_url, expires_in}
    submit(session_id, code) send the pasted code, wait briefly for the outcome
    poll(session_id)         {status: pending | connected | invalid_code | failed | cancelled}
    cancel(session_id)
    status()                 what ``claude auth status --json`` reports, minus credentials
    logout()

xo-space streams the same flow over SSE; HEARTH polls instead, like the GitHub CLI
connector. One login runs at a time. A PTY needs a POSIX system, which a space
(a Linux container) is; elsewhere ``start`` refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from services.hearth_agent.adapters.claude_code.cli import claude_binary
from services.hearth_agent.adapters.cli_status import CliStatusError, run_cli

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
# Env tokens the CLI weighs above a native login; a login must not be steered by them.
TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class LoginError(RuntimeError):
    """Caller-actionable: no PTY here, CLI missing, no sign-in link, empty code."""


class UnknownLogin(LoginError):
    """No running sign-in with that id (finished long ago, cancelled, or replaced)."""


# ── Terminal output → plain text (from xo-space) ──────────────────────────────

_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_2CHAR_RE = re.compile(r"\x1b[@-Z\\-_]")
_ANSI_NF_RE = re.compile(r"\x1b[\x20-\x2f]+[\x30-\x7e]")
_VISIT_URL_RE = re.compile(r"visit:\s*(https://[^\s\"'<>\x00-\x1f]+)", re.IGNORECASE)
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+/oauth/authorize\?[^\s\"'<>\x00-\x1f]+")


def strip_ansi(line: str) -> str:
    """Remove ANSI/OSC escape sequences (and stray BEL) from CLI output."""
    for pattern in (_ANSI_OSC_RE, _ANSI_CSI_RE, _ANSI_NF_RE, _ANSI_2CHAR_RE):
        line = pattern.sub("", line)
    return line.replace("\x07", "")


def extract_auth_url(text: str) -> str | None:
    """The OAuth authorize URL the CLI prints ("…visit: <url>"), cleaned and de-duplicated, or None."""
    match = _VISIT_URL_RE.search(text)
    raw = match.group(1) if match else None
    if raw is None:
        fallback = _AUTH_URL_RE.search(text)
        raw = fallback.group(0) if fallback else None
    if not raw:
        return None
    second = raw.find("https://", len("https://"))  # a redraw glued onto the first copy
    if second != -1:
        raw = raw[:second]
    split = urlsplit(raw)
    params: dict[str, str] = {}
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        if key and key not in params:
            params[key] = "".join(ch for ch in value if ord(ch) >= 0x20)
    if not params:
        return None
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), ""))


def normalize_code(raw: str) -> str:
    """Accept what Claude's page shows (``code#state``), a redirect URL, or a query string → ``code#state``."""
    value = (raw or "").strip()
    if value.startswith(("http://", "https://")):
        params = parse_qs(urlparse(value).query)
    elif "code=" in value and "state=" in value:
        params = parse_qs(value)
    else:
        return value
    code = (params.get("code") or [""])[0]
    state = (params.get("state") or [""])[0]
    return f"{code}#{state}" if code and state else code or value


# ── Session ───────────────────────────────────────────────────────────────────


@dataclass
class _Session:
    id: str
    process: asyncio.subprocess.Process
    master_fd: int | None
    started: float = field(default_factory=time.monotonic)
    auth_url: str | None = None
    state: str = "pending"  # pending | connected | invalid_code | failed | cancelled
    error: str | None = None
    failure_line: str | None = None
    exited: bool = False
    url_or_exit: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)  # the reader handed over every line
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.state, "session_id": self.id}
        if self.auth_url:
            out["auth_url"] = self.auth_url
        if self.error:
            out["error"] = self.error
        return out


_lock = asyncio.Lock()
_active: _Session | None = None
_finished: dict[str, dict[str, Any]] = {}  # last outcome per session, for a late poll


def _connected(session: _Session) -> None:
    if session.state != "connected":
        session.state, session.error = "connected", None
        asyncio.get_running_loop().run_in_executor(None, _mark_onboarding_complete)


def _on_output(session: _Session, text: str) -> None:
    line = strip_ansi(text).strip()
    if not line:
        return
    if session.auth_url is None:
        url = extract_auth_url(line)
        if url:
            session.auth_url = url
            session.url_or_exit.set()
    if "Login successful" in line:
        _connected(session)
    elif "Invalid code" in line and session.state == "pending":
        session.state = "invalid_code"
        session.error = line[line.find("Invalid code"):]
    elif "Login failed" in line:
        session.failure_line = line[line.find("Login failed"):]
    session.changed.set()


def _reader(session: _Session, fd: int, loop: asyncio.AbstractEventLoop) -> None:
    """Read the PTY in a plain thread: an asyncio reader stops when the request that started the
    login ends, and a full PTY buffer would then block the CLI (xo-space learned this). The thread
    owns the fd's lifetime: it reads until the CLI's side closes, then has the loop close it, so a
    read never races a close."""
    buf = b""
    try:
        while chunk := os.read(fd, 65536):
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                loop.call_soon_threadsafe(_on_output, session, raw.decode("utf-8", "replace"))
            # The code prompt has no newline; surface the URL line before it does.
            if buf and session.auth_url is None and extract_auth_url(strip_ansi(buf.decode("utf-8", "replace"))):
                loop.call_soon_threadsafe(_on_output, session, buf.decode("utf-8", "replace"))
    except OSError:  # EIO once the CLI exits
        pass
    if buf:
        loop.call_soon_threadsafe(_on_output, session, buf.decode("utf-8", "replace"))
    loop.call_soon_threadsafe(_close, session)


def _close(session: _Session) -> None:
    fd, session.master_fd = session.master_fd, None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    session.drained.set()


def _mark_onboarding_complete() -> None:
    """Record ``hasCompletedOnboarding`` in ``~/.claude.json`` (from xo-space): ``claude auth login``
    writes only the credentials, and without the flag a terminal ``claude`` in the space still walks
    its first-run screens. Existing keys are kept; an unreadable file is left alone."""
    path = os.path.expanduser("~/.claude.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict) or data.get("hasCompletedOnboarding") is True:
        return
    data["hasCompletedOnboarding"] = True
    tmp = f"{path}.hearth.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


async def _watch(session: _Session) -> None:
    code = await session.process.wait()
    try:  # judge the outcome on the CLI's last words ("Login failed: …"), which may still be in flight
        await asyncio.wait_for(session.drained.wait(), 2)
    except TimeoutError:
        pass
    session.exited = True
    if code == 0:
        _connected(session)
    elif session.state not in ("connected", "cancelled"):
        session.state = "failed"
        session.error = session.failure_line or session.error or "Claude Code sign-in did not finish."
    _finished[session.id] = session.view()
    while len(_finished) > FINISHED_KEPT:
        _finished.pop(next(iter(_finished)))
    session.url_or_exit.set()
    session.changed.set()


def _kill(session: _Session) -> None:
    """Stop the CLI and anything it started; the reader then sees EOF and closes the PTY."""
    if session.process.returncode is not None:
        return
    try:
        os.killpg(session.process.pid, signal.SIGKILL)  # start_new_session made it a group leader
    except (ProcessLookupError, PermissionError, AttributeError):
        try:
            session.process.kill()
        except ProcessLookupError:
            pass


def _login_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in TOKEN_VARS:
        env.pop(key, None)
    env.setdefault("TERM", "xterm-256color")
    return env


async def _spawn() -> _Session:
    master, slave = pty.openpty()
    try:
        # A wide terminal keeps the long URL on one line; no echo keeps the pasted code out of the output.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 1000, 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        process = await asyncio.create_subprocess_exec(
            claude_binary(), "auth", "login", "--claudeai",
            stdin=slave, stdout=slave, stderr=slave, env=_login_env(), start_new_session=True,
        )
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    return _Session(id=uuid.uuid4().hex, process=process, master_fd=master)


def _current(session_id: str) -> _Session:
    if _active is None or _active.id != session_id:
        raise UnknownLogin("Unknown or expired Claude Code sign-in. Start a new one.")
    return _active


def _expired(session: _Session) -> bool:
    return time.monotonic() - session.started > SESSION_TTL_SECONDS


async def start() -> dict[str, Any]:
    """Start a login and return its authorize URL. A running login is replaced."""
    global _active
    if not HAS_PTY:
        raise LoginError(
            "Claude Code sign-in runs inside a Hearth space (a Linux container). "
            "On this machine, run `claude auth login` in a terminal instead."
        )
    async with _lock:
        if _active is not None:
            _kill(_active)
            _active = None
        try:
            session = await _spawn()
        except FileNotFoundError as exc:
            raise LoginError(f"Claude Code CLI not found ({claude_binary()}).") from exc
        _active = session
        threading.Thread(target=_reader, args=(session, session.master_fd, asyncio.get_running_loop()),
                         daemon=True, name=f"claude-login-{session.id[:8]}").start()
        asyncio.create_task(_watch(session))
    try:
        await asyncio.wait_for(session.url_or_exit.wait(), URL_TIMEOUT_SECONDS)
    except TimeoutError:
        pass
    if session.auth_url is None:
        _kill(session)
        raise LoginError(session.failure_line or "Claude Code did not print a sign-in link.")
    log.info("claude login session %s started", session.id)
    return {"session_id": session.id, "auth_url": session.auth_url, "expires_in": SESSION_TTL_SECONDS}


async def submit(session_id: str, code: str) -> dict[str, Any]:
    """Send the code Claude's page showed; wait for the CLI to accept or reject it."""
    if session_id in _finished:
        return _finished[session_id]
    session = _current(session_id)
    if _expired(session):
        _kill(session)
        raise UnknownLogin("This sign-in expired. Start a new one.")
    value = normalize_code(code)
    if not value or any(ch in value for ch in "\r\n\x00"):
        raise LoginError("Paste the code Claude showed after you approved.")
    if session.state not in ("pending", "invalid_code") or session.master_fd is None:
        return session.view()
    session.state, session.error = "pending", None
    # Written from the loop thread, like the close in _reader, so the fd is never closed mid-write.
    # A code is far below the PTY's buffer, so this does not block.
    os.write(session.master_fd, value.encode("utf-8") + b"\r")
    deadline = time.monotonic() + OUTCOME_TIMEOUT_SECONDS
    while session.state == "pending" and not session.exited:
        session.changed.clear()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(session.changed.wait(), remaining)
        except TimeoutError:
            break
    return session.view()


async def poll(session_id: str) -> dict[str, Any]:
    if session_id in _finished:
        return _finished[session_id]
    session = _current(session_id)
    if _expired(session):
        _kill(session)
        return {"status": "failed", "session_id": session_id, "error": "This sign-in expired. Start a new one."}
    return session.view()


async def cancel(session_id: str) -> dict[str, Any]:
    global _active
    async with _lock:
        if _active is not None and _active.id == session_id:
            if _active.state != "connected":
                _active.state, _active.error = "cancelled", None
            _kill(_active)
            _active = None
    return {"status": "cancelled", "session_id": session_id}


# ── Status and logout ─────────────────────────────────────────────────────────


def describe(auth: dict[str, Any]) -> dict[str, Any]:
    """``claude auth status --json`` → what the web app shows. Never includes a credential."""
    if not auth.get("loggedIn"):
        return {"status": "needs_auth"}
    method = auth.get("authMethod") or ("api_key" if auth.get("apiKeySource") else None)
    return {
        "status": "connected",
        "method": method,
        "email": auth.get("email"),
        "organization": auth.get("orgName"),
        "subscription": auth.get("subscriptionType"),
    }


async def status() -> dict[str, Any]:
    """Whether Claude Code here can run turns, judged with the env the turns run with."""
    from services.hearth_agent.adapters.claude_code.adapter import ClaudeCodeAdapter

    try:
        res = await run_cli(claude_binary(), ["auth", "status", "--json"], timeout=15, label="claude",
                            env=ClaudeCodeAdapter({}).cli_env())
    except CliStatusError as exc:
        return {"status": "unavailable", "error": str(exc), "login_supported": HAS_PTY}
    try:
        payload = json.loads(res.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    view = describe(payload if isinstance(payload, dict) else {})
    view["login_supported"] = HAS_PTY
    return view


async def logout() -> dict[str, Any]:
    try:
        res = await run_cli(claude_binary(), ["auth", "logout"], timeout=15, label="claude",
                            env={key: "" for key in TOKEN_VARS})
    except CliStatusError as exc:
        raise LoginError(str(exc)) from exc
    if res.returncode != 0:
        log.warning("claude auth logout exited %s", res.returncode)
    return await status()
