"""The single external-command executor.

Every subprocess HEARTH starts goes through :func:`run` (or :func:`spawn`, when
its output is read as it is written): an argv list (never a string, never
``shell=True``), stdin closed unless ``input`` is given so nothing can hang on a
prompt, a timeout that kills the whole process group (so grandchildren die too),
and secrets redacted from anything logged.

Shape mirrors xo-space ``utils/commands`` (``CommandResult``, ``run``,
``safe_arg``) so call sites read the same in both codebases.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one subprocess run.

    ``returncode`` is -1 when no process ran or its code is unknown (binary not
    found, a local exception). Check ``ok`` rather than testing for 0 directly
    when you want "finished cleanly".
    """

    argv: list[str]
    returncode: int
    output: str  # stdout (+ stderr merged unless separate_stderr was requested)
    duration_seconds: float
    timed_out: bool = False
    binary_missing: bool = False
    exception: str | None = None
    stderr: str = ""  # populated only when separate_stderr=True

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.binary_missing

    @property
    def stdout(self) -> str:
        """Alias for ``output``, so call sites ported from ``subprocess.run`` keep reading ``.stdout``."""
        return self.output


# ── Redaction ────────────────────────────────────────────────────────────────

_SENSITIVE_FLAGS = ("--token", "--api-key", "--password", "--secret", "--with-token")
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(authorization:\s*(?:basic|bearer)\s+)\S+"),
)


def redact(text: str) -> str:
    """Mask tokens and Authorization headers in free text."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


def redact_argv(argv: Sequence[str]) -> list[str]:
    out: list[str] = []
    hide_next = False
    for token in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if token in _SENSITIVE_FLAGS:
            hide_next = True
        elif any(token.startswith(f + "=") for f in _SENSITIVE_FLAGS):
            token = token.split("=", 1)[0] + "=***"
        out.append(redact(token))
    return out


# ── Argument safety ──────────────────────────────────────────────────────────


class CommandSpecError(ValueError):
    pass


def safe_arg(value: Any, *, allow_option: bool = False) -> str:
    """Refuse values that could be read as an option (argument injection) or carry a NUL byte."""
    text = str(value)
    if "\x00" in text:
        raise CommandSpecError("argument contains a NUL byte")
    if not allow_option and text.startswith("-"):
        raise CommandSpecError(f"argument may not start with '-': {text!r}")
    return text


# ── Execution ────────────────────────────────────────────────────────────────


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def run(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    unset_env: Sequence[str] = (),
    input: bytes | str | None = None,
    separate_stderr: bool = False,
) -> CommandResult:
    """Run a command asynchronously and return a :class:`CommandResult`. Never raises for process failures.

    argv:            command + args as a list — never a string (no shell).
    cwd:             working directory for the child process.
    timeout:         seconds before the process group is killed. ``None`` = no timeout.
    env:             environment overrides merged onto the parent's.
    unset_env:       variables removed from the child's environment (after ``env`` is merged).
    input:           written to the child's stdin; without it stdin is /dev/null.
    separate_stderr: keep stderr apart (``result.stderr``) instead of merging it into ``output``.

    Only the redacted argv is ever logged, never the output: some commands print a
    secret (``gh auth token``), and redaction cannot recognise every token shape
    (a classic GitHub PAT is 40 bare hex characters).
    """
    argv = [str(a) for a in argv]
    started = time.monotonic()
    log.debug("exec %s", redact_argv(argv))
    full_env = {**os.environ, **(env or {})}
    for key in unset_env:
        full_env.pop(key, None)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    kwargs: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}
    data = input.encode() if isinstance(input, str) else input
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if separate_stderr else asyncio.subprocess.STDOUT,
            **kwargs,
        )
    except FileNotFoundError:
        return CommandResult(argv, -1, f"binary not found: {argv[0]}", 0.0, binary_missing=True)
    except OSError as exc:
        return CommandResult(argv, -1, str(exc), 0.0, exception=str(exc))

    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=timeout)
    except TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        return CommandResult(argv, proc.returncode if proc.returncode is not None else -1, "timed out",
                             time.monotonic() - started, timed_out=True)
    return CommandResult(
        argv,
        proc.returncode if proc.returncode is not None else -1,
        (out or b"").decode(errors="replace"),
        time.monotonic() - started,
        stderr=(err or b"").decode(errors="replace") if separate_stderr else "",
    )


# ── Streaming ────────────────────────────────────────────────────────────────

# Agent CLIs print one JSON event per line, and one event can hold a whole file or command output.
STREAM_LINE_LIMIT = 32 * 1024 * 1024


class Spawned:
    """A running command whose stdout is read line by line as it is written (see :func:`spawn`).

    stderr is drained in the background (an unread pipe stops the child once it fills) and only its
    tail is kept. ``async with`` kills the whole process group on exit, so a reader that stops early
    never leaves the command running.
    """

    def __init__(self, argv: list[str], proc: asyncio.subprocess.Process | None, *,
                 binary_missing: bool = False, exception: str | None = None, stderr_limit: int = 8192):
        self.argv = argv
        self.process = proc
        self.binary_missing = binary_missing
        self.exception = exception
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._stderr_limit = stderr_limit
        self._drain: asyncio.Task | None = None
        if proc is not None and proc.stderr is not None:
            self._drain = asyncio.create_task(self._drain_stderr(proc.stderr))

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(4096):
            self._stderr.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr_size > self._stderr_limit and len(self._stderr) > 1:
                self._stderr_size -= len(self._stderr.popleft())

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process is not None else -1

    @property
    def stderr_tail(self) -> str:
        return b"".join(self._stderr).decode(errors="replace")[-self._stderr_limit:]

    async def lines(self) -> AsyncIterator[bytes]:
        """stdout, one line at a time (newline kept), until the command closes it."""
        if self.process is None or self.process.stdout is None:
            return
        while line := await self.process.stdout.readline():
            yield line

    async def wait(self) -> int:
        if self.process is None:
            return -1
        code = await self.process.wait()
        if self._drain is not None:
            await asyncio.gather(self._drain, return_exceptions=True)
        return code

    def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            _kill_tree(self.process)

    async def __aenter__(self) -> Spawned:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.kill()
        await self.wait()


async def spawn(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    unset_env: Sequence[str] = (),
    input: bytes | str | None = None,
    merge_stderr: bool = False,
    stderr_limit: int = 8192,
    user: str | None = None,
) -> Spawned:
    """Start a command and return it running, for output read as it is written. Never raises for process failures.

    The streaming counterpart of :func:`run`, with the same rules: an argv list, stdin closed unless
    ``input`` is given (then written and closed), its own process group, and only the redacted argv
    logged. ``merge_stderr`` folds stderr into the stdout lines; ``user`` runs the command as that
    user (POSIX). A command that could not start comes back with ``binary_missing`` or ``exception``
    set and no lines.
    """
    argv = [str(a) for a in argv]
    log.debug("spawn %s", redact_argv(argv))
    full_env = {**os.environ, **(env or {})}
    for key in unset_env:
        full_env.pop(key, None)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    kwargs: dict[str, Any] = {"start_new_session": True} if sys.platform != "win32" else {}
    if user and sys.platform != "win32":
        kwargs["user"] = user
    data = input.encode() if isinstance(input, str) else input
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
            limit=STREAM_LINE_LIMIT,
            **kwargs,
        )
    except FileNotFoundError:
        return Spawned(argv, None, binary_missing=True)
    except OSError as exc:
        return Spawned(argv, None, exception=str(exc))
    if data is not None and proc.stdin is not None:
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
    return Spawned(argv, proc, stderr_limit=stderr_limit)
