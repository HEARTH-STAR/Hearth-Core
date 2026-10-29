"""antigravity adapter-owned routes.

Mounted only when antigravity is the active agent (``routers/hearth_agent/__init__.py`` resolves the active
agent's ``routes`` module via ``try_load_capability('routes')``).

Antigravity sign-in (``login.py``), the space's Google account for agy — xo-space's Connect Antigravity,
polled like claude_code's instead of streamed:

  * ``GET  /api/connectors/antigravity/status``       → connected | needs_auth | unavailable
  * ``POST /api/connectors/antigravity/login/start``  → {session_id, auth_url, expires_in}
  * ``POST /api/connectors/antigravity/login/code``   → type the pasted code
  * ``POST /api/connectors/antigravity/login/poll``   → pending | connected | failed | cancelled
  * ``POST /api/connectors/antigravity/login/cancel``
  * ``POST /api/connectors/antigravity/logout``

Remote Control, same paths as claude_code (``remote_control.py``; not supported, so start answers 403).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.hearth_agent.adapters.antigravity import login, remote_control

router = APIRouter()


class LoginSessionBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class LoginCodeBody(LoginSessionBody):
    code: str = Field(min_length=1, max_length=4096)


def _login_error(exc: login.LoginError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, login.UnknownLogin) else 400, detail=str(exc))


@router.get("/api/connectors/antigravity/status")
async def antigravity_status():
    """Whether agy in this space is signed in. Never returns a credential."""
    return await login.status()


@router.post("/api/connectors/antigravity/login/start")
async def antigravity_login_start():
    """Run a throwaway ``agy -p`` and return the Google link the owner opens to approve."""
    try:
        return await login.start()
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/antigravity/login/code")
async def antigravity_login_code(body: LoginCodeBody):
    """Hand agy the code Google showed (or the redirect URL that carries it)."""
    try:
        return await login.submit(body.session_id, body.code)
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/antigravity/login/poll")
async def antigravity_login_poll(body: LoginSessionBody):
    try:
        return await login.poll(body.session_id)
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/antigravity/login/cancel")
async def antigravity_login_cancel(body: LoginSessionBody):
    return await login.cancel(body.session_id)


@router.post("/api/connectors/antigravity/logout")
async def antigravity_logout():
    """Sign agy out of this space (removes its token file)."""
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
