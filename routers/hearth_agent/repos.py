"""
Read-only browsing of the repos the API watch fixes (the web app's repo page).

  GET /api/repos                                     every watched repo, with its watches
  GET /api/repos/{owner}/{name}?ref=                 summary: default branch, size, newest commit, clone URLs
  GET /api/repos/{owner}/{name}/tree?ref=&path=      a folder, each entry with its last commit
  GET /api/repos/{owner}/{name}/blob?ref=&path=      a file's text (512 KB at most)
  GET /api/repos/{owner}/{name}/commits?ref=&path=&limit=&skip=
  GET /api/repos/{owner}/{name}/commits/{sha}        one commit with its per-file patches
  GET /api/repos/{owner}/{name}/branches

Only repos with an API watch answer (404 otherwise); everything is read from the watch's bare mirror.
"""

from fastapi import APIRouter, Path, Query
from fastapi.responses import JSONResponse

from services.hearth_agent.watch.browse import BrowseError, RepoBrowser, watched_repos
from services.hearth_agent.watch.store import get_store

router = APIRouter()

# One Path() per parameter: a shared one would fill both owner and name from the same segment.
OWNER = Path(pattern=r"^[A-Za-z0-9_.-]+$", max_length=100)
NAME = Path(pattern=r"^[A-Za-z0-9_.-]+$", max_length=100)


def _browser(owner: str, name: str) -> RepoBrowser:
    repo = f"{owner}/{name}"
    if repo not in watched_repos(get_store()):
        raise BrowseError(404, f"{repo} has no API watch in this Space.")
    return RepoBrowser(repo)


def _error(exc: BrowseError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@router.get("/api/repos")
async def list_repos() -> dict:
    repos = watched_repos(get_store())
    return {"repos": [{
        "repo": repo,
        "providers": [w.name for w in watches],
        "watch_ids": [w.id for w in watches],
        "branches": [w.base_branch for w in watches],
    } for repo, watches in sorted(repos.items())]}


@router.get("/api/repos/{owner}/{name}")
async def repo_summary(owner: str = OWNER, name: str = NAME, ref: str | None = Query(None, max_length=255)):
    try:
        return await _browser(owner, name).summary(ref)
    except BrowseError as exc:
        return _error(exc)


@router.get("/api/repos/{owner}/{name}/tree")
async def repo_tree(owner: str = OWNER, name: str = NAME, ref: str | None = Query(None, max_length=255),
                    path: str = Query("", max_length=1024)):
    try:
        return await _browser(owner, name).tree(ref, path)
    except BrowseError as exc:
        return _error(exc)


@router.get("/api/repos/{owner}/{name}/blob")
async def repo_blob(owner: str = OWNER, name: str = NAME, ref: str | None = Query(None, max_length=255),
                    path: str = Query(..., max_length=1024)):
    try:
        return await _browser(owner, name).blob(ref, path)
    except BrowseError as exc:
        return _error(exc)


@router.get("/api/repos/{owner}/{name}/commits")
async def repo_commits(owner: str = OWNER, name: str = NAME, ref: str | None = Query(None, max_length=255),
                       path: str = Query("", max_length=1024), limit: int = Query(30, ge=1, le=100),
                       skip: int = Query(0, ge=0)):
    try:
        return await _browser(owner, name).commits(ref, path, limit, skip)
    except BrowseError as exc:
        return _error(exc)


@router.get("/api/repos/{owner}/{name}/commits/{sha}")
async def repo_commit(owner: str = OWNER, name: str = NAME, sha: str = Path(pattern=r"^[0-9a-f]{7,40}$")):
    try:
        return await _browser(owner, name).commit_detail(sha)
    except BrowseError as exc:
        return _error(exc)


@router.get("/api/repos/{owner}/{name}/branches")
async def repo_branches(owner: str = OWNER, name: str = NAME):
    try:
        return await _browser(owner, name).branches()
    except BrowseError as exc:
        return _error(exc)
