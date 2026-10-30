"""
REST routes for the API watch: watched OpenAPI specs, their detections, and each detection's PR review.

  GET    /api/watch                                  poller status, watches, detections
  POST   /api/watch                                  add a watch (its first check records the baseline)
  POST   /api/watch/repo                             watch the APIs a repo uses (one watch per provider found)
  PATCH  /api/watch/{watch_id}                       edit / pause a watch
  DELETE /api/watch/{watch_id}                       remove a watch and its detections
  POST   /api/watch/{watch_id}/check                 check now
  GET    /api/watch/detections/{id}                  a detection with every change of its diff
  POST   /api/watch/detections/{id}/fix              start or retry the AI fix
  GET    /api/watch/detections/{id}/pull             the PR: state, reviews, checks, mergeability
  GET    /api/watch/detections/{id}/pull/files       changed files with their patches
  GET    /api/watch/detections/{id}/pull/comments    conversation, inline comments and reviews
  POST   /api/watch/detections/{id}/pull/comments    comment (inline with path + line)
  POST   /api/watch/detections/{id}/pull/reviews     approve / request changes / comment
  POST   /api/watch/detections/{id}/pull/merge       merge

PR routes act only on the PR a detection opened, in its watch's repo, through the Space's gh connection.
"""

import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Path, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from services.hearth_agent.connectors.github.gh_api import GhCli
from services.hearth_agent.connectors.github.repo import GitError
from services.hearth_agent.watch import checkout, discover, review
from services.hearth_agent.watch.fixer import link_pull
from services.hearth_agent.watch.models import RUNNING, Detection, RepoWatchInput, Watch, WatchInput, WatchPatch
from services.hearth_agent.watch.scheduler import get_scheduler
from services.hearth_agent.watch.store import get_store

router = APIRouter()

WATCH_ID = Path(pattern=r"^w_[0-9a-f]{12}$")
DETECTION_ID = Path(pattern=r"^cr_[0-9a-f]{12}$")


def _watch_view(w: Watch) -> dict:
    return w.model_dump(mode="json")


def _watch(watch_id: str) -> Watch:
    watch = get_store().watches.get(watch_id)
    if watch is None:
        raise HTTPException(404, detail="Watch not found.")
    return watch


def _detection(detection_id: str) -> tuple[Detection, Watch]:
    store = get_store()
    d = store.detections.get(detection_id)
    watch = store.watches.get(d.watch_id) if d else None
    if d is None or watch is None:
        raise HTTPException(404, detail="Detection not found.")
    return d, watch


def _pull(detection_id: str) -> tuple[Detection, Watch, int]:
    d, watch = _detection(detection_id)
    if d.fix.pr is None:
        raise HTTPException(404, detail="This detection has no pull request yet.")
    return d, watch, d.fix.pr.number


def _github_error(exc: review.GitHubError) -> JSONResponse:
    content: dict = {"detail": exc.detail}
    if exc.code:
        content["code"] = exc.code
    return JSONResponse(content, status_code=exc.status)


@router.get("/api/watch")
async def list_watches() -> dict:
    store = get_store()
    detections = sorted(store.detections.values(), key=lambda d: d.detected_at, reverse=True)
    return {
        "poller": get_scheduler().status(),
        "watches": [_watch_view(w) for w in sorted(store.watches.values(), key=lambda w: w.created_at)],
        "detections": [d.summary_view() for d in detections],
    }


@router.post("/api/watch", status_code=201)
async def add_watch(body: WatchInput) -> dict:
    store = get_store()
    watch = Watch(id=f"w_{secrets.token_hex(6)}", created_at=datetime.now(UTC), **body.model_dump())
    store.watches[watch.id] = watch
    store.save()
    get_scheduler().check_soon(watch)
    return _watch_view(watch)


@router.post("/api/watch/repo", status_code=201)
async def watch_repo(body: RepoWatchInput) -> dict:
    """Read the repo through the Space's GitHub connection, find the API providers its dependencies name, and
    watch each one's spec (fix PRs on by default). A provider already watched for this repo and branch is kept."""
    try:
        found = await discover.discover(body.repo, body.base_branch)
    except discover.DiscoveryError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except GitError as exc:
        raise HTTPException(400, detail=f"Could not read {body.repo}: {str(exc).removeprefix('git ')} "
                                        "Check the name, and that the GitHub account connected in Setup can see it.") from exc
    if not found.validate_commands:
        raise HTTPException(422, detail=f"Could not work out how to build or test {body.repo}.")
    store = get_store()
    created: list[Watch] = []
    existing: list[Watch] = []
    for entry in found.found:
        same = next((w for w in store.watches.values() if w.repo == body.repo and w.spec_url ==
                     entry.provider.spec_url and w.base_branch == body.base_branch), None)
        if same is not None:
            existing.append(same)
            continue
        watch = Watch(id=f"w_{secrets.token_hex(6)}", name=entry.provider.name, spec_url=entry.provider.spec_url,
                      repo=body.repo, base_branch=body.base_branch, validate_commands=found.validate_commands,
                      packages=sorted(entry.packages), created_at=datetime.now(UTC))
        store.watches[watch.id] = watch
        created.append(watch)
    store.save()
    for watch in created:
        get_scheduler().check_soon(watch)
    await checkout.refresh(body.repo, body.base_branch)  # the mirror discover just fetched
    return {
        "branch": found.branch,
        "checkout": checkout.relative_path(body.repo),
        "commit": found.commit,
        "providers": [{"id": e.provider.id, "name": e.provider.name, "packages": sorted(e.packages),
                       "files": sorted(e.files)} for e in found.found],
        "validate_commands": found.validate_commands,
        "watches": [_watch_view(w) for w in created],
        "existing": [_watch_view(w) for w in existing],
    }


