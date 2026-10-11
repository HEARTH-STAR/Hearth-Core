from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.base import BaseAgentAdapter, HarnessHooks
from services.hearth_agent.adapters.claude_code.cli import claude_binary
from services.hearth_agent.adapters.claude_code.hooks import build_hooks, deny_prompts
from services.hearth_agent.adapters.claude_code.mcp_config import (
    CLIENT_SERVER_NAME,
    chat_mcp_servers,
    server_name,
    session_mcp_servers,
)
from services.hearth_agent.adapters.claude_code.mcp_tools import JobState
from services.hearth_agent.adapters.claude_code.prompts import MIGRATOR_RULES, build_chat_system, build_task_message
from services.hearth_agent.adapters.claude_code.streaming import parse_message, parse_partial
from services.hearth_agent.engine import sessions_io as _session_index
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.chat_bridge import ToolResultBridge
from services.hearth_agent.models import AgentResult, ChatPermissions, ChatSpec, JobSpec, Usage
from services.hearth_agent.policy import ToolPolicy

log = logging.getLogger(__name__)

# Fallbacks when manifest.json leaves a field out.
_DEFAULT_MODEL = "claude-opus-5-5"
_DEFAULT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]
_DEFAULT_DISALLOWED = ["WebFetch", "WebSearch", "Task"]
_BACKEND = "claude_code"

# A chat turn always gets the read-only tools; the owner's permissions add file edits and the shell.
_CHAT_READ_TOOLS = ("Read", "Glob", "Grep")
_CHAT_FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_CHAT_SHELL_TOOLS = ("Bash",)


def chat_tools(manifest_tools: list[str], permissions: ChatPermissions) -> list[str]:
    """The manifest's tools a chat turn may use, in manifest order."""
    wanted = {*_CHAT_READ_TOOLS}
    if permissions.files:
        wanted.update(_CHAT_FILE_TOOLS)
    if permissions.shell:
        wanted.update(_CHAT_SHELL_TOOLS)
    return [name for name in manifest_tools if name in wanted]


# ── Session index (ported from xo-space; one row per job) ─────────────────────
# xo-space keys rows by a chat session key and stores them per project; HEARTH
# keys them by job id under the state root (engine/sessions_io). The native
# session id is pre-allocated and written BEFORE the CLI starts, so the transcript
# file name is known from t=0 and a cancelled job can't orphan the mapping.

_native_map: dict[str, str] = {}


def make_session_key(job_id: str) -> str:
    return job_id


def make_chat_key(session: str) -> str:
    """Index key for a web-app conversation; the prefix keeps it apart from job ids."""
    return f"chat:{session}"


def _mark_running(session_key: str) -> None:
    row = _session_index.read_session_row(session_key)
    if row:
        row.update(status="running", updatedAt=_session_index.now_ms())
        _session_index.write_session_row(session_key, row)


def find_session_id_by_key(session_key: str) -> str | None:
    row = _session_index.read_session_row(session_key)
    return row.get("sessionId") if row else None


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    row = _session_index.read_session_row(session_key)
    native = (row or {}).get("nativeSessionId")
    if native:
        _native_map[session_key] = native
        return native
    return None


def get_session_directory(session_key: str) -> str | None:
    row = _session_index.read_session_row(session_key)
    return row.get("directory") if row else None


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Index key whose ``sessionId`` or ``nativeSessionId`` is ``session_id``."""
    for key, row in _session_index.iter_session_rows():
        if session_id in (row.get("sessionId"), row.get("nativeSessionId")):
            if row.get("nativeSessionId"):
                _native_map[key] = row["nativeSessionId"]
            return key
    return None


def write_preliminary_entry(
    session_key: str,
    session_id: str,
    cwd: str,
    native_session_id: str = "",
    *,
    model: str | None = None,
    resumed_from: str | None = None,
) -> None:
    """Write the job's index row before the CLI starts. Messages are not stored here;
    they live in the CLI's own transcript."""
    now = _session_index.now_ms()
    row = {
        "sessionId": session_id,
        "jobId": session_id,
        "nativeSessionId": native_session_id,
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
    }
    _session_index.write_session_row(session_key, row)
    if native_session_id:
        _native_map[session_key] = native_session_id


