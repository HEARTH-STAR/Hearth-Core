"""
REST routes for reading the space workspace (``services/storage/workspace.py``).

  GET /api/workspace/files?directory=dots/scout   — every file under a folder, paths relative to it
  GET /api/workspace/file?path=dots/scout/a.md    — one file's text (``binary: true`` and no text otherwise)

Read-only: the agent changes files, the owner reads them. Paths are relative to the workspace root;
anything that leaves it answers 400.
"""

from fastapi import APIRouter, HTTPException

from services.storage import workspace

router = APIRouter()


@router.get("/api/workspace/files")
def list_files(directory: str = ""):
    try:
        return workspace.list_files(directory)
    except workspace.WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/workspace/file")
def read_file(path: str):
    try:
        return workspace.read_file(path)
    except workspace.WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found.") from exc
