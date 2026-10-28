from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.base import BaseAgentAdapter, HarnessHooks
from services.hearth_agent.adapters.cli_status import CliStatusError, run_cli
from services.hearth_agent.adapters.cursor.cli import cursor_binary
from services.hearth_agent.adapters.cursor.hooks import check_tool, permission_rules
from services.hearth_agent.adapters.cursor.mcp_config import CLIENT_SERVER_NAME, project_config
from services.hearth_agent.adapters.cursor.mcp_tools import ClientToolRelay
from services.hearth_agent.adapters.cursor.paths import find_chat, global_mcp_servers
from services.hearth_agent.adapters.cursor.prompts import (
    CHAT_RULES,
    MIGRATOR_RULES,
    build_fixup_message,
    build_task_message,
    build_turn_prompt,
    parse_attention,
)
from services.hearth_agent.adapters.cursor.streaming import CursorStream
from services.hearth_agent.engine import sessions_io as _session_index
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import AgentResult, ChatSpec, ChatTool, JobSpec, Usage, ValidationResult
from services.hearth_agent.policy import ToolPolicy
from utils.commands import safe_arg, spawn

log = logging.getLogger(__name__)

# Fallbacks when manifest.json leaves a field out.
_DEFAULT_MODEL = "auto"  # let Cursor pick: no --model flag
_BACKEND = "cursor"
_FORCE_MODES = {"edit_turns", "never"}

Emit = Callable[[dict], Awaitable[None]]


# ── Session index (as in claude_code/adapter.py; one row per job or chat) ─────
# Cursor cannot pre-allocate a chat id, so a row starts with an empty
# ``nativeSessionId`` and gets it the moment the stream's system/init event names it.
# Rows of another backend under the same key (the space switched agents) are not ours.

_native_map: dict[str, str] = {}


def make_session_key(job_id: str) -> str:
    return job_id


def make_chat_key(session: str) -> str:
    """Index key for a web-app conversation; the same key claude_code uses, told apart by ``backend``."""
    return f"chat:{session}"


def _own_row(session_key: str) -> dict | None:
    row = _session_index.read_session_row(session_key)
    return row if row and row.get("backend") == _BACKEND else None


def _mark_running(session_key: str) -> None:
    row = _own_row(session_key)
    if row:
        row.update(status="running", updatedAt=_session_index.now_ms())
        _session_index.write_session_row(session_key, row)


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    native = (_own_row(session_key) or {}).get("nativeSessionId")
    if native:
        _native_map[session_key] = native
        return native
    return None


def get_session_directory(session_key: str) -> str | None:
    row = _own_row(session_key)
    return row.get("directory") if row else None


def write_preliminary_entry(
    session_key: str, session_id: str, cwd: str, *, model: str | None = None, resumed_from: str | None = None,
) -> None:
    """Write the row before the CLI starts. Messages are not stored here; they live in Cursor's own chat store."""
    now = _session_index.now_ms()
    _native_map.pop(session_key, None)
    _session_index.write_session_row(session_key, {
        "sessionId": session_id,
        "jobId": session_id,
        "nativeSessionId": "",
        "directory": cwd,
        "directoryHistory": [{"directory": cwd, "selectedAt": now}],
        "backend": _BACKEND,
        "model": model,
        "resumedFrom": resumed_from,
        "status": "running",
        "createdAt": now,
        "updatedAt": now,
        "usage": _session_index.empty_usage(),
        "costUsd": None,
        "numTurns": None,
    })


def _patch_native_session_id(session_key: str, native_sid: str) -> bool:
    """Record the chat id the first time it is seen (idempotent; never clobbers a different id)."""
    row = _own_row(session_key)
    if not row or not native_sid:
        return False
    existing = row.get("nativeSessionId") or ""
    if existing == native_sid:
        _native_map[session_key] = native_sid
        return True
    if existing:
        return False
    row.update(nativeSessionId=native_sid, updatedAt=_session_index.now_ms())
    _session_index.write_session_row(session_key, row)
    _native_map[session_key] = native_sid
    return True


