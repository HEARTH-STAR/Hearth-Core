"""claude_code adapter-owned routes.

Mounted only when claude_code is the active agent (``routers/hearth_agent/__init__.py``
resolves the active agent's ``routes`` module via ``try_load_capability('routes')``).

Claude Code sign-in (``login.py``), the space's Claude account:

  * ``GET  /api/connectors/claude-code/status``       → connected | needs_auth | unavailable
  * ``POST /api/connectors/claude-code/login/start``  → {session_id, auth_url, expires_in}
  * ``POST /api/connectors/claude-code/login/code``   → send the pasted ``code#state``
  * ``POST /api/connectors/claude-code/login/poll``   → pending | connected | invalid_code | failed
  * ``POST /api/connectors/claude-code/login/cancel``
  * ``POST /api/connectors/claude-code/logout``

Remote Control lifecycle:

  * ``GET  /api/remote-control/status`` → running, enabled, login presence, link
  * ``POST /api/remote-control/start``  → launch (idempotent); 403 while disabled
  * ``POST /api/remote-control/stop``   → stop (idempotent; always allowed)

Shape mirrors xo-space ``adapters/claude_code/routes.py``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.hearth_agent.adapters.claude_code import login, remote_control

router = APIRouter()


class LoginSessionBody(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class LoginCodeBody(LoginSessionBody):
    code: str = Field(min_length=1, max_length=4096)


def _login_error(exc: login.LoginError) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, login.UnknownLogin) else 400, detail=str(exc))


@router.get("/api/connectors/claude-code/status")
async def claude_code_status():
    """Whether Claude Code in this space is signed in, and as whom. Never returns a credential."""
    return await login.status()


@router.post("/api/connectors/claude-code/login/start")
async def claude_code_login_start():
    """Run ``claude auth login`` and return the link the owner opens to approve."""
    try:
        return await login.start()
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/claude-code/login/code")
async def claude_code_login_code(body: LoginCodeBody):
    """Hand the CLI the code Claude's page showed; ``invalid_code`` means paste it again."""
    try:
        return await login.submit(body.session_id, body.code)
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/claude-code/login/poll")
async def claude_code_login_poll(body: LoginSessionBody):
    try:
        return await login.poll(body.session_id)
    except login.LoginError as exc:
        raise _login_error(exc) from exc


@router.post("/api/connectors/claude-code/login/cancel")
async def claude_code_login_cancel(body: LoginSessionBody):
    return await login.cancel(body.session_id)


@router.post("/api/connectors/claude-code/logout")
async def claude_code_logout():
    """Sign Claude Code out of this space (``claude auth logout``)."""
    try:
        return await login.logout()
    except login.LoginError as exc:
        raise _login_error(exc) from exc


class RemoteControlStartBody(BaseModel):
    name: str | None = None


@router.get("/api/remote-control/status")
def remote_control_status():
    """Whether a Remote Control session is live, whether it is enabled, login presence and the link."""
    return remote_control.status()


@router.post("/api/remote-control/start")
def remote_control_start(body: RemoteControlStartBody | None = None):
    """Start the Remote Control session (idempotent). Refused with 403 unless the operator enabled it."""
    result = remote_control.start(name=body.name if body else None)
    if result.get("error") == "disabled":
        return JSONResponse(status_code=403, content=result)
    return result


@router.post("/api/remote-control/stop")
def remote_control_stop():
    """Stop the Remote Control session (idempotent)."""
    return remote_control.stop()
