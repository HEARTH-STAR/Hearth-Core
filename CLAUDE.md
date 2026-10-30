# Hearth-Core - Project Memory

## Project overview

- FastAPI service that runs inside each HEARTH space (one Docker container on the customer's machine).
- Holds four things: the GitHub connector (gh CLI), the API diff engine, the agent adapters, and the API watch
  that ties them together (`services/hearth_agent/watch/`, routes `/api/watch*`).
- Product docs: `../HEARTH_PRD.pdf`, `../HEARTH_TDD.pdf` (their engine services in Hearth-backend are not
  built; Hearth-backend only spins up containers today).
- Three intelligences: Claude Code (`claude_code`, the default), Cursor's agent CLI (`cursor`) and Google
  Antigravity's CLI (`antigravity`, `agy`), one active per space (`AGENT_NAME`). There is no OpenCode fork; do not add one.

## Sibling repos (github.com/HEARTH-STAR, checked out side by side)

- Hearth-frontend (`../Hearth-frontend`, :4310): the web app. Each of its Spaces is a container running this
  service; it calls `/api/chat`, `/api/workspace/*`, `/api/connectors/{github,claude-code}/*`, `/api/watch*` and `/health` at the
  port Docker published for the container's 5010.
- Hearth-Core (this repo, :5010).
- Hearth-backend (`../Hearth-backend`, :5020): spins up containers. Its `space` template clones this repo's
  `main` into `/workspace/hearth-core` and runs it on 5010 (published on loopback); each Space in the web app
  is one such container, so a change here reaches spaces only once it is pushed.
- Ports and URLs are documented in each repo's `.env.example`; keep them in step when one changes.

## Architecture conventions

- Layout mirrors xo-space (`../xo-space`, reference only — never copy it wholesale):
  `routers/hearth_agent/` (thin APIRouters) → `services/hearth_agent/` (agent-specific logic) and
  top-level `services/<name>/` for shared space-level pieces (`services/storage/`).
- The API diff engine is its own root-level package, `apidiff/` (`from apidiff import ...`), not under
  `services/`. CLI: `python -m apidiff.cli OLD NEW`.
- Keep route handlers thin; logic lives in services.
- `server.py` pins the proactor event loop on Windows (`event_loop()`): with reload, uvicorn picks the selector
  loop, which cannot start subprocesses, and every gh, git and claude call would fail.
- Every external command runs through `utils/commands` (`run` over an argv list, `safe_arg` for untrusted
  values). No `shell=True`, no command strings. The one exception: a watch's `validate_commands` (the
  owner's own build/test lines, like a CI file) run as `sh -c <line>` in a clean checkout
  (`watch/validation.py`).

## API watch (`services/hearth_agent/watch/`)

- The scheduler (started in `server.py`'s lifespan; `HEARTH_API_WATCH=0` turns it off) ticks every 30 s:
  due checks fetch each watched OpenAPI spec, diff it against the stored baseline with `apidiff`, and record a
  detection for breaking changes. The detection id is the Change Record id: one breaking diff = one
  detection = at most one PR (also checked on GitHub by branch name before pushing).
- A fix runs one at a time: mirror sync → impact scan → baseline validation → the active agent edits an
  export (its `finish` is gated on the watch's commands passing on a fresh checkout with only its diff) →
  this code (never the agent) pushes `hearth/...` and opens the PR through `GhCli`.
- State: `$HEARTH_STATE_DIR/watch/state.json` + spec snapshots in `watch/specs/`. Hearth-frontend keeps
  no copy; it forwards `/api/spaces/:id/watch*` here.
- Spec URLs resolving to private/loopback addresses are refused unless `HEARTH_WATCH_ALLOW_PRIVATE=1`.
- `POST /api/watch/repo` ({repo, base_branch?}) is how the web app adds watches: `watch/discover.py` reads the
  repo's dependency manifests from the mirror, matches `CATALOG` (providers with a public OpenAPI spec), makes one
  watch per provider (`packages` = its SDKs, used by the impact scan to match SDK calls) and derives the
  validation commands from the lockfile and scripts.
- `/api/repos*` (`routers/hearth_agent/repos.py`, `watch/browse.py`) browse a watched repo read-only from its
  bare mirror (summary, tree, blob, commits, commit patches, branches) for the web app's repo page; unwatched
  repos answer 404 and the mirror is re-fetched at most every 2 minutes.
- Every watched repo is also checked out at `<workspace>/repos/<owner>/<name>` (`watch/checkout.py`), so it shows in
  the Space's Files tree and chat agents can read it; it follows the mirror unless it has local edits, and goes
  with the repo's last watch.

## Self-update (`services/update/core.py`, `/api/core/version`, `/api/core/update`)

- A Space's Hearth-Core is a depth-1 clone of `main` in `/workspace/hearth-core`, not part of the image. Update =
  fetch the branch, `reset --hard`, SIGTERM itself; the container's restart policy runs `hearth-space-start` again
  (requirements reinstalled when they changed). Only `HEARTH_SELF_UPDATE=1` (the space template) enables it;
  refused with local changes or while an API watch fix runs.

## Agent-modular architecture

- Core never names a specific agent. The active agent comes from `config/agents/<name>/manifest.json`
  via `services/hearth_agent/registry/agent_registry.py` (`AGENT_NAME` → `DEFAULT_AGENT` → sole manifest →
  the one manifest with `"default": true`, which is claude_code's). Spaces set no `AGENT_NAME`, so keep
  exactly one manifest marked default or every space fails at boot.
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

## Claude Code sign-in (`adapters/claude_code/login.py`)

- The space's Claude account is set up from the web app: `/api/connectors/claude-code/{status, login/start,
  login/code, login/poll, login/cancel, logout}` (adapter `routes.py`). Ported from xo-space's Connect Claude.
- `start` runs `claude auth login --claudeai` under a PTY (echo off) and returns the authorize URL; `login/code`
  types the `code#state` the owner pastes back. `invalid_code` is retryable. On success the CLI writes its own
  `~/.claude/.credentials.json`, and `hasCompletedOnboarding` is set in `~/.claude.json`.
- Needs a PTY, so it works inside a space (Linux) and refuses on Windows.
- Every `claude` call (login, status, models, Remote Control, health) uses `cli.claude_binary()`:
  `CLAUDE_CLI_PATH` → the SDK's bundled CLI → PATH, so a login lands in the CLI chat turns actually run.

## Cursor (`adapters/cursor/`)

- Runs `cursor-agent -p --output-format stream-json` per turn (`cli.cursor_binary()`: `CURSOR_CLI_PATH` →
  `cursor-agent` on PATH → `~/.local/bin`). Package mirrors `claude_code/` module for module; `mcp_server.py`
  is the one extra.
- No system-prompt flag: the rules (`prompts.py`) travel in the prompt, which is one argv string (≤120 KB;
  `build_turn_prompt` trims the caller's context first).
- No HEARTH tools: the Change Record and Impact Report are inline, and the finish gate runs when a turn ends —
  `validate_candidate()`, then on failure a fix-up turn resuming the same chat (`--resume`), up to
  `max_validation_rounds`. The agent's summary ends with a "Needs human attention:" list.
- Web-app tools (`ChatSpec.tools`) reach Cursor as MCP server `client`: `mcp_server.py` (stdlib-only stdio
  JSON-RPC) relays each call over a per-turn loopback socket with a random token to `mcp_tools.ClientToolRelay`,
  which emits the `tool-call` event and waits on the `ToolResultBridge`.
- Cursor reports no tokens or cost: `Usage` has steps only, and `usage.py` serves runs from the index.
- Sign-in: `/api/connectors/cursor/{status, login/start, login/poll, login/cancel, logout}` (`login.py`) —
  `cursor-agent login` on pipes with `NO_OPEN_BROWSER=1`; the owner opens the link, no code to paste.
  `CURSOR_API_KEY` also works. Remote Control is not supported (start answers 403).

## Antigravity (`adapters/antigravity/`)

- Runs `agy -p <prompt> --model <name> --add-dir <workspace> --log-file <run log>` per turn (`cli.agy_binary()`:
  `AGY_CLI_PATH` → PATH → `~/.local/bin/agy`). Shape follows xo-space's antigravity adapter. agy ignores its cwd;
  `--add-dir` pins the workspace. `--model` takes only names from agy's catalog (manifest `model.allowed`).
- Model names differ by agy version and by Google account (one is offered "Gemini 3.5 Flash", another "Gemini 3.8
  Flash"), so `model.allowed` lists every known set. `_model_for` picks `HEARTH_ANTIGRAVITY_MODEL` (as given), the
  requested name, the remembered one, then the default. When agy rejects the model ("invalid model selection"),
  `pick_offered` takes the closest allowed name from its "Available models" list, the turn is retried once, and the
  choice is remembered in `$HEARTH_STATE_DIR/antigravity-model`. Keep `model.allowed` in step with the agy version
  Hearth-backend pins in `templates/space/install-agent-cli`.
- No JSON output: the conversation id comes from the run's `--log-file` (never agy's caches), the run's
  `brain/<id>/.system_generated/logs/transcript_full.jsonl` is tailed for progress and `tool-call` events, and its
  last tool-less `PLANNER_RESPONSE` is the reply. Tokens are agy's own client-side estimates from
  `conversations/<id>.db` (`tokens.py`, ported); no cost.
- Same finish gate as Cursor (fix-up turns continue the conversation with `--conversation`). Rules travel in the
  prompt (`prompts.py`). The web app's tools (`ChatSpec.tools`) are not offered to agy.
- Sign-in: `/api/connectors/antigravity/{status, login/start, login/code, login/poll, login/cancel, logout}`
  (`login.py`): agy has no login command, so a throwaway `agy -p` under a PTY (in an empty folder, under a
  read-only HEARTH policy) prints the Google link and takes the pasted code; success is agy's token file becoming
  usable. Logout removes that file. Remote Control is not supported.

## Security invariants (from the TDD — do not weaken)

- Claude Code never runs with permissions bypassed. `manifest.json` `flags.dangerously_skip_permissions`
  must stay `false`; the adapter refuses to start otherwise.
- The tool-layer policy (`services/hearth_agent/policy.py`) is enforced in the `PreToolUse` hook for every
  tool call. Edits outside the Impact Report scope, CI config, secrets and `.git/` are denied; network
  tools, `git push/remote/fetch`, `gh`, `sudo` are denied.
- `finish` is accepted only after `HarnessHooks.validate_candidate()` passes.
- Cursor (`adapters/cursor/hooks.py`): the same `ToolPolicy`, applied as Cursor permission rules written to the
  workspace's `.cursor/cli.json` for the run (deny wins, even under `--force`) and as a check of every
  `tool_call` on the stream; a forbidden call that *ran* kills the turn (job status `policy_violation`).
  `--sandbox enabled` always (`flags.sandbox` must stay `"enabled"`); `--force` only on turns that may edit
  (`flags.force`: `"edit_turns"` or `"never"`); the adapter refuses other values. For a run, `mcp_config.
  project_config` sets the workspace's own `.cursor/{cli,mcp,hooks}.json` aside and restores them after,
  refuses a symlinked `.cursor`, and the agent may never write `.cursor/**`.
- Antigravity (`adapters/antigravity/hooks.py`, `policy_hook.py`): never `--dangerously-skip-permissions`
  (`flags.dangerously_skip_permissions` must stay `false`; it also switches agy's hooks off). HEARTH's
  `ToolPolicy` runs in agy's `PreToolUse` hook, registered in `~/.gemini/config/hooks.json`: per run the adapter
  writes the policy under `$HEARTH_STATE_DIR/antigravity/policies/` keyed by workspace, the hook answers
  `allow`/`deny` (unknown tools, web tools and subagents are denied; it fails closed), and denials go to the
  audit stream. agy's `toolPermission` is set to `always-proceed` so calls the hook allows are not auto-denied
  in headless mode. The workspace's own `.agents/hooks.json` is set aside for the run. Workspaces with no HEARTH
  policy get `ask` (agy's own rules). The hook path and headless behaviour come from agy's docs and bug reports,
  not a live check: confirm them on the agy version a space ships.
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
- The Claude credential lives only in the CLI's own store. The sign-in never logs, returns or echoes the code
  or a token; it runs without `ANTHROPIC_API_KEY`/`ANTHROPIC_OAUTH_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, and
  `status` reports account details only (`login.describe`).

## Testing

- `python -m unittest discover -s tests -t .` is the gate; `python -m pytest -q` also works.
- Tests are plain `unittest.TestCase` / `IsolatedAsyncioTestCase`. No test calls a model or GitHub.
