"""cursor adapter-owned routes.

Mounted only when cursor is the active agent (``routers/hearth_agent/__init__.py`` resolves the active
agent's ``routes`` module via ``try_load_capability('routes')``).

Cursor sign-in (``login.py``), the space's Cursor account:

  * ``GET  /api/connectors/cursor/status``       → connected | needs_auth | unavailable
  * ``POST /api/connectors/cursor/login/start``  → {session_id, auth_url, expires_in}
  * ``POST /api/connectors/cursor/login/poll``   → pending | connected | failed | cancelled
  * ``POST /api/connectors/cursor/login/cancel``
  * ``POST /api/connectors/cursor/logout``

Remote Control, same paths as claude_code (``remote_control.py``; not supported, so start answers 403).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.hearth_agent.adapters.cursor import login, remote_control

router = APIRouter()


class LoginSessionBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


def _login_error(exc: login.LoginError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, login.UnknownLogin) else 400, detail=str(exc))


@router.get("/api/connectors/cursor/status")
async def cursor_status():
    """Whether Cursor in this space is signed in, and as whom. Never returns a credential."""
    return await login.status()


@router.post("/api/connectors/cursor/login/start")
async def cursor_login_start():
    """Run ``cursor-agent login`` and return the link the owner opens to approve."""
    try:
        return await login.start()
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/cursor/login/poll")
async def cursor_login_poll(body: LoginSessionBody):
    try:
        return await login.poll(body.session_id)
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/cursor/login/cancel")
async def cursor_login_cancel(body: LoginSessionBody):
    return await login.cancel(body.session_id)


@router.post("/api/connectors/cursor/logout")
async def cursor_logout():
    """Sign Cursor out of this space (``cursor-agent logout``)."""
    try:
        return await login.logout()
    except login.LoginError as exc:
        raise _login_error(exc) from exc


class RemoteControlStartBody(BaseModel):
    name: str | None = None


@router.get("/api/remote-control/status")
def remote_control_status():
    return remote_control.status()


@router.post("/api/remote-control/start")
def remote_control_start(body: RemoteControlStartBody | None = None):
    return JSONResponse(status_code=403, content=remote_control.start(name=body.name if body else None))


@router.post("/api/remote-control/stop")
def remote_control_stop():
    return remote_control.stop()