@router.patch("/api/watch/{watch_id}")
async def edit_watch(body: WatchPatch, watch_id: str = WATCH_ID) -> dict:
    watch = _watch(watch_id)
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is not None or key == "base_branch":  # base_branch: null means the repo's default branch
            setattr(watch, key, value)
    status = watch.status
    if changes.get("spec_url"):  # a new URL starts over from a fresh baseline
        status.spec_sha = status.spec_version = status.etag = status.last_modified = None
    if not watch.enabled:
        status.next_check_at = None
    elif {"enabled", "interval_minutes", "spec_url"} & changes.keys():
        last = status.last_checked_at
        due_now = changes.get("spec_url") or last is None
        status.next_check_at = None if due_now else last + timedelta(minutes=watch.interval_minutes)
    get_store().save()
    return _watch_view(watch)


@router.delete("/api/watch/{watch_id}", status_code=204)
async def delete_watch(watch_id: str = WATCH_ID) -> Response:
    repo = _watch(watch_id).repo
    store = get_store()
    store.remove_watch(watch_id)
    if not any(w.repo == repo for w in store.watches.values()):
        checkout.remove(repo)
    return Response(status_code=204)


@router.post("/api/watch/{watch_id}/check", status_code=202)
async def check_watch(watch_id: str = WATCH_ID) -> dict:
    watch = _watch(watch_id)
    if not get_scheduler().check_soon(watch):
        raise HTTPException(409, detail="A check of this watch is already running.")
    return _watch_view(watch)


@router.get("/api/watch/detections/{detection_id}")
async def get_detection(detection_id: str = DETECTION_ID) -> dict:
    d, _ = _detection(detection_id)
    return d.detail_view()


@router.post("/api/watch/detections/{detection_id}/fix", status_code=202)
async def fix_detection(detection_id: str = DETECTION_ID) -> dict:
    d, _ = _detection(detection_id)
    if d.fix.status in RUNNING:
        raise HTTPException(409, detail="A fix for this change is already running.")
    if d.fix.status in ("pr_open", "merged"):
        raise HTTPException(409, detail=f"This change already has pull request #{d.fix.pr.number if d.fix.pr else '?'}.")
    get_scheduler().enqueue(detection_id)
    return d.summary_view()


# -- pull request review --------------------------------------------------------


class CommentBody(BaseModel):
    model_config = {"extra": "forbid"}

    body: str = Field(min_length=1, max_length=65536)
    path: str | None = Field(None, min_length=1, max_length=1000)
    line: int | None = Field(None, ge=1)

    @model_validator(mode="after")
    def _line_needs_path(self):
        if self.line is not None and not self.path:
            raise ValueError("line needs path")
        return self


class ReviewBody(BaseModel):
    model_config = {"extra": "forbid"}

    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"]
    body: str = Field("", max_length=65536)

    @model_validator(mode="after")
    def _body_unless_approve(self):
        if self.event != "APPROVE" and not self.body.strip():
            raise ValueError("a review that requests changes or comments needs a body")
        return self


class MergeBody(BaseModel):
    model_config = {"extra": "forbid"}

    method: Literal["merge", "squash", "rebase"] = "squash"


@router.get("/api/watch/detections/{detection_id}/pull")
async def get_pull(detection_id: str = DETECTION_ID):
    _, watch, number = _pull(detection_id)
    try:
        return await review.pull_detail(GhCli(), watch.repo, number)
    except review.GitHubError as exc:
        return _github_error(exc)


@router.get("/api/watch/detections/{detection_id}/pull/files")
async def get_pull_files(detection_id: str = DETECTION_ID):
    _, watch, number = _pull(detection_id)
    try:
        return {"files": await review.pull_files(GhCli(), watch.repo, number)}
    except review.GitHubError as exc:
        return _github_error(exc)


@router.get("/api/watch/detections/{detection_id}/pull/comments")
async def get_pull_comments(detection_id: str = DETECTION_ID):
    _, watch, number = _pull(detection_id)
    try:
        return {"comments": await review.pull_comments(GhCli(), watch.repo, number)}
    except review.GitHubError as exc:
        return _github_error(exc)


@router.post("/api/watch/detections/{detection_id}/pull/comments", status_code=201)
async def post_pull_comment(body: CommentBody, detection_id: str = DETECTION_ID):
    _, watch, number = _pull(detection_id)
    try:
        return await review.add_comment(GhCli(), watch.repo, number, body.body, path=body.path, line=body.line)
    except review.GitHubError as exc:
        return _github_error(exc)


@router.post("/api/watch/detections/{detection_id}/pull/reviews", status_code=201)
async def post_pull_review(body: ReviewBody, detection_id: str = DETECTION_ID):
    d, watch, number = _pull(detection_id)
    gh = GhCli()
    try:
        out = await review.submit_review(gh, watch.repo, number, body.event, body.body)
    except review.GitHubError as exc:
        return _github_error(exc)
    await get_scheduler().sync_pull(get_store(), d.id, gh=gh)
    return out


@router.post("/api/watch/detections/{detection_id}/pull/merge")
async def merge_pull(body: MergeBody, detection_id: str = DETECTION_ID):
    d, watch, number = _pull(detection_id)
    gh = GhCli()
    try:
        out = await review.merge(gh, watch.repo, number, body.method)
    except review.GitHubError as exc:
        return _github_error(exc)
    if out["merged"] and d.fix.pr is not None:
        link_pull(d, {"number": number, "html_url": d.fix.pr.url, "title": d.fix.pr.title, "merged": True,
                      "state": "closed", "head": {"sha": d.fix.pr.head_sha}})
        d.fix.message = f"Merged pull request #{number}."
        get_store().save()
    return out