def _roll_up(session_key: str, native_sid: str | None, num_turns: int | None, status: str) -> None:
    """Record the final status on the row. Cursor reports no tokens or cost, so those stay empty."""
    row = _own_row(session_key)
    if not row:
        return
    if native_sid and not row.get("nativeSessionId"):
        row["nativeSessionId"] = native_sid
    row.update(numTurns=num_turns, status=status, updatedAt=_session_index.now_ms())
    _session_index.write_session_row(session_key, row)


# ── One CLI run ────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    """What one ``cursor-agent -p`` run gets."""

    prompt: str
    workspace: Path
    model: str
    policy: ToolPolicy
    files: bool  # may edit files (always for a job; the owner's choice for a chat)
    shell: bool  # may run allowlisted commands
    resume: str | None = None
    tools: list[ChatTool] = field(default_factory=list)
    bridge: ToolResultBridge | None = None
    partial: bool = False
    max_steps: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    run_as_user: str | None = None


@dataclass
class TurnState:
    """What the runs of one job or chat turn produced (the counterpart of claude_code's ``JobState``)."""

    native_session_id: str | None = None
    texts: list[str] = field(default_factory=list)  # this run's full text blocks
    steps: int = 0
    policy_violations: int = 0
    failed: bool = False
    budget_hit: bool = False
    breach: str | None = None  # a forbidden call that ran anyway; the run was stopped

    @property
    def text(self) -> str:
        return "\n\n".join(t.strip() for t in self.texts if t.strip())


# Runs in one workspace are serialised: each one swaps that workspace's .cursor/ config.
_workspace_locks: dict[str, asyncio.Lock] = {}


def _workspace_lock(workspace: Path) -> asyncio.Lock:
    return _workspace_locks.setdefault(str(workspace.resolve()), asyncio.Lock())


# ── Adapter class ──────────────────────────────────────────────────────────────