def _patch_native_session_id(session_key: str, native_sid: str) -> bool:
    """Write ``nativeSessionId`` into the row the moment it is first seen. Idempotent; never clobbers
    a different id already recorded."""
    if not session_key or not native_sid:
        return False
    row = _session_index.read_session_row(session_key)
    if not row:
        return False
    existing = row.get("nativeSessionId") or ""
    if existing == native_sid:
        _native_map[session_key] = native_sid
        return True
    if existing:
        return False
    row["nativeSessionId"] = native_sid
    row["updatedAt"] = _session_index.now_ms()
    _session_index.write_session_row(session_key, row)
    _native_map[session_key] = native_sid
    return True


def _roll_up(session_key: str, native_sid: str | None, raw_usage: dict, cost: float | None,
             num_turns: int | None, status: str) -> None:
    """Record the job's final usage, cost and status on its row (runs even when the job was cut short)."""
    row = _session_index.read_session_row(session_key)
    if not row:
        return
    if native_sid and not row.get("nativeSessionId"):
        row["nativeSessionId"] = native_sid
    usage = row.get("usage") or _session_index.empty_usage()
    for key in usage:
        usage[key] = int(usage.get(key, 0)) + int(raw_usage.get(key, 0) or 0)
    row.update(usage=usage, costUsd=cost, numTurns=num_turns, status=status, updatedAt=_session_index.now_ms())
    _session_index.write_session_row(session_key, row)


# ── Adapter class ──────────────────────────────────────────────────────────────


