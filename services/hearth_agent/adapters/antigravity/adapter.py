from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.antigravity import transcript as _t
from services.hearth_agent.adapters.antigravity.auth import LOGIN_REQUIRED_MESSAGE, has_usable_login
from services.hearth_agent.adapters.antigravity.cli import agy_binary
from services.hearth_agent.adapters.antigravity.hooks import (
    active_policy,
    ensure_tool_permission,
    install_hook,
    project_hooks_aside,
    read_denials,
)
from services.hearth_agent.adapters.antigravity.paths import transcript_path, valid_conversation_id
from services.hearth_agent.adapters.antigravity.prompts import (
    CHAT_RULES,
    MIGRATOR_RULES,
    build_fixup_message,
    build_task_message,
    build_turn_prompt,
    parse_attention,
)
from services.hearth_agent.adapters.antigravity.tokens import conversation_tokens
from services.hearth_agent.adapters.base import BaseAgentAdapter, HarnessHooks
from services.hearth_agent.adapters.cli_status import CliStatusError, run_cli
from services.hearth_agent.engine import sessions_io as _session_index
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import AgentResult, ChatSpec, JobSpec, Usage, ValidationResult
from services.hearth_agent.policy import ToolPolicy
from utils.commands import spawn

log = logging.getLogger(__name__)

_BACKEND = "antigravity"
_DEFAULT_MODEL = "Gemini 3.5 Flash (Low)"
POLL_SECONDS = 0.4

Emit = Callable[[dict], Awaitable[None]]

# agy tool → the name ``stream_events.running`` turns into a fixed activity label.
_ACTIVITY_NAMES = {"run_command": "Bash", "write_to_file": "Edit", "replace_file_content": "Edit",
                   "multi_replace_file_content": "Edit"}
# Transcript steps that are work in progress, as a label only (xo-space's ``_label_for_step``).
_STEP_LABELS = {"RUN_COMMAND": se.RUNNING_COMMAND, "VIEW_FILE": "running view_file",
                "GREP_SEARCH": "running grep_search", "CODEBASE_SEARCH": "running codebase_search",
                "ERROR_MESSAGE": "running error recovery"}


# ── Session index (as in claude_code/adapter.py; one row per job or chat) ─────
# agy names the conversation itself, so a row starts with an empty ``nativeSessionId`` and gets it the
# moment the run's --log-file names it. Rows of another backend under the same key are not ours.

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
    """Write the row before agy starts. Messages are not stored here; they live in agy's transcript."""
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
    """Record the conversation id the first time it is seen (idempotent; never clobbers a different id)."""
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


def _roll_up(session_key: str, native_sid: str | None, tokens: dict, num_turns: int | None, status: str) -> None:
    """Record the outcome on the row. agy's token counts are the whole conversation's, so they replace
    (not add to) what the row held; they are agy's own client-side estimates (``tokens.py``)."""
    row = _own_row(session_key)
    if not row:
        return
    if native_sid and not row.get("nativeSessionId"):
        row["nativeSessionId"] = native_sid
    if tokens:
        row["usage"] = {**_session_index.empty_usage(), "input_tokens": int(tokens.get("total_input", 0)),
                        "output_tokens": int(tokens.get("total_output", 0))}
    row.update(numTurns=num_turns, status=status, updatedAt=_session_index.now_ms())
    _session_index.write_session_row(session_key, row)


# ── One CLI run ────────────────────────────────────────────────────────────────


@dataclass
class Turn:
    """What one ``agy -p`` run gets."""

    prompt: str
    workspace: Path
    model: str
    policy: ToolPolicy
    files: bool  # may edit files (always for a job; the owner's choice for a chat)
    shell: bool  # may run allowlisted commands
    resume: str | None = None
    max_steps: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    run_as_user: str | None = None


@dataclass
class TurnState:
    """What the runs of one job or chat turn produced (the counterpart of claude_code's ``JobState``)."""

    native_session_id: str | None = None
    texts: list[str] = field(default_factory=list)  # this run's answer
    steps: int = 0
    policy_violations: int = 0
    tokens: dict = field(default_factory=dict)
    failed: bool = False
    budget_hit: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(t.strip() for t in self.texts if t.strip())


# Runs in one workspace are serialised: the hook finds a run's policy by its workspace.
_workspace_locks: dict[str, asyncio.Lock] = {}


def _workspace_lock(workspace: Path) -> asyncio.Lock:
    return _workspace_locks.setdefault(str(workspace.resolve()), asyncio.Lock())


