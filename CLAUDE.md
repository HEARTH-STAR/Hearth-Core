# Hearth-Core - Project Memory

## Project overview

- FastAPI service that runs inside each HEARTH space (one Docker container on the customer's machine).
- Holds three things: the GitHub connector (gh CLI), the API diff engine, and the Claude Code adapter.
- Product docs: `../HEARTH_PRD.pdf`, `../HEARTH_TDD.pdf` (their engine services in Hearth-backend are not
  built; Hearth-backend only spins up containers today).
- Claude Code is the only intelligence. There is no OpenCode fork; do not add one.

## Sibling repos (github.com/HEARTH-STAR, checked out side by side)

- Hearth-frontend (`../Hearth-frontend`, :4310): the web app. Calls this API at its `HEARTH_CORE_URL`
  (`/api/chat`, `/api/workspace/*`, `/health`).
- Hearth-Core (this repo, :5010).
- Hearth-backend (`../Hearth-backend`, :5020): spins up containers; its `ubuntu-26.04` template clones this
  repo into `/workspace/hearth-core`.
- Ports and URLs are documented in each repo's `.env.example`; keep them in step when one changes.

## Architecture conventions

- Layout mirrors xo-space (`../xo-space`, reference only — never copy it wholesale):
  `routers/hearth_agent/` (thin APIRouters) → `services/hearth_agent/` (agent-specific logic) and
  top-level `services/<name>/` for shared space-level pieces (`services/storage/`).
- The API diff engine is its own root-level package, `apidiff/` (`from apidiff import ...`), not under
  `services/`. CLI: `python -m apidiff.cli OLD NEW`.
- Keep route handlers thin; logic lives in services.
- Every external command runs through `utils/commands` (`run` over an argv list, `safe_arg` for untrusted
  values). No `shell=True`, no command strings.

## Agent-modular architecture

- Core never names a specific agent. The active agent comes from `config/agents/<name>/manifest.json`
  via `services/hearth_agent/registry/agent_registry.py` (`AGENT_NAME` → `DEFAULT_AGENT` → sole manifest).
- Agent-specific code lives only in `config/agents/<name>/` and `services/hearth_agent/adapters/<name>/`.
  `adapters/loader.py` (`load_capability` / `try_load_capability`) is the one seam to reach it.
- Every adapter's `stream()` speaks `services/hearth_agent/engine/stream_events.py` and ends with exactly
  one `{"done": True, "native_session_id", "result"}`. So does `chat()`, for adapters with `supports_chat`.

## Chat (the Hearth web app, `../Hearth-frontend`)

- `POST /api/chat` runs one turn of a `ChatSpec` and streams the events as SSE; turns with the same
  `session` continue one Claude Code session (index key `chat:<session>`). One turn per session at a time.
- The turn runs in `ChatSpec.directory` under the workspace root (`services/storage/workspace.py`,
  `HEARTH_WORKSPACE_DIR`). `GET /api/workspace/files` and `GET /api/workspace/file` are read-only views of it.
- `ChatSpec.permissions` picks the built-in tools (reads always; `files` adds edits, `shell` adds Bash), and
  the PreToolUse hook denies anything outside that set on top of the job policy.
- `ChatSpec.tools` are tools the web app runs itself, served as `mcp__client__<name>`. The caller posts each
  result to `POST /api/chat/{session}/tool-results` (`engine/chat_bridge.py`); `handoff` tools return an
  acknowledgement at once and the owner's decision arrives as the next turn.

## Security invariants (from the TDD — do not weaken)

- Claude Code never runs with permissions bypassed. `manifest.json` `flags.dangerously_skip_permissions`
  must stay `false`; the adapter refuses to start otherwise.
- The tool-layer policy (`services/hearth_agent/policy.py`) is enforced in the `PreToolUse` hook for every
  tool call. Edits outside the Impact Report scope, CI config, secrets and `.git/` are denied; network
  tools, `git push/remote/fetch`, `gh`, `sudo` are denied.
- `finish` is accepted only after `HarnessHooks.validate_candidate()` passes.
- Chat turns keep every job guardrail (policy hook, `deny_prompts`, no web tools, `setting_sources: []`).
  Workspace paths from callers go through `services/storage/workspace.resolve`, which refuses absolute paths,
  `..` and symlinks out of the root.
- Remote Control (`adapters/claude_code/remote_control.py`) stays off unless an operator sets
  `remote_control.enabled: true` in `config/agents/claude_code/capabilities.json`: it lets claude.ai drive a
  Claude Code session in the space outside the sandbox and policy. `start()` refuses (route: 403) otherwise.
- The adapter package mirrors xo-space's `claude_code/` module for module. Modules that do not fit HEARTH
  (`session_telemetry`, `visualizer_source`) keep the contract and report `supported: False`; `agents` serves
  the one built-in migrator and answers 405 to create/patch/delete.
- The GitHub token lives only in gh's store (`gh auth login --with-token`). `token.json` holds metadata.
  Never write a token into a remote URL, a sandbox, a log, an error message, or a JSON file. Every gh call
  runs without `GH_TOKEN`/`GITHUB_TOKEN` (`gh_api.STORE_BYPASS_ENV`), so gh's store is the only credential.
- One gh account per space: connecting signs gh out of every other github.com account; disconnecting signs
  it out of all of them and removes the git identity and credential helper connecting wrote
  (quirq-ai/xo-space#197).
- Sandboxes receive `RepoMirror.export_tree()` output: no `.git` remote, no credential.

## Testing

- `python -m unittest discover -s tests -t .` is the gate; `python -m pytest -q` also works.
- Tests are plain `unittest.TestCase` / `IsolatedAsyncioTestCase`. No test calls a model or GitHub.