class CursorAdapter(BaseAgentAdapter):
    """Cursor's agent CLI (``cursor-agent -p --output-format stream-json``) for one HEARTH job at a time, and
    for the Hearth web app's chat turns (:meth:`chat`), under the same guardrails as claude_code.

    What differs from claude_code, because Cursor is driven as a CLI rather than through an SDK:

    - the tool-layer policy is Cursor's own permission rules plus a check of every reported tool call
      (``hooks.py``); ``--force`` is passed only on turns that may edit files, ``--sandbox enabled`` always;
    - the workspace's ``.cursor/`` config is replaced by HEARTH's for the length of a run (``mcp_config.py``);
    - the finish gate runs when a turn ends: HEARTH validates the candidate, and a failure goes back to the
      agent as the next turn of the same chat, at most ``limits.max_validation_rounds`` times;
    - the web app's tools reach Cursor through a stdio MCP server (``mcp_tools.py``, ``mcp_server.py``);
    - Cursor reports no token usage, so ``Usage`` carries steps only.
    """

    @property
    def adapter_name(self) -> str:
        return "cursor"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()
        flags = self.commands.get("flags") or {}
        if flags.get("sandbox", "enabled") != "enabled":
            raise ValueError("HEARTH never runs Cursor with its sandbox off; set flags.sandbox to \"enabled\" in manifest.json")
        if flags.get("force", "edit_turns") not in _FORCE_MODES:
            raise ValueError("HEARTH passes --force only on turns that may edit; set flags.force to "
                             "\"edit_turns\" or \"never\" in manifest.json")

    # ── Manifest-driven settings ───────────────────────────────────────────────

    @property
    def _flags(self) -> dict[str, Any]:
        return self.commands.get("flags") or {}

    def _model_for(self, requested: str | None) -> str:
        model_cfg = self.commands.get("model") or {}
        return requested or self.config.get("model") or model_cfg.get("default") or _DEFAULT_MODEL

    def _cli(self) -> list[str]:
        return [self.config.get("cli_path") or cursor_binary()]

    def cli_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Environment the CLI runs with: never opens a browser; ``CURSOR_API_KEY`` passes through from the
        process; the job's scoped provider env (``HarnessHooks.llm_env``) on top."""
        env = {"NO_OPEN_BROWSER": "1", "CI": "1"}
        env.update(extra or {})
        return env

    def build_command(self, turn: Turn) -> list[str]:
        argv = [*self._cli(), "-p", "--output-format", "stream-json",
                "--workspace", str(turn.workspace), "--sandbox", "enabled"]
        if self._flags.get("trust_workspace", True):
            argv.append("--trust")  # headless runs cannot answer the trust prompt; the repo's own .cursor/ is set aside
        if turn.partial:
            argv.append("--stream-partial-output")
        if turn.files and self._flags.get("force", "edit_turns") == "edit_turns":
            argv.append("--force")  # print mode applies edits only with --force; deny rules still hold
        if turn.tools:
            argv.append("--approve-mcps")
        if turn.model and turn.model != "auto":
            argv += ["--model", safe_arg(turn.model)]
        if turn.resume:
            argv.append(f"--resume={safe_arg(turn.resume)}")
        return [*argv, "--", turn.prompt]

    def transcript_available(self, chat_id: str, workspace: Path, session_key: str) -> bool:
        """Whether ``--resume`` can continue ``chat_id`` here: Cursor still has the chat, and the session's
        folder is still ``workspace`` (Cursor stores chats per workspace)."""
        directory = get_session_directory(session_key)
        if directory and Path(directory).resolve() != workspace.resolve():
            return False
        return find_chat(chat_id) is not None

    # ── One run ────────────────────────────────────────────────────────────────

    async def run_turn(self, turn: Turn, state: TurnState, emit: Emit) -> None:
        """Run the CLI once under HEARTH's project config, emitting events; never raises for CLI failures."""
        run_dir = Path(tempfile.mkdtemp(prefix="hearth-cursor-"))
        try:
            async with _workspace_lock(turn.workspace):
                relay = ClientToolRelay(turn.tools, turn.bridge, emit, run_dir) if turn.tools and turn.bridge else None
                async with relay if relay is not None else contextlib.nullcontext():
                    rules = permission_rules(turn.policy, files=turn.files, shell=turn.shell,
                                             client_tools=relay is not None, other_servers=global_mcp_servers())
                    servers = {CLIENT_SERVER_NAME: relay.server_entry()} if relay is not None else {}
                    with project_config(turn.workspace, rules, servers, stash_dir=run_dir):
                        await self._run_cli(turn, state, emit, relay.names if relay is not None else frozenset())
        except ValueError as exc:  # a .cursor that is not a folder
            state.failed = True
            await emit(se.error(str(exc)))
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    async def _run_cli(self, turn: Turn, state: TurnState, emit: Emit, client_tools: frozenset[str]) -> None:
        stream = CursorStream(partial=turn.partial, client_tools=client_tools)
        pending: dict[str | None, tuple[Any, str]] = {}  # forbidden calls awaiting their outcome
        stopped = False

        async def handle(events: list[dict]) -> None:
            nonlocal stopped
            for event in events:
                if event["type"] == se.SESSION_ID:
                    state.native_session_id = event["session_id"]
                elif event["type"] == se.TOKEN and not event.get("partial"):
                    state.texts.append(event["token"])
                    state.steps += 1
                await emit(event)
            for call in stream.pop_calls():
                if call.phase == "completed":
                    state.steps += 1
                decision = check_tool(call.action, turn.policy, turn.workspace,
                                      files=turn.files, shell=turn.shell, client_tools=client_tools)
                if decision.allowed:
                    continue
                if call.phase == "started":
                    pending[call.call_id] = (call.action, decision.reason)
                    continue
                pending.pop(call.call_id, None)
                if call.succeeded:
                    state.breach = f"{call.action.name} ran although HEARTH's policy forbids it: {decision.reason}"
                    await emit(se.error(state.breach))
                    stopped = True
                else:
                    await _denied(call.call_id, call.action, decision.reason)
            if turn.max_steps and state.steps >= turn.max_steps and not stopped:
                state.budget_hit = stopped = True
                await emit(se.error(f"the turn reached its {turn.max_steps} step limit and was stopped"))

        async def _denied(call_id: str | None, action: Any, reason: str) -> None:
            state.policy_violations += 1
            await emit(se.policy_denied(action.name, call_id, action.input, reason))

        try:
            argv = self.build_command(turn)
        except ValueError as exc:
            state.failed = True
            await emit(se.error(str(exc)))
            return
        async with await spawn(argv, cwd=turn.workspace, env=self.cli_env(turn.env), user=turn.run_as_user) as proc:
            if proc.binary_missing or proc.exception:
                state.failed = True
                await emit(se.error(f"Cursor CLI unavailable: {proc.exception or argv[0]}"))
                return
            async for raw in proc.lines():
                await handle(stream.feed(raw))
                if stopped:
                    proc.kill()
                    break
            await handle(stream.finish())
            code = await proc.wait()
        for call_id, (action, reason) in pending.items():  # refused before they ran
            await _denied(call_id, action, reason)
        if stopped:
            return
        if stream.result is None:
            state.failed = True
            detail = proc.stderr_tail.strip()[-1000:]
            await emit(se.error(f"cursor-agent exited with code {code}" + (f": {detail}" if detail else "")))
        elif stream.result.get("is_error"):
            state.failed = True

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def stream(self, job: JobSpec, workspace: Path, hooks: HarnessHooks) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        state = TurnState()
        rounds = 0
        finished = out_of_rounds = False
        summary = ""
        attention: list[str] = []
        last_validation: ValidationResult | None = None
        model = self._model_for(job.model)

        # Session index: continue the earlier job's chat when Cursor still has it for this workspace
        # (the Sandbox Runner reuses the branch's fix workspace for a revise); otherwise start fresh.
        # Cursor cannot fork a chat, so a revise continues the earlier one.
        key = make_session_key(job.job_id)
        resume_native: str | None = None
        resume_note: str | None = None
        if job.resume_job_id:
            previous = get_native_session_id(make_session_key(job.resume_job_id))
            if previous and self.transcript_available(previous, workspace, make_session_key(job.resume_job_id)):
                resume_native = previous
                resume_note = f"continued from {job.resume_job_id}"
            else:
                resume_note = f"fresh session: {job.resume_job_id}'s chat is not reachable from this workspace"
                log.info("job %s: %s", job.job_id, resume_note)
        write_preliminary_entry(key, job.job_id, str(workspace), model=model,
                                resumed_from=job.resume_job_id if resume_native else None)

        async def emit(event: dict) -> None:
            if event.get("type") == se.SESSION_ID:
                # Persist at once, so a cancelled job still maps to its chat.
                _patch_native_session_id(key, event["session_id"])
            await queue.put(event)

        async def produce() -> None:
            nonlocal rounds, finished, out_of_rounds, summary, attention, last_validation
            try:
                prompt = build_turn_prompt(MIGRATOR_RULES, "", build_task_message(job))
                resume = resume_native
                while True:
                    state.texts.clear()
                    await self.run_turn(Turn(
                        prompt=prompt, workspace=workspace, model=model, policy=hooks.policy, files=True, shell=True,
                        resume=resume, max_steps=job.limits.max_steps, env=dict(hooks.llm_env),
                        run_as_user=hooks.run_as_user,
                    ), state, emit)
                    if state.breach or state.failed or state.budget_hit:
                        break
                    # The finish gate: the agent ended its turn; only an independent validation counts.
                    rounds += 1
                    await emit(se.activity(se.VALIDATING))
                    v = await hooks.validate_candidate()
                    last_validation = v
                    ok = v.passed and not v.regressions
                    await emit(se.validation(phase="finish", passed=ok, regressions=v.regressions, round=rounds))
                    if ok:
                        finished, summary = True, state.text
                        attention = parse_attention(summary)
                        break
                    if rounds >= job.limits.max_validation_rounds:
                        out_of_rounds = True
                        break
                    if not state.native_session_id:
                        await emit(se.error("Cursor did not report a chat id, so the fix-up turn cannot continue it"))
                        break
                    prompt, resume = build_fixup_message(v), state.native_session_id
            except Exception as exc:  # CLI failure: report, never raise into the harness
                log.exception("cursor run failed")
                await emit(se.error(f"{type(exc).__name__}: {exc}"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        timed_out = False
        completed = False  # False only when the caller stopped consuming the stream early
        try:
            async with asyncio.timeout(job.limits.wall_clock_seconds):
                while (event := await queue.get()) is not None:
                    yield event
            completed = True
        except TimeoutError:
            timed_out = completed = True
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if finished:
                status = "passed"
            elif state.breach:
                status = "policy_violation"
            elif timed_out or state.budget_hit:
                status = "budget_exhausted"
            elif completed:
                status = "failed_fix"
            else:
                status = "cancelled"
            # Always record the outcome on the index row, even when the caller stopped early.
            _roll_up(key, state.native_session_id, state.steps or None, status)
        result = AgentResult(
            job_id=job.job_id,
            intelligence=self.adapter_name,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            needs_human_attention=attention,
            validation=last_validation,
            usage=Usage(steps=state.steps),
            native_session_id=state.native_session_id,
            finished_at=datetime.now(UTC),
            extra={"policy_violations": state.policy_violations, "validation_rounds": rounds,
                   **({"out_of_rounds": True} if out_of_rounds else {}),
                   **({"breach": state.breach} if state.breach else {}),
                   **({"session": resume_note} if resume_note else {})},
        )
        yield se.done(state.native_session_id, result.model_dump(mode="json"))

    # ── Chat (the Hearth web app) ──────────────────────────────────────────────

    supports_chat = True

    async def chat(self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        state = TurnState()
        failed = False
        model = self._model_for(spec.model)

        # Continue the conversation's chat when Cursor still has it for this folder (it does not after the
        # assistant's folder changed); otherwise start a new one.
        key = make_chat_key(spec.session)
        previous = get_native_session_id(key)
        if previous and self.transcript_available(previous, workspace, key):
            resume: str | None = previous
            _mark_running(key)
        else:
            resume = None
            write_preliminary_entry(key, key, str(workspace), model=model)

        async def emit(event: dict) -> None:
            if event.get("type") == se.SESSION_ID:
                _patch_native_session_id(key, event["session_id"])
            await queue.put(event)

        async def produce() -> None:
            try:
                prompt = build_turn_prompt(CHAT_RULES, spec.system, spec.message)
                await self.run_turn(Turn(
                    prompt=prompt, workspace=workspace, model=model, policy=ToolPolicy(),
                    files=spec.permissions.files, shell=spec.permissions.shell, resume=resume,
                    tools=list(spec.tools), bridge=bridge, partial=True, max_steps=spec.limits.max_turns,
                ), state, emit)
            except Exception as exc:  # CLI failure: report it on the stream, never raise into the route
                log.exception("cursor chat turn failed")
                await emit(se.error(f"{type(exc).__name__}: {exc}"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        timed_out = False
        completed = False  # False only when the caller stopped consuming the stream early
        try:
            async with asyncio.timeout(spec.limits.wall_clock_seconds):
                while (event := await queue.get()) is not None:
                    failed = failed or event.get("type") == se.ERROR
                    yield event
            completed = True
        except TimeoutError:
            timed_out = completed = True
            yield se.error(f"the turn reached its {spec.limits.wall_clock_seconds} second limit and was stopped")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if timed_out or state.budget_hit:
                status = "budget_exhausted"
            elif not completed:
                status = "cancelled"
            else:
                status = "error" if failed or state.failed or state.breach else "completed"
            _roll_up(key, state.native_session_id, state.steps or None, status)
        yield se.done(state.native_session_id, {
            "session": spec.session,
            "status": status,
            "usage": Usage(steps=state.steps).model_dump(mode="json"),
            "policy_violations": state.policy_violations,
        })

    async def health(self) -> dict[str, Any]:
        try:
            res = await run_cli(self._cli()[0], ["--version"], timeout=10, label="cursor-agent")
        except CliStatusError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": res.returncode == 0, "version": res.stdout}


# Stable discovery alias — the dynamic loader resolves
# services.hearth_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = CursorAdapter
