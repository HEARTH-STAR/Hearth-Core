"""
REST routes for the API diff engine.

  POST /api/apidiff   — diff two API surfaces given inline (OpenAPI JSON/YAML, Python source, or .d.ts)
"""

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apidiff import (
    diff_surfaces,
    extract_openapi,
    extract_python_module,
    extract_typescript,
    to_change_record,
)
from apidiff.openapi import parse_spec_text
from services.hearth_agent.models import PackageRef

router = APIRouter()


class DiffBody(BaseModel):
    language: Literal["openapi", "python", "typescript"]
    old: str
    new: str
    module: str = ""
    provider: str | None = None
    package: PackageRef | None = None


def _extract(language: str, source: str, module: str):
    if language == "openapi":
        return extract_openapi(parse_spec_text(source))  # parse as text only; never read it as a server path
    if language == "python":
        return extract_python_module(source, module or "module")
    return extract_typescript(source, module)


@router.post("/api/apidiff")
async def apidiff(body: DiffBody) -> JSONResponse:
    try:
        changes = diff_surfaces(_extract(body.language, body.old, body.module),
                                _extract(body.language, body.new, body.module))
    except (SyntaxError, ValueError) as exc:
        raise HTTPException(400, detail=f"Could not parse input: {exc}") from exc
    payload: dict = {
        "changes": [{**asdict(c), "breaking": c.breaking} for c in changes],
        "breaking": sum(c.breaking for c in changes),
    }
    if body.provider and body.package:
        record = to_change_record(changes, provider=body.provider, package=body.package)
        payload["change_record"] = record.model_dump(mode="json", by_alias=True) if record else None
    return JSONResponse(payload)
