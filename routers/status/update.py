"""
Hearth-Core's own version, and its update to the newest commit of its branch (Space containers only).

  GET  /api/core/version   running commit, newest commit on the branch, update_available (?refresh=1 re-fetches)
  POST /api/core/update    move to the newest commit and restart; 409 while an API watch fix runs
"""

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from services.hearth_agent.watch.scheduler import get_scheduler
from services.update import core

router = APIRouter()


@router.get("/api/core/version")
async def core_version(refresh: bool = Query(False)) -> dict:
    return await core.version(refresh=refresh)


@router.post("/api/core/update")
async def core_update():
    active = get_scheduler().status()["active"]
    busy = "An API watch fix is running; a restart would stop it." if active else None
    try:
        return await core.update(busy=busy)
    except core.UpdateError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)
