"""
REST routes for chat turns from the Hearth web app, resolved through the active agent's adapter.

  POST /api/chat                          — run one turn; the response is a Server-Sent Events stream
                                            of ``engine.stream_events`` (one ``data:`` JSON per event),
                                            ending with the ``done`` event
  POST /api/chat/{session}/tool-results   — deliver the result of a web-app tool the running turn called

The turn runs in ``ChatSpec.directory`` under the workspace root (created if missing). A session runs
one turn at a time: a second turn for a busy session answers 409. Closing the stream stops the turn.
"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.hearth_agent.engine import chat_bridge
from services.hearth_agent.engine import stream_events as se
from services.hearth_agent.engine.dispatcher import AgentDispatcher
from services.hearth_agent.models import ChatSpec
from services.storage import workspace

router = APIRouter()


@router.post("/api/chat")
async def chat(body: ChatSpec):
    dispatcher = AgentDispatcher()
    if not dispatcher.supports_chat:
        raise HTTPException(status_code=501, detail="The active agent does not support chat.")
    try:
        directory = workspace.resolve(body.directory)
    except workspace.WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if directory.exists() and not directory.is_dir():
        raise HTTPException(status_code=400, detail=f"{body.directory} is a file, not a folder.")
    directory.mkdir(parents=True, exist_ok=True)
    if chat_bridge.get_bridge(body.session) is not None:
        raise HTTPException(status_code=409, detail=_BUSY)

    async def events():
        # The bridge lives exactly as long as this generator runs, so a stream that never starts
        # (the client left first) cannot leave the session marked busy.
        try:
            bridge = chat_bridge.open_bridge(body.session, body.limits.tool_result_timeout_seconds)
        except chat_bridge.SessionBusy:
            yield _sse(se.error(_BUSY))
            yield _sse(se.done(None, {"session": body.session, "status": "error"}))
            return
        try:
            async for event in dispatcher.chat(body, directory, bridge):
                yield _sse(event)
        finally:
            chat_bridge.close_bridge(body.session, bridge)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


_BUSY = "This conversation is already running a turn."


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


class ToolResultBody(BaseModel):
    tool_use_id: str = Field(min_length=1, max_length=200)
    output: str = Field(max_length=200_000)
    is_error: bool = False


@router.post("/api/chat/{session}/tool-results")
async def tool_result(session: str, body: ToolResultBody):
    bridge = chat_bridge.get_bridge(session)
    if bridge is None:
        raise HTTPException(status_code=404, detail="No turn is running for this conversation.")
    if not bridge.resolve(body.tool_use_id, body.output, body.is_error):
        raise HTTPException(status_code=409, detail="This tool call already has a result or the turn has ended.")
    return {"ok": True}
