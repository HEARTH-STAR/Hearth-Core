"""
GitHub connector — `gh auth login` (CLI device-flow) acquisition.

Spawns `gh auth login --web` as a subprocess, parses the one-time device code
from its output, and waits asynchronously for the user to authorize on
github.com. Once `gh` exits successfully, gh holds the token; ``connect()`` reads
it once with `gh auth token` to validate it, then records metadata only.

This sits alongside the PAT flow (``pat.py``) — the two methods share the same
storage and validation (``common.py``); only the *acquisition* differs.

Differences from xo-space ``connectors/github/cli_auth.py``:
  - The token is never copied out of gh. xo-space exports it into token.json;
    HEARTH keeps gh as the only holder (TDD §6) and stores metadata.
  - A login whose token fails validation (e.g. an account with admin scopes) is
    logged out again, so no unusable credential is left behind.
  - Device-flow tokens are OAuth tokens that reach every repository the account
    can; the connection payload carries a warning recommending a dedicated
    machine user or a fine-grained PAT (PRD §5).

Caveats (same as xo-space):
  - In-memory session state. A worker restart drops in-progress logins.
  - Output parsing depends on `gh` CLI version 2.x output format.
  - At most one login session is active at a time (per process).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .common import (
    configure_git_identity,
    connection_payload,
    save_github_credential,
    sign_gh_out,
    sign_gh_out_of_other_accounts,
    validate_token,
)
from .gh_api import GhCli

log = logging.getLogger(__name__)

AUTH_METHOD = "cli"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GH_BIN = "gh"
GITHUB_HOSTNAME = "github.com"
VERIFICATION_URI = "https://github.com/login/device"

# Time we'll wait for `gh` to print the device code on startup.
DEVICE_CODE_TIMEOUT_SECONDS = 15

# How long a session may sit in "pending" before we treat it as expired.
# GitHub device codes expire in 15 min — match that.
SESSION_TTL_SECONDS = 15 * 60

# Matches gh's one-time code, e.g. "7B79-D4F8".
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")

DEVICE_FLOW_WARNING = (
    "Device login gives HEARTH an OAuth token that reaches every repository this account can. "
    "Use a dedicated machine user (e.g. hearth-bot), or a fine-grained PAT limited to selected repositories."
)


# ---------------------------------------------------------------------------
# Session state (in-memory, single-process)
# ---------------------------------------------------------------------------

@dataclass
class _Session:
    session_id: str
    process: asyncio.subprocess.Process
    user_code: str
    started_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | completed | failed | cancelled
    error: str | None = None
    # Keeps the background reader alive for the lifetime of the subprocess.
    drain_task: asyncio.Task | None = None


_active: dict[str, _Session] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh() -> GhCli:
    return GhCli(host=GITHUB_HOSTNAME, binary=GH_BIN)


def _gh_available() -> bool:
    return shutil.which(GH_BIN) is not None


def _login_argv() -> list[str]:
    # `--insecure-storage`: the space container has no OS keyring, so gh keeps the
    # token in its own config file under the engine user's home on the space volume.
    # That file is gh's store, the only place the token lives.
    # No `--skip-ssh-key`: gh offers an SSH key only for the ssh protocol, and the
    # flag does not exist before gh 2.49 (Ubuntu's gh, in the space image, is 2.46),
    # where it makes `gh auth login` exit with "unknown flag" before any device code.
    return [
        GH_BIN, "auth", "login",
        "--web",
        "--hostname", GITHUB_HOSTNAME,
        "--git-protocol", "https",
        "--insecure-storage",
    ]


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _evict_stale_locked() -> None:
    """Drop sessions older than SESSION_TTL_SECONDS. Caller holds _lock."""
    now = time.time()
    for sid in [sid for sid, s in _active.items() if now - s.started_at > SESSION_TTL_SECONDS]:
        s = _active.pop(sid, None)
        if s:
            _kill(s.process)


async def _read_until_code(proc: asyncio.subprocess.Process) -> str:
    """
    Read merged stdout/stderr line-by-line until we find the device code.

    In non-TTY mode `gh` writes "First copy your one-time code: XXXX-XXXX", then a
    URL line, then polls silently. We return as soon as we see the code — the
    background drain task takes over from there.

    Raises RuntimeError if no code is found before the deadline.
    """
    deadline = time.time() + DEVICE_CODE_TIMEOUT_SECONDS
    assert proc.stdout is not None  # we asked for PIPE
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError("Timed out waiting for `gh auth login` to print a device code.")
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except TimeoutError:
            raise RuntimeError("Timed out waiting for `gh auth login` to print a device code.") from None
        if not line:
            raise RuntimeError("`gh auth login` exited before producing a device code.")
        m = _DEVICE_CODE_RE.search(line.decode("utf-8", errors="replace"))
        if m:
            return m.group(1)


async def _finalize_session(proc: asyncio.subprocess.Process, sid: str) -> None:
    """Record the outcome of `gh auth login` on the session, if still pending."""
    async with _lock:
        session = _active.get(sid)
        if not session or session.status != "pending":
            return  # unknown, or already finalized (e.g. cancelled)
        if proc.returncode == 0:
            session.status = "completed"
        else:
            session.status = "failed"
            session.error = f"`gh auth login` exited with status {proc.returncode}."


async def _drain_until_exit(proc: asyncio.subprocess.Process, sid: str) -> None:
    """Background task: drain output and update session status on exit."""
    try:
        if proc.stdout is not None:
            try:
                while await proc.stdout.readline():
                    pass
            except Exception:
                pass
        await proc.wait()
    finally:
        # No `return` here on purpose: a return inside `finally` would swallow the
        # CancelledError that cancel_login() delivers (PEP 765).
        await _finalize_session(proc, sid)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_login() -> dict[str, Any]:
    """
    Spawn `gh auth login --web`, parse the device code, and return the
    user-facing details. The subprocess keeps running in the background until
    the user authorizes on github.com (or the code expires).
    """
    if not _gh_available():
        raise RuntimeError(
            "GitHub CLI (`gh`) is not installed in this space. "
            "It ships in the space image; outside a space install it from https://cli.github.com/, "
            "or use the PAT method instead."
        )

    # Hold the lock for the whole start so two concurrent /cli/start calls can't
    # both spawn a `gh auth login` (which would clobber each other's state).
    async with _lock:
        _evict_stale_locked()
        if any(s.status == "pending" for s in _active.values()):
            raise RuntimeError(
                "A GitHub CLI login is already in progress. Cancel it first or wait for it to complete."
            )

        env = os.environ.copy()
        env["BROWSER"] = "true"  # never try to open a browser inside the space

        # `gh auth login` refuses a fresh device flow while an account is logged in.
        # Every account, named: a bare `gh auth logout` fails outright once gh holds
        # more than one. Errors are non-fatal.
        await sign_gh_out(_gh())

        proc = await asyncio.create_subprocess_exec(
            *_login_argv(),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            user_code = await _read_until_code(proc)
        except Exception:
            _kill(proc)
            await proc.wait()
            raise

        sid = uuid.uuid4().hex
        session = _Session(session_id=sid, process=proc, user_code=user_code)
        _active[sid] = session
        session.drain_task = asyncio.create_task(_drain_until_exit(proc, sid))

    log.info("gh auth login session %s started", sid)
    return {
        "session_id": sid,
        "user_code": user_code,
        "verification_uri": VERIFICATION_URI,
        "expires_in": SESSION_TTL_SECONDS,
    }


async def poll_login(session_id: str) -> dict[str, Any]:
    """
    Check the status of an in-progress login. Returns one of:
      - {"status": "pending",   "user_code": ..., "verification_uri": ...}
      - {"status": "completed"}                            (consume once)
      - {"status": "failed",    "error": ...}
      - {"status": "not_found"}                            (unknown session_id)
    """
    async with _lock:
        _evict_stale_locked()
        session = _active.get(session_id)
        if not session:
            return {"status": "not_found"}
        if session.status == "pending":
            return {"status": "pending", "user_code": session.user_code, "verification_uri": VERIFICATION_URI}
        # Terminal state — pop so subsequent polls return not_found.
        _active.pop(session_id, None)
        if session.status == "completed":
            return {"status": "completed"}
        return {"status": session.status, "error": session.error or "Login failed."}


async def connect(session_id: str) -> dict[str, Any]:
    """
    Poll a login session and, once gh holds a token, validate it and record metadata.

    Mirrors ``pat.connect``, so both flows converge on the same stored entry and
    the same response body. Returns:
        {"ok": True,  "payload": <connection body>}      login finished
        {"ok": False, "status": "pending", ...}          user hasn't authorized yet
        {"ok": False, "status": "not_found"}             unknown/expired session
        {"ok": False, "status": "failed", "error": ...}  login or validation failed
    """
    result = await poll_login(session_id)
    if result.get("status") != "completed":
        return {"ok": False, **result}

    gh = _gh()
    token = await gh.auth_token()  # read once for validation; never stored by HEARTH
    if not token:
        return {"ok": False, "status": "failed", "error": "Login succeeded but `gh auth token` returned no token."}

    validation = await validate_token(token)
    del token
    if not validation.get("valid"):
        await sign_gh_out(gh)  # don't leave an unusable credential in gh
        return {
            "ok": False,
            "status": "failed",
            "error": validation.get("error", "GitHub CLI login completed but the token failed validation."),
        }

    validation["warnings"] = [*validation.get("warnings", []), DEVICE_FLOW_WARNING]
    await sign_gh_out_of_other_accounts(gh)
    save_github_credential(validation, auth_method=AUTH_METHOD)
    # This flow leaves a live gh session behind; let git use it for HTTPS pushes.
    setup = await gh.setup_git()
    if not setup.ok:
        log.warning("`gh auth setup-git` failed: %s", setup.message)
    await configure_git_identity(validation)
    log.info("GitHub connected as @%s (via gh CLI device flow)", validation.get("username"))
    return {"ok": True, "payload": connection_payload(validation, AUTH_METHOD)}


async def cancel_login(session_id: str) -> dict[str, Any]:
    """Kill an in-progress login and forget the session."""
    async with _lock:
        session = _active.pop(session_id, None)
    if not session:
        return {"status": "not_found"}
    session.status = "cancelled"
    _kill(session.process)
    try:
        await asyncio.wait_for(session.process.wait(), timeout=5)
    except TimeoutError:
        log.warning("gh subprocess for session %s did not exit promptly", session_id)
    if session.drain_task is not None:
        session.drain_task.cancel()
    return {"status": "cancelled"}
