"""
Hearth-Core API server.

Runs inside each customer's space (and on a laptop for development). Exposes the
GitHub connector, the API diff engine and the agent framework over HTTP; the
Engine API in Hearth-backend and the custom UI (through the space agent's
tunnel) call it. It never listens on a public interface: HOST defaults to
loopback, and inside the space container the port is not published.

Run:   ./hearth-core.sh dev        or        python server.py
Shape mirrors xo-space ``server.py`` (lifespan → agent setup → mount routers).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from routers.hearth_agent import all_routers  # noqa: E402
from routers.status import all_routers as status_routers  # noqa: E402
from services.hearth_agent.adapters.loader import try_load_capability  # noqa: E402
from services.hearth_agent.registry.agent_registry import get_active_agent  # noqa: E402
from services.hearth_agent.watch.scheduler import enabled as watch_enabled  # noqa: E402
from services.hearth_agent.watch.scheduler import get_scheduler  # noqa: E402

log = logging.getLogger("hearth_core")
REPO_ROOT = Path(__file__).resolve().parent


def _run_agent_setup() -> None:
    """Run ``config/agents/<AGENT_NAME>/setup.sh`` unless boot installs are disabled (the space image preloads)."""
    if os.getenv("HEARTH_SKIP_BOOT_INSTALL", "1") == "1":
        return
    script = REPO_ROOT / "config" / "agents" / get_active_agent().name / "setup.sh"
    if not script.exists():
        return
    try:
        subprocess.run(["bash", str(script)], check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("agent setup failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent = get_active_agent()
    log.info("Hearth-Core starting: agent=%s model=%s", agent.name, agent.model_default)
    _run_agent_setup()
    telemetry = try_load_capability("session_telemetry")
    if telemetry is not None:
        telemetry.start_daemon()
    watch = get_scheduler() if watch_enabled() else None
    if watch is not None:
        watch.start()
    try:
        yield
    finally:
        if watch is not None:
            await watch.stop()
        if telemetry is not None:
            telemetry.stop_daemon()


app = FastAPI(title="Hearth-Core", version="0.1.0", lifespan=lifespan)

for _r in [*status_routers, *all_routers]:
    app.include_router(_r)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "agent": get_active_agent().name}


def event_loop(platform: str = sys.platform) -> str:
    """uvicorn's ``loop`` setting. On Windows uvicorn picks the selector loop whenever it runs a
    reloader (``./hearth-core.sh dev``), and that loop cannot start subprocesses, so every gh, git
    and claude call would fail. The proactor loop can; elsewhere uvicorn's own choice stands."""
    return "asyncio:ProactorEventLoop" if platform == "win32" else "auto"


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    uvicorn.run(
        "server:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5010")),
        reload=os.getenv("UVICORN_RELOAD", "false").lower() == "true",
        loop=event_loop(),
    )