# ── Adapter class ──────────────────────────────────────────────────────────────


class AntigravityAdapter(BaseAgentAdapter):
    """Google Antigravity's CLI (``agy -p``) for one HEARTH job at a time, and for the Hearth web app's chat
    turns (:meth:`chat`), under the same guardrails as claude_code.

    Shape follows xo-space ``adapters/antigravity/adapter.py``. What HEARTH changes:

    - no ``--dangerously-skip-permissions``: HEARTH's tool policy runs in agy's ``PreToolUse`` hook
      (``hooks.py``, ``policy_hook.py``) for every call; ``manifest.json`` ``flags.dangerously_skip_permissions``
      must stay ``false``;
    - agy prints no events, so the run's transcript is tailed for progress and audit events and its final
      answer is the reply (as in xo-space); the hook's denials log becomes ``policy-denied`` events;
    - the finish gate runs when a turn ends: HEARTH validates the candidate, and a failure goes back to the
      agent as the next turn of the same conversation, at most ``limits.max_validation_rounds`` times;
    - the web app's own tools (``ChatSpec.tools``) are not offered: agy reaches no MCP server of HEARTH's.
    """

    @property
    def adapter_name(self) -> str:
        return _BACKEND

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()
        if self._flags.get("dangerously_skip_permissions"):
            raise ValueError("HEARTH never runs Antigravity with permissions bypassed (that also switches its "
                             "hooks off); set flags.dangerously_skip_permissions to false in manifest.json")

    # ── Manifest-driven settings ───────────────────────────────────────────────

    @property
    def _flags(self) -> dict[str, Any]:
        return self.commands.get("flags") or {}

    def _catalog(self) -> tuple[set[str], str]:
        model_cfg = self.commands.get("model") or {}
        default = str(model_cfg.get("default") or _DEFAULT_MODEL)
        return {default, *(str(m) for m in model_cfg.get("allowed") or [])}, default

    def _model_for(self, requested: str | None) -> str:
        """A name from agy's own catalog (``agy --model`` rejects anything else). ``requested`` may also be an
        ``/api/models`` id (``antigravity/<name>``); anything unknown falls back to the default."""
        catalog, default = self._catalog()
        default = self.config.get("model") or default
        for candidate in (requested, (requested or "").rsplit("/", 1)[-1]):
            if candidate and candidate in catalog:
                return candidate
        return default if default in catalog else next(iter(sorted(catalog)))

    def _cli(self) -> list[str]:
        return [self.config.get("cli_path") or agy_binary()]

    def cli_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """agy authenticates with its token file, so nothing is stripped. Pin the version (flags and transcript
        shapes must not shift mid-flight) and keep agy from opening a browser on the server."""
        env = {"AGY_CLI_DISABLE_AUTO_UPDATE": "1", "BROWSER": "true"}
        env.update(extra or {})
        return env

    def build_command(self, turn: Turn, log_file: Path) -> list[str]:
        # agy ignores its cwd: --add-dir is what pins the workspace (and what the hook sees).
        argv = [*self._cli(), "-p", turn.prompt, "--model", turn.model, "--add-dir", str(turn.workspace),
                "--log-file", str(log_file)]
        if turn.resume:
            argv += ["--conversation", turn.resume]
        return argv

    def transcript_available(self, conversation_id: str, workspace: Path, session_key: str) -> bool:
        """Whether ``--conversation`` can continue ``conversation_id`` here: agy still has its transcript, and
        the session's folder is still ``workspace``."""
        directory = get_session_directory(session_key)
        if directory and Path(directory).resolve() != workspace.resolve():
            return False
        return valid_conversation_id(conversation_id) and transcript_path(conversation_id).is_file()

    # ── One run ────────────────────────────────────────────────────────────────

    async def run_turn(self, turn: Turn, state: TurnState, emit: Emit) -> None:
        """Run agy once under HEARTH's policy hook, emitting events; never raises for CLI failures."""
        if not has_usable_login():
            state.failed = True
            await emit(se.error(LOGIN_REQUIRED_MESSAGE))
            return
        run_dir = Path(tempfile.mkdtemp(prefix="hearth-agy-"))
        denials = run_dir / "denials.jsonl"
        try:
            install_hook()
            ensure_tool_permission(self._flags.get("tool_permission", "always-proceed"))
            async with _workspace_lock(turn.workspace):
                with project_hooks_aside(turn.workspace, run_dir), active_policy(
                    turn.workspace, turn.policy, files=turn.files, shell=turn.shell, denials_log=denials,
                    extra_allowed_tools=list(self._flags.get("extra_allowed_tools") or []),
                ):
                    await self._run_cli(turn, state, emit, run_dir, denials)
        except ValueError as exc:  # unreadable hooks/settings file, or a symlinked .agents
            state.failed = True
            await emit(se.error(str(exc)))
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    async def _run_cli(self, turn: Turn, state: TurnState, emit: Emit, run_dir: Path, denials: Path) -> None:
        log_file = run_dir / "agy.log"
        argv = self.build_command(turn, log_file)
        # A resumed conversation already holds earlier turns' steps: only new ones are this run's.
        seen = {s.get("step_index") for s in _t.read_steps(turn.resume)} if turn.resume else set()
        earlier = frozenset(seen)
        audited: set = set(seen)
        output: deque[str] = deque(maxlen=40)
        offset = 0
        stopped = False

        async def follow() -> None:
            nonlocal offset, stopped
            cid = _t.conversation_id_from_log(log_file) or turn.resume
            if cid and cid != state.native_session_id:
                state.native_session_id = cid
                await emit(se.session_id(cid))
            for step in _t.read_steps(cid) if cid else []:
                for event in self._step_events(cid, step, seen, audited, state):
                    await emit(event)
            records, offset = read_denials(denials, offset)
            for record in records:
                state.policy_violations += 1
                await emit(se.policy_denied(str(record.get("tool") or ""), None, record.get("input") or {},
                                            str(record.get("reason") or "")))
            if turn.max_steps and state.steps >= turn.max_steps and not stopped:
                state.budget_hit = stopped = True
                await emit(se.error(f"the turn reached its {turn.max_steps} step limit and was stopped"))

        async with await spawn(argv, cwd=turn.workspace, env=self.cli_env(turn.env), merge_stderr=True,
                               user=turn.run_as_user) as proc:
            if proc.binary_missing or proc.exception:
                state.failed = True
                await emit(se.error(f"Antigravity CLI unavailable: {proc.exception or argv[0]}"))
                return

            async def drain() -> None:
                async for raw in proc.lines():
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        output.append(line)

            reader = asyncio.create_task(drain())
            while not stopped:
                finished = reader.done()
                await follow()
                if finished:
                    break
                await asyncio.sleep(POLL_SECONDS)
            if stopped:
                proc.kill()
            await reader
            code = await proc.wait()
        await follow()  # the last steps and denials written before agy exited

        cid = state.native_session_id
        if cid:
            state.tokens = conversation_tokens(cid)
        if stopped:
            return
        answer = _t.final_answer(cid) if cid else None
        if answer and (not turn.resume or self._answer_is_new(cid, earlier)):
            state.texts.append(answer)
            state.steps += 1
            await emit(se.token(answer))
            return
        # agy can print a failure to stdout with a non-zero exit, so stdout is never shown as a reply.
        state.failed = True
        detail = "\n".join(output)[-1000:]
        await emit(se.error(f"agy exited with code {code} without an answer" + (f": {detail}" if detail else "")))

    @staticmethod
    def _answer_is_new(cid: str, earlier: set) -> bool:
        """True when the final answer comes from a step this run wrote (not an earlier turn's)."""
        for step in reversed(_t.read_steps(cid)):
            if step.get("type") == "PLANNER_RESPONSE" and not step.get("tool_calls") and step.get("content"):
                return step.get("step_index") not in earlier
        return False

    @staticmethod
    def _step_events(cid: str, step: dict, seen: set, audited: set, state: TurnState) -> list[dict]:
        """Progress and audit events for a transcript step, each emitted once."""
        idx = step.get("step_index")
        events: list[dict] = []
        calls = _t.tool_calls(step) if step.get("type") == "PLANNER_RESPONSE" else []
        if calls and idx not in audited:  # a step can be written RUNNING first and gain its calls later
            audited.add(idx)
            for n, (name, args) in enumerate(calls):
                state.steps += 1
                events.append(se.running(_ACTIVITY_NAMES.get(name, name)))
                events.append(se.tool_call(name, f"{cid}:{idx}:{n}", args))
        if idx not in seen:
            seen.add(idx)
            label = _STEP_LABELS.get(str(step.get("type")))
            if label:
                events.append(se.activity(label))
        return events

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

        # Continue the earlier job's conversation when agy still has it for this workspace (the Sandbox
        # Runner reuses the branch's fix workspace for a revise); otherwise start fresh. agy cannot fork.
        key = make_session_key(job.job_id)
        resume_native: str | None = None
        resume_note: str | None = None
        if job.resume_job_id:
            previous = get_native_session_id(make_session_key(job.resume_job_id))
            if previous and self.transcript_available(previous, workspace, make_session_key(job.resume_job_id)):
                resume_native = previous
                resume_note = f"continued from {job.resume_job_id}"
            else:
                resume_note = f"fresh session: {job.resume_job_id}'s conversation is not reachable from this workspace"
                log.info("job %s: %s", job.job_id, resume_note)
        write_preliminary_entry(key, job.job_id, str(workspace), model=model,
                                resumed_from=job.resume_job_id if resume_native else None)

        async def emit(event: dict) -> None:
            if event.get("type") == se.SESSION_ID:
                # Persist at once, so a cancelled job still maps to its conversation.
                _patch_native_session_id(key, event["session_id"])
            await queue.put(event)

        async def produce() -> None:
            nonlocal rounds, finished, out_of_rounds, summary, attention, last_validation
            try:
                message = build_task_message(job)
                resume = resume_native
                while True:
                    state.texts.clear()
                    await self.run_turn(Turn(
                        prompt=build_turn_prompt(MIGRATOR_RULES, "", message), workspace=workspace, model=model,
                        policy=hooks.policy, files=True, shell=True, resume=resume, max_steps=job.limits.max_steps,
                        env=dict(hooks.llm_env), run_as_user=hooks.run_as_user,
                    ), state, emit)
                    if state.failed or state.budget_hit:
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
                        await emit(se.error("agy did not name its conversation, so the fix-up turn cannot continue it"))
                        break
                    message, resume = build_fixup_message(v), state.native_session_id
            except Exception as exc:  # CLI failure: report, never raise into the harness
                log.exception("antigravity run failed")
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
            elif timed_out or state.budget_hit:
                status = "budget_exhausted"
            elif completed:
                status = "failed_fix"
            else:
                status = "cancelled"
            _roll_up(key, state.native_session_id, state.tokens, state.steps or None, status)
        result = AgentResult(
            job_id=job.job_id,
            intelligence=self.adapter_name,
            status=status,  # type: ignore[arg-type]
            summary=summary,
            needs_human_attention=attention,
            validation=last_validation,
            usage=self._usage(state),
            native_session_id=state.native_session_id,
            finished_at=datetime.now(UTC),
            extra={"policy_violations": state.policy_violations, "validation_rounds": rounds,
                   **({"out_of_rounds": True} if out_of_rounds else {}),
                   **({"session": resume_note} if resume_note else {})},
        )
        yield se.done(state.native_session_id, result.model_dump(mode="json"))

    @staticmethod
    def _usage(state: TurnState) -> Usage:
        return Usage(input_tokens=int(state.tokens.get("total_input", 0)),
                     output_tokens=int(state.tokens.get("total_output", 0)), steps=state.steps)

    # ── Chat (the Hearth web app) ──────────────────────────────────────────────

    supports_chat = True

    async def chat(self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        state = TurnState()
        failed = False
        model = self._model_for(spec.model)

        # Continue the conversation when agy still has it for this folder; otherwise start a new one.
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
                await self.run_turn(Turn(
                    prompt=build_turn_prompt(CHAT_RULES, spec.system, spec.message), workspace=workspace,
                    model=model, policy=ToolPolicy(), files=spec.permissions.files, shell=spec.permissions.shell,
                    resume=resume, max_steps=spec.limits.max_turns,
                ), state, emit)
            except Exception as exc:  # CLI failure: report it on the stream, never raise into the route
                log.exception("antigravity chat turn failed")
                await emit(se.error(f"{type(exc).__name__}: {exc}"))
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        timed_out = False
        completed = False
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
                status = "error" if failed or state.failed else "completed"
            _roll_up(key, state.native_session_id, state.tokens, state.steps or None, status)
        yield se.done(state.native_session_id, {
            "session": spec.session,
            "status": status,
            "usage": self._usage(state).model_dump(mode="json"),
            "policy_violations": state.policy_violations,
        })

    async def health(self) -> dict[str, Any]:
        try:
            res = await run_cli(self._cli()[0], ["--version"], timeout=10, label="agy")
        except CliStatusError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": res.returncode == 0, "version": res.stdout, "logged_in": has_usable_login()}


# Stable discovery alias — the dynamic loader resolves
# services.hearth_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = AntigravityAdapter
