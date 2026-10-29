# Contributing to Hearth-Core

## Setup

```bash
./install.sh --no-start  # uv venv + requirements.txt + .env from .env.example
./hearth-core.sh test
./hearth-core.sh dev
```

Requires Python 3.11+, `git`, and for real jobs the `gh` and `claude` CLIs.

## Rules

1. Keep route handlers thin. Logic goes in `services/`.
2. Every subprocess goes through `utils/commands.run` with an argv list. Use `safe_arg` for any value that
   came from a user, a repo, a changelog or an API response.
3. Core code never names a specific agent; reach agent code through `services/hearth_agent/adapters/loader.py`.
4. Do not weaken the security invariants listed in `CLAUDE.md`. A change that touches `policy.py`,
   `adapters/claude_code/hooks.py`, `connectors/github/repo.py` or the GitHub credential handling needs a test
   that shows the invariant still holds.
5. Tests are `unittest.TestCase` / `IsolatedAsyncioTestCase` and must not call a model, GitHub or the network.

## Commits

Small, focused commits with an imperative subject line (`apidiff: detect renamed OpenAPI params`).
