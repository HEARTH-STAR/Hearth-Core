"""Results of caller-run tools, carried back into a live chat turn.

The Hearth web app offers the agent tools it runs itself (its Spaces and pages
live in its own database), as ``ChatSpec.tools``. When the agent calls one, the
caller sees the ``tool-call`` event on the chat stream, runs the tool, and posts
the output to ``POST /api/chat/{session}/tool-results`` keyed by the call's
``tool_use_id``; the adapter's tool handler is waiting here for it.

One bridge exists per session while a turn runs, which also makes a session
single-flight: a second turn for a busy session is refused. Nothing here names
an agent; the adapter feeds ``expect`` from its pre-tool hook (which knows the
call id) and ``take`` from the tool handler (which does not).
"""

from __future__ import annotations

import asyncio
from collections import deque

# Results posted before the handler asks for them (the caller can be faster than the agent's
# hook), capped so a misbehaving caller cannot grow memory without bound.
MAX_EARLY_RESULTS = 64


class SessionBusy(RuntimeError):
    """A chat turn is already running for this session."""


class ToolResultBridge:
    def __init__(self, timeout: float):
        self.timeout = timeout
        self._ids: dict[str, deque[str]] = {}
        self._results: dict[str, asyncio.Future[tuple[str, bool]]] = {}
        self._waiting: set[str] = set()
        self._finished: set[str] = set()
        self._closed = False

    def expect(self, tool: str, tool_use_id: str | None) -> None:
        """Record that the next call of ``tool`` has ``tool_use_id`` (called just before the tool runs)."""
        if tool_use_id:
            self._ids.setdefault(tool, deque()).append(tool_use_id)

    def take(self, tool: str) -> str | None:
        """The call id for the call of ``tool`` that is starting now."""
        queue = self._ids.get(tool)
        return queue.popleft() if queue else None

    def _future(self, tool_use_id: str) -> asyncio.Future[tuple[str, bool]]:
        future = self._results.get(tool_use_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._results[tool_use_id] = future
        return future

    def resolve(self, tool_use_id: str, output: str, is_error: bool = False) -> bool:
        """Deliver a result. False when the bridge is closed, the call already has one, or too many are early."""
        if self._closed or tool_use_id in self._finished:
            return False
        future = self._results.get(tool_use_id)
        if future is None and len(self._results) - len(self._waiting) >= MAX_EARLY_RESULTS:
            return False
        future = self._future(tool_use_id)
        if future.done():
            return False
        future.set_result((output, is_error))
        return True

    async def wait(self, tool_use_id: str) -> tuple[str, bool]:
        """The posted ``(output, is_error)`` for a call, or an error result after the timeout."""
        future = self._future(tool_use_id)
        self._waiting.add(tool_use_id)
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except TimeoutError:
            return f"The web app did not return a result within {self.timeout:g} seconds.", True
        finally:
            self._waiting.discard(tool_use_id)
            self._finished.add(tool_use_id)
            self._results.pop(tool_use_id, None)

    def close(self) -> None:
        self._closed = True
        for future in self._results.values():
            if not future.done():
                future.set_result(("The chat turn ended before the web app returned a result.", True))
        self._results.clear()
        self._ids.clear()


_active: dict[str, ToolResultBridge] = {}


def open_bridge(session: str, timeout: float) -> ToolResultBridge:
    if session in _active:
        raise SessionBusy(session)
    bridge = _active[session] = ToolResultBridge(timeout)
    return bridge


def get_bridge(session: str) -> ToolResultBridge | None:
    return _active.get(session)


def close_bridge(session: str, bridge: ToolResultBridge) -> None:
    bridge.close()
    if _active.get(session) is bridge:
        del _active[session]
