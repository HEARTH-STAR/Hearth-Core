"""Router aggregation for the hearth_agent subpackage.

Each route module exposes a single ``router: APIRouter``. ``all_routers`` is the
ordered list ``server.py`` uses to mount them onto the FastAPI app.

Shape mirrors xo-space ``routers/cowork_agent/__init__.py``, including mounting
the active agent's own ``routes`` capability when it has one.
"""

from fastapi import APIRouter

from services.hearth_agent.adapters.loader import try_load_capability

from .agents import router as agents_router
from .apidiff import router as apidiff_router
from .chat import router as chat_router
from .connectors.github_cli import router as github_cli_router
from .connectors.github_pat import router as github_pat_router
from .models import router as models_router
from .repos import router as repos_router
from .sessions import router as sessions_router
from .usage import router as usage_router
from .watch import router as watch_router
from .workspace import router as workspace_router


def _active_agent_routes() -> list[APIRouter]:
    """Mount ``services/hearth_agent/adapters/<AGENT_NAME>/routes.py`` when the agent provides one."""
    mod = try_load_capability("routes")
    router = getattr(mod, "router", None) if mod else None
    return [router] if router is not None else []


all_routers: list[APIRouter] = [
    sessions_router,
    chat_router,
    workspace_router,
    agents_router,
    models_router,
    usage_router,
    *_active_agent_routes(),
    apidiff_router,
    watch_router,
    repos_router,
    github_pat_router,
    github_cli_router,
]
