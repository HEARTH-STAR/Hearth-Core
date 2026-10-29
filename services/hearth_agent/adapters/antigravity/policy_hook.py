"""HEARTH's ``PreToolUse`` hook for Antigravity: HEARTH's tool policy, applied to one ``agy`` tool call.

agy runs it before every tool call (``hooks.py`` registers it in ``~/.gemini/config/hooks.json``)::

    python policy_hook.py <policies dir>      stdin: the hook payload    stdout: {"decision": ..., "reason": ...}

Payload (camelCase): ``toolCall: {name, args}``, ``workspacePaths[]``, ``conversationId``, ``stepIdx``. The
run's policy is found by workspace: the adapter writes ``<policies dir>/<key>.json`` (``policy_key``) for the
length of a HEARTH run. Answers:

- a HEARTH run, call allowed by the policy → ``allow``;
- a HEARTH run, call forbidden → ``deny`` with the reason (fed back to the agent), and a line in the run's
  denials log so the adapter can put it on the audit stream;
- not a HEARTH run (no policy for the workspace) → ``ask``: agy's own rules decide, as without the hook;
- anything unreadable or unexpected → ``deny``. The hook always exits 0 with JSON (agy treats a crash or a
  non-zero exit as a failure that blocks the call).

Standard library plus ``services.hearth_agent.policy`` only: agy starts a fresh Python for every call, with a
sanitized environment, so nothing here may depend on Hearth-Core's process or env.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import IO, Any
from urllib.parse import unquote, urlparse

if __package__ in (None, ""):  # run by agy as a script: make ``services`` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from services.hearth_agent.policy import Decision, ToolPolicy  # noqa: E402

# agy's native tool names (hook payload ``toolCall.name``).
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "grep_search", "find_by_name", "list_dir",
              "codebase_search"}
EDIT_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
SHELL_TOOLS = {"run_command"}
WEB_TOOLS = {"search_web", "read_url_content", "read_browser_page", "browser_subagent"}
_PATH_KEYS = ("TargetFile", "AbsolutePath", "FilePath", "Path", "path", "file_path", "target_file")
_COMMAND_KEYS = ("CommandLine", "command", "Command")


def policy_key(workspace: str) -> str:
    """The policy file name for a workspace: the same for every spelling of the same folder."""
    real = os.path.normcase(os.path.realpath(workspace))
    return hashlib.sha256(real.encode("utf-8")).hexdigest()[:32]


def _workspace(entry: Any) -> str | None:
    if not isinstance(entry, str) or not entry:
        return None
    if entry.startswith("file:"):
        path = unquote(urlparse(entry).path)
        if len(path) > 2 and path[0] == "/" and path[2] == ":":  # file:///C:/x on Windows
            path = path[1:]
        return path or None
    return entry


def load_policy(policies_dir: str, workspace_paths: list) -> dict | None:
    for entry in workspace_paths:
        workspace = _workspace(entry)
        if workspace is None:
            continue
        try:
            data = json.loads((Path(policies_dir) / f"{policy_key(workspace)}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _first(args: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def decide(spec: dict, name: str, args: dict) -> Decision:
    policy = ToolPolicy(
        allowed_edit_paths=list(spec.get("allowed_edit_paths") or []),
        allow_test_edits=bool(spec.get("allow_test_edits", True)),
        allowed_binaries=set(spec.get("allowed_binaries") or []),
    )
    if name in READ_TOOLS or name in set(spec.get("extra_allowed_tools") or []):
        return Decision.allow()
    if name in EDIT_TOOLS:
        if not spec.get("files"):
            return Decision.deny("file edits are switched off for this conversation")
        path = _first(args, _PATH_KEYS)
        if not path:
            return Decision.deny(f"could not tell which file {name} changes")
        return policy.check_edit(path, workspace_root=str(spec.get("workspace") or ""))
    if name in SHELL_TOOLS:
        if not spec.get("shell"):
            return Decision.deny("shell commands are switched off for this conversation")
        return policy.check_command(_first(args, _COMMAND_KEYS) or "")
    if name in WEB_TOOLS:
        return Decision.deny("HEARTH agents have no web access")
    return Decision.deny(f"tool {name} is not available to HEARTH agents")


def _log_denial(spec: dict, name: str, args: dict, reason: str, payload: dict) -> None:
    log = spec.get("denials_log")
    if not log:
        return
    record = {"tool": name, "input": args, "reason": reason, "step": payload.get("stepIdx"),
              "conversation": payload.get("conversationId")}
    try:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def main(argv: list[str], stdin: IO[str], stdout: IO[str]) -> int:
    def answer(decision: str, reason: str = "") -> int:
        stdout.write(json.dumps({"decision": decision, **({"reason": reason} if reason else {})}) + "\n")
        stdout.flush()
        return 0

    try:
        if len(argv) != 2:
            return answer("deny", "HEARTH's policy hook is misconfigured")
        payload = json.load(stdin)
        if not isinstance(payload, dict):
            return answer("deny", "HEARTH's policy hook got no tool call")
        spec = load_policy(argv[1], payload.get("workspacePaths") or [])
        if spec is None:
            return answer("ask")
        call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        decision = decide(spec, name, args)
        if decision.allowed:
            return answer("allow")
        _log_denial(spec, name, args, decision.reason, payload)
        return answer("deny", decision.reason)
    except Exception as exc:  # fail closed, but still with an answer agy can read
        return answer("deny", f"HEARTH's policy hook failed: {type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin, sys.stdout))