class ClaudeCodeAdapter(BaseAgentAdapter):
    """Claude Code, driven through the Claude Agent SDK, for one HEARTH job at a time, and for the
    Hearth web app's chat turns (:meth:`chat`), which run under the same guardrails.

    Shape mirrors xo-space ``adapters/claude_code/adapter.py``. What HEARTH changes:

    - permissions stay on: no ``--dangerously-skip-permissions``; the tool-layer
      policy runs in a ``PreToolUse`` hook (``hooks.py``) and prompts are denied;
    - the agent's tools are the manifest's ``flags.tools`` plus HEARTH's domain
      tools (``mcp_tools.py``); web tools and subagents are disallowed;
    - user/project settings files are not loaded (``setting_sources: []``), so the
      job config is the whole config;
    - ``finish`` only succeeds after an independent validation passes.
    """

    @property
    def adapter_name(self) -> str:
        return "claude_code"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()
        flags = self.commands.get("flags") or {}
        if flags.get("dangerously_skip_permissions"):
            raise ValueError("HEARTH never runs Claude Code with permissions bypassed; "
                             "set flags.dangerously_skip_permissions to false in manifest.json")

    # ── Manifest-driven settings ───────────────────────────────────────────────

    @property
    def _flags(self) -> dict[str, Any]:
        return self.commands.get("flags") or {}

    @property
    def _mcp_server(self) -> str:
        return server_name(self.commands)

    def _model(self, job: JobSpec) -> str:
        return self._model_for(job.model)

    def _model_for(self, requested: str | None) -> str:
        model_cfg = self.commands.get("model") or {}
        return requested or self.config.get("model") or model_cfg.get("default") or _DEFAULT_MODEL

    def _effort(self) -> str | None:
        return (self.commands.get("model") or {}).get("effort")

    def _cli_path(self) -> str | None:
        return self.config.get("cli_path") or None

    # ── Subprocess environment (ported from xo-space) ─────────────────────────

    def cli_env(self, hooks: HarnessHooks | None = None) -> dict[str, str]:
        """Environment the spawned ``claude`` runs with.

        From xo-space: drop empty/placeholder auth vars and OAuth tokens leaked into
        ``ANTHROPIC_API_KEY``, and let a usable native login win over static env
        tokens. HEARTH adds the space's LLM gateway and switches off non-essential
        traffic (no telemetry leaves the space).

        The SDK starts the CLI with ``{**os.environ, **options.env}``, so a variable
        is removed by overriding it with an empty string rather than omitting it.
        """
        env: dict[str, str] = {}
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            value = os.environ.get(key)
            if value is None:
                continue
            if value == "sk-ant-none" or (key != "CLAUDE_CODE_OAUTH_TOKEN" and value.startswith("sk-ant-oat")):
                env[key] = ""
        if self._has_usable_native_login():
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                if key in os.environ:
                    env[key] = ""
        if hooks is not None:
            env.update(hooks.llm_env)
            if hooks.llm_base_url:
                env["ANTHROPIC_BASE_URL"] = hooks.llm_base_url
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _agent_home_dir(self) -> Path:
        return Path(os.path.expanduser(self.commands.get("home_dir") or "~/.claude"))

    def _has_usable_native_login(self) -> bool:
        """True when ``~/.claude/.credentials.json`` holds a refresh token or an unexpired access token."""
        for name in (".credentials.json", "credentials.json"):
            path = self._agent_home_dir() / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            if not isinstance(oauth, dict):
                oauth = data if isinstance(data, dict) else {}
            if oauth.get("refreshToken"):
                return True
            access, expires_at = oauth.get("accessToken"), oauth.get("expiresAt")
            if access and isinstance(expires_at, (int, float)):
                if expires_at / 1000 > datetime.now(UTC).timestamp():
                    return True
            elif access and expires_at is None:
                return True
        return False

    # ── SDK options ────────────────────────────────────────────────────────────

    def _transcript_available(self, native_session_id: str, workspace: Path) -> bool:
        """Whether the CLI can resume ``native_session_id`` from ``workspace`` (its transcript is under that cwd)."""
        from claude_agent_sdk import get_session_info

        try:
            return get_session_info(native_session_id, directory=str(workspace)) is not None
        except Exception:
            return False

    def build_options(
        self, job: JobSpec, workspace: Path, hooks: HarnessHooks, state: JobState, emit,
        *, session_id: str | None = None, resume: str | None = None,
    ):
        """SDK options for one job. ``session_id`` pre-allocates the native id; ``resume`` forks a prior
        session (``/hearth revise``) into that new id, leaving the earlier transcript untouched."""
        from claude_agent_sdk import ClaudeAgentOptions

        tools = list(self._flags.get("tools") or _DEFAULT_TOOLS)
        server = self._mcp_server
        return ClaudeAgentOptions(
            cwd=str(workspace),
            model=self._model(job),
            effort=self._effort(),  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", "append": MIGRATOR_RULES},
            tools=tools,
            allowed_tools=[*tools, f"mcp__{server}__*"],
            disallowed_tools=list(self._flags.get("disallowed_tools") or _DEFAULT_DISALLOWED),
            mcp_servers=session_mcp_servers(job, hooks, state, emit, manifest=self.commands),
            session_id=session_id,
            resume=resume,
            fork_session=bool(resume),
            hooks=build_hooks(workspace, hooks.policy, state, emit, mcp_server=server),
            can_use_tool=deny_prompts,
            permission_mode=self._flags.get("permission_mode", "default"),
            setting_sources=list(self._flags.get("setting_sources", [])),
            max_turns=job.limits.max_steps,
            env=self.cli_env(hooks),
            user=hooks.run_as_user,
            cli_path=self._cli_path(),
        )

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def stream(self, job: JobSpec, workspace: Path, hooks: HarnessHooks) -> AsyncIterator[dict[str, Any]]:
        import claude_agent_sdk

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        state = JobState()
        usage = Usage()
        raw_usage: dict = {}
        native_session_id: str | None = None

        async def emit(event: dict) -> None:
            await queue.put(event)

        # Session index: resume the earlier job's session when its transcript is reachable from this
        # workspace (the Sandbox Runner reuses the branch's fix workspace for a revise); otherwise start fresh.
        key = make_session_key(job.job_id)
        resume_native: str | None = None
        resume_note: str | None = None
        if job.resume_job_id:
            previous = get_native_session_id(make_session_key(job.resume_job_id))
            if previous and self._transcript_available(previous, workspace):
                resume_native = previous
                resume_note = f"forked from {job.resume_job_id}"
            else:
                resume_note = f"fresh session: {job.resume_job_id}'s transcript is not reachable from this workspace"
                log.info("job %s: %s", job.job_id, resume_note)
        pre_allocated = str(uuid.uuid4())
        write_preliminary_entry(key, job.job_id, str(workspace), pre_allocated, model=self._model(job),
                                resumed_from=job.resume_job_id if resume_native else None)

        options = self.build_options(job, workspace, hooks, state, emit,
                                     session_id=pre_allocated, resume=resume_native)

        async def prompts():
            yield {"type": "user", "message": {"role": "user", "content": build_task_message(job)}}

        async def produce() -> None:
            nonlocal native_session_id
            try:
                async for msg in claude_agent_sdk.query(prompt=prompts(), options=options):
                    if isinstance(msg, claude_agent_sdk.AssistantMessage):
                        usage.steps += 1
                    for event in parse_message(msg):
                        if event["type"] == se.SESSION_ID:
                            native_session_id = event["session_id"]
                            # Persist at once, so a cancelled job still maps to its transcript.
                            _patch_native_session_id(key, native_session_id)
                        elif event["type"] == se.RESULT:
                            u = event.get("usage") or {}
                            raw_usage.clear()
                            raw_usage.update(u)
                            usage.input_tokens = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
                            usage.output_tokens = int(u.get("output_tokens", 0))
                            usage.cost_usd = event.get("total_cost_usd")
                            usage.steps = event.get("num_turns") or usage.steps
                            native_session_id = event.get("session_id") or native_session_id
                        await emit(event)
                    if state.finished or state.out_of_rounds:
                        break
                    if usage.input_tokens + usage.output_tokens > job.limits.max_tokens:
                        await emit(se.error("token budget exhausted"))
                        break
            except Exception as exc:  # SDK / CLI failure: report, never raise into the harness
                log.exception("claude code run failed")
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
            if state.finished:
                status = "passed"
            elif timed_out or (usage.steps >= job.limits.max_steps and not state.out_of_rounds):
                status = "budget_exhausted"
            elif completed:
                status = "failed_fix"
            else:
                status = "cancelled"
            # Always roll usage up onto the index row, even when the caller stopped early.
            _roll_up(key, native_session_id, raw_usage, usage.cost_usd, usage.steps or None, status)
        result = AgentResult(
            job_id=job.job_id,
            intelligence=self.adapter_name,
            status=status,  # type: ignore[arg-type]
            summary=state.summary,
            needs_human_attention=state.needs_attention,
            validation=state.last_validation,
            usage=usage,
            native_session_id=native_session_id,
            finished_at=datetime.now(UTC),
            extra={"policy_violations": state.policy_violations, "validation_rounds": state.validation_rounds,
                   **({"session": resume_note} if resume_note else {})},
        )
        yield se.done(native_session_id, result.model_dump(mode="json"))

    # ── Chat (the Hearth web app) ──────────────────────────────────────────────

    supports_chat = True

    def build_chat_options(
        self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge, state: JobState, emit,
        *, session_id: str | None = None, resume: str | None = None,
    ):
        """SDK options for one chat turn: the same guardrails as a job (policy hook, no prompts, no web,
        no project settings), the tools the owner allowed, and the web app's tools."""
        from claude_agent_sdk import ClaudeAgentOptions

        tools = chat_tools(self._flags.get("tools") or _DEFAULT_TOOLS, spec.permissions)
        client_prefix = f"mcp__{CLIENT_SERVER_NAME}__"

        def on_allow(name: str, tool_use_id: str | None) -> None:
            if name.startswith(client_prefix):
                bridge.expect(name[len(client_prefix):], tool_use_id)

        return ClaudeAgentOptions(
            cwd=str(workspace),
            model=self._model_for(spec.model),
            effort=self._effort(),  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", "append": build_chat_system(spec.system)},
            tools=tools,
            allowed_tools=[*tools, *([client_prefix + "*"] if spec.tools else [])],
            disallowed_tools=list(self._flags.get("disallowed_tools") or _DEFAULT_DISALLOWED),
            mcp_servers=chat_mcp_servers(spec, bridge),
            session_id=session_id,
            resume=resume,
            hooks=build_hooks(workspace, ToolPolicy(), state, emit, mcp_server=CLIENT_SERVER_NAME,
                              allowed=frozenset(tools), on_allow=on_allow),
            can_use_tool=deny_prompts,
            permission_mode=self._flags.get("permission_mode", "default"),
            setting_sources=list(self._flags.get("setting_sources", [])),
            max_turns=spec.limits.max_turns,
            # Text streams to the owner as it is written (parse_partial); jobs have no live reader.
            include_partial_messages=True,
            env=self.cli_env(),
            cli_path=self._cli_path(),
        )

    async def chat(self, spec: ChatSpec, workspace: Path, bridge: ToolResultBridge) -> AsyncIterator[dict[str, Any]]:
        import claude_agent_sdk

        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        state = JobState()
        usage = Usage()
        raw_usage: dict = {}
        native_session_id: str | None = None
        failed = False

        async def emit(event: dict) -> None:
            await queue.put(event)

        # Continue the conversation's session when its transcript is reachable from this folder (it is
        # not after the assistant's folder changed); otherwise start a new one with a pre-allocated id.
        key = make_chat_key(spec.session)
        previous = get_native_session_id(key)
        if previous and self._transcript_available(previous, workspace):
            session_id, resume = None, previous
            _mark_running(key)
        else:
            session_id, resume = str(uuid.uuid4()), None
            write_preliminary_entry(key, key, str(workspace), session_id, model=self._model_for(spec.model))

        options = self.build_chat_options(spec, workspace, bridge, state, emit, session_id=session_id, resume=resume)

        async def prompts():
            yield {"type": "user", "message": {"role": "user", "content": spec.message}}

        async def produce() -> None:
            nonlocal native_session_id
            try:
                async for msg in claude_agent_sdk.query(prompt=prompts(), options=options):
                    if isinstance(msg, claude_agent_sdk.AssistantMessage):
                        usage.steps += 1
                    for event in [*parse_partial(msg), *parse_message(msg)]:
                        if event["type"] == se.SESSION_ID:
                            native_session_id = event["session_id"]
                            _patch_native_session_id(key, native_session_id)
                        elif event["type"] == se.RESULT:
                            u = event.get("usage") or {}
                            raw_usage.clear()
                            raw_usage.update(u)
                            usage.input_tokens = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0))
                            usage.output_tokens = int(u.get("output_tokens", 0))
                            usage.cost_usd = event.get("total_cost_usd")
                            usage.steps = event.get("num_turns") or usage.steps
                            native_session_id = event.get("session_id") or native_session_id
                        await emit(event)
            except Exception as exc:  # SDK / CLI failure: report it on the stream, never raise into the route
                log.exception("claude code chat turn failed")
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
            if timed_out or usage.steps >= spec.limits.max_turns:
                status = "budget_exhausted"
            elif not completed:
                status = "cancelled"
            else:
                status = "error" if failed else "completed"
            _roll_up(key, native_session_id, raw_usage, usage.cost_usd, usage.steps or None, status)
        yield se.done(native_session_id, {
            "session": spec.session,
            "status": status,
            "usage": usage.model_dump(mode="json"),
            "policy_violations": state.policy_violations,
        })

    async def health(self) -> dict[str, Any]:
        cli = self._cli_path() or claude_binary()
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"ok": proc.returncode == 0, "version": stdout.decode().strip()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.hearth_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = ClaudeCodeAdapter
