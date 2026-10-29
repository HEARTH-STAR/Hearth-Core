"""
cursor agents capability — the uniform agents contract, as in ``claude_code/agents.py``:

  list_agents()              -> list[dict]
  create_agent(body)         -> dict | JSONResponse
  get_detail(agent_id)       -> dict | None           # None if not ours
  patch(agent_id, body)      -> resp | None           # None if not ours
  delete(agent_id)           -> resp | None           # None if not ours

HEARTH's one agent is the built-in **migrator** — here Cursor, configured by
``config/agents/cursor/manifest.json`` with the rules in ``prompts.py``. It is changed by editing that
manifest, so create / patch / delete answer 405 for the migrator and ``None`` for anything else.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from services.hearth_agent.adapters.cursor.hooks import permission_rules
from services.hearth_agent.adapters.cursor.prompts import MIGRATOR_RULES
from services.hearth_agent.policy import ToolPolicy
from services.hearth_agent.registry.agent_registry import get_agent
from services.storage.layout import workspaces_dir

_BACKEND = "cursor"
MIGRATOR_ID = "migrator"
_NOT_SUPPORTED = "HEARTH has one built-in agent (the migrator); change it in config/agents/cursor/manifest.json."
_DESCRIPTION = "Applies one provider change to a repository and proves it with the repo's own tests."


def _job_permissions() -> dict[str, list[str]]:
    """The permission rules a job runs with when its Impact Report puts no limit on the files."""
    return permission_rules(ToolPolicy(), files=True, shell=True, client_tools=False)


def _agent_info() -> dict:
    manifest = get_agent(_BACKEND)
    rules = _job_permissions()
    return {
        "name": MIGRATOR_ID,
        "description": _DESCRIPTION,
        "mode": "primary",
        "tools": rules["allow"],
        "permissions": {"rules": [{"deny": rule} for rule in rules["deny"]]},
        "system_prompt": MIGRATOR_RULES,
        "temperature": None,
        "metadata": {
            "backend": _BACKEND,
            "display_name": "Migrator",
            "workspace": str(workspaces_dir()),
            "model": manifest.model_default,
            "builtin": True,
        },
    }


def list_agents() -> list[dict]:
    return [_agent_info()]


def create_agent(body) -> dict | JSONResponse:
    return JSONResponse(status_code=405, content={"detail": _NOT_SUPPORTED})


def get_detail(agent_id: str) -> dict | None:
    if agent_id != MIGRATOR_ID:
        return None
    manifest = get_agent(_BACKEND)
    return {
        "id": MIGRATOR_ID,
        "display_name": "Migrator",
        "description": _DESCRIPTION,
        "workspace": str(workspaces_dir()),
        "model": manifest.model_default,
        "model_raw": manifest.raw.get("model"),
        "identity": {"name": "HEARTH migrator", "emoji": None, "bio": None},
        "config_entry": {"flags": manifest.flags, "mcp": manifest.raw.get("mcp"), "permissions": _job_permissions()},
        "agents_defaults": {},
        "workspace_files": {},
        "on_disk": {
            "agent_dir": str(workspaces_dir()),
            "models_catalog": None,
            "auth_state": None,
            "auth_profiles": None,
        },
        "sessions": {"index_path": None, "count": 0, "session_ids": []},
        "backend": _BACKEND,
        "builtin": True,
    }


def patch(agent_id: str, body) -> dict | JSONResponse | None:
    if agent_id != MIGRATOR_ID:
        return None
    return JSONResponse(status_code=405, content={"detail": _NOT_SUPPORTED})


def delete(agent_id: str) -> dict | JSONResponse | None:
    if agent_id != MIGRATOR_ID:
        return None
    return JSONResponse(status_code=405, content={"detail": _NOT_SUPPORTED})
