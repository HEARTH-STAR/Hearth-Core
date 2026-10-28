"""
Sign Cursor in from the web app, inside the space.

``cursor-agent login`` prints a link (``NO_OPEN_BROWSER=1`` keeps it from opening a browser) and waits
while the owner approves it in their own browser; Cursor's servers then hand the CLI its credential,
which the CLI stores itself. There is no code to paste back, so unlike ``claude_code/login.py`` no PTY is
needed: the CLI runs on pipes (``utils.commands.spawn``) and HEARTH polls for it to exit.

    start()            spawn the CLI → {session_id, auth_url, expires_in}
    poll(session_id)   {status: pending | connected | failed | cancelled}
    cancel(session_id)
    status()           what ``cursor-agent status`` reports, minus credentials
    logout()

Nothing here stores, logs or returns a credential. The login runs without ``CURSOR_API_KEY``, so a key
in the environment cannot stand in for the account being connected. One login runs at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from services.hearth_agent.adapters.cli_status import CliStatusError, run_cli
from services.hearth_agent.adapters.cursor.cli import cursor_binary
from services.hearth_agent.adapters.cursor.models_status import fetch_status, strip_ansi
from utils.commands import Spawned, spawn

log = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 600
URL_TIMEOUT_SECONDS = 30
FINISHED_KEPT = 8
TOKEN_VARS = ("CURSOR_API_KEY",)

_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]*cursor\.(?:com|sh)[^\s\"'<>\x00-\x1f]*")


class LoginError(RuntimeError):
    """Caller-actionable: CLI missing, no sign-in link."""


class UnknownLogin(LoginError):
    """No running sign-in with that id (finished long ago, cancelled, or replaced)."""


def extract_auth_url(text: str) -> str | None:
    match = _URL_RE.search(strip_ansi(text))
    return match.group(0).rstrip(".,)") if match else None


@dataclass
class _Session:
    id: str
    process: Spawned
    started: float = field(default_factory=time.monotonic)
    auth_url: str | None = None
    state: str = "pending"  # pending | connected | failed | cancelled
    error: str | None = None
    last_line: str | None = None
    url_or_exit: asyncio.Event = field(default_factory=asyncio.Event)
    reader: asyncio.Task | None = None

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.state, "session_id": self.id}
        if self.error:
            out["error"] = self.error
        return out


_active: _Session | None = None
_finished: dict[str, dict[str, Any]] = {}
_lock = asyncio.Lock()


def _remember(session: _Session) -> None:
    _finished[session.id] = session.view()
    while len(_finished) > FINISHED_KEPT:
        _finished.pop(next(iter(_finished)))


async def _read(session: _Session) -> None:
    """Follow the CLI's output for the link, then its exit for the outcome."""
    async for raw in session.process.lines():
        text = strip_ansi(raw.decode(errors="replace")).strip()
        if not text:
            continue
        session.last_line = text[:300]
        if session.auth_url is None and (url := extract_auth_url(text)):
            session.auth_url = url
            session.url_or_exit.set()
    code = await session.process.wait()
    if session.state == "pending":
        if code == 0:
            session.state = "connected"
        else:
            session.state = "failed"
            session.error = session.last_line or f"cursor-agent login exited with code {code}"
    session.url_or_exit.set()
    _remember(session)


def _kill(session: _Session) -> None:
    session.process.kill()


def login_command() -> list[str]:
    return [cursor_binary(), "login"]


def _expired(session: _Session) -> bool:
    return time.monotonic() - session.started > SESSION_TTL_SECONDS


async def start() -> dict[str, Any]:
    """Start a login and return the link the owner opens. A running login is replaced."""
    global _active
    async with _lock:
        if _active is not None and _active.state == "pending":
            _active.state = "cancelled"
            _kill(_active)
        argv = login_command()
        process = await spawn(argv, env={"NO_OPEN_BROWSER": "1"}, unset_env=TOKEN_VARS, merge_stderr=True)
        if process.binary_missing or process.exception:
            raise LoginError(f"Cursor CLI unavailable: {process.exception or argv[0]}")
        session = _Session(id=uuid.uuid4().hex, process=process)
        session.reader = asyncio.create_task(_read(session))
        _active = session
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(session.url_or_exit.wait(), URL_TIMEOUT_SECONDS)
    if session.auth_url is None:
        if session.state == "connected":  # already signed in: the CLI exited 0 at once
            return {"session_id": session.id, "auth_url": None, "expires_in": 0, "status": "connected"}
        _kill(session)
        raise LoginError(session.error or "cursor-agent login did not print a sign-in link")
    return {"session_id": session.id, "auth_url": session.auth_url, "expires_in": SESSION_TTL_SECONDS}


def _current(session_id: str) -> _Session:
    if _active is None or _active.id != session_id:
        raise UnknownLogin("No sign-in is running with that id. Start a new one.")
    return _active


async def poll(session_id: str) -> dict[str, Any]:
    if session_id in _finished:
        return _finished[session_id]
    session = _current(session_id)
    if _expired(session) and session.state == "pending":
        session.state, session.error = "failed", "This sign-in expired. Start a new one."
        _kill(session)
    return session.view()


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


def describe(parsed: dict[str, Any]) -> dict[str, Any]:
    """``models_status.parse_status`` output → what the web app shows. Never includes a credential."""
    if not parsed.get("logged_in"):
        return {"status": "needs_auth"}
    return {"status": "connected", "method": parsed.get("method"), "email": parsed.get("email")}


async def status() -> dict[str, Any]:
    """Whether Cursor here can run turns, judged with the env the turns run with."""
    try:
        parsed = await fetch_status()
    except CliStatusError as exc:
        return {"status": "unavailable", "error": str(exc), "login_supported": True}
    view = describe(parsed)
    view["login_supported"] = True
    return view


async def logout() -> dict[str, Any]:
    try:
        res = await run_cli(cursor_binary(), ["logout"], timeout=15, label="cursor-agent",
                            env={key: "" for key in TOKEN_VARS})
    except CliStatusError as exc:
        raise LoginError(str(exc)) from exc
    if res.returncode != 0:
        log.warning("cursor-agent logout exited %s", res.returncode)
    return await status()
