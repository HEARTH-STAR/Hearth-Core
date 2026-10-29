"""Review a detection's pull request on GitHub through the Space's own gh connection.

Everything goes through :class:`GhCli` (gh holds the token; HEARTH never sees it). Lists are paged by
hand: ``gh api --paginate`` prints one JSON document per page, which ``GhCli.api`` does not parse.
GitHub's answer is passed on as a :class:`GitHubError` with an HTTP status the route can return as is
(e.g. 405 "Pull Request is not mergeable", 422 "Can not approve your own pull request").
"""

from __future__ import annotations

import re
from typing import Any, Literal

from services.hearth_agent.connectors.github.gh_api import GhCli, GhErrorKind, GhResult
from services.hearth_agent.connectors.token_store import get_entry
from services.hearth_agent.watch.models import ReviewDecision

MAX_PAGES = 30  # GitHub lists at most 3000 files per pull request

_STATUS_BY_KIND = {
    GhErrorKind.NOT_AUTHENTICATED: 409,
    GhErrorKind.NO_CLI: 503,
    GhErrorKind.TIMEOUT: 504,
    GhErrorKind.RATE_LIMITED: 429,
    GhErrorKind.FORBIDDEN: 403,
    GhErrorKind.NOT_FOUND: 404,
    GhErrorKind.CONFLICT: 409,
    GhErrorKind.NETWORK: 502,
}
_PASS_THROUGH = {403, 404, 405, 409, 422}


class GitHubError(RuntimeError):
    def __init__(self, status: int, detail: str, code: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


def _clean(message: str) -> str:
    text = message.strip().splitlines()[0] if message.strip() else ""
    text = re.sub(r"^gh:\s*", "", text)
    return re.sub(r"\s*\(HTTP \d{3}\)\s*$", "", text) or "GitHub request failed"


def _ok(r: GhResult) -> Any:
    if r.ok:
        return r.data
    if r.kind == GhErrorKind.NOT_AUTHENTICATED:
        raise GitHubError(409, "GitHub is not connected in this Space. Connect it in Setup.", "github_not_connected")
    status = r.status if r.status in _PASS_THROUGH else _STATUS_BY_KIND.get(r.kind or GhErrorKind.ERROR, 502)
    raise GitHubError(status, _clean(r.message))


async def _pages(gh: GhCli, path: str) -> list[dict[str, Any]]:
    sep = "&" if "?" in path else "?"
    out: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _ok(await gh.api(f"{path}{sep}per_page=100&page={page}")) or []
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def _login(user: Any) -> str:
    return (user or {}).get("login") or "ghost"


async def viewer(gh: GhCli) -> str | None:
    """The gh account's login: the one HEARTH saved at connect time, else asked from GitHub."""
    login = (get_entry("github", read_only=True) or {}).get("login")
    if login:
        return login
    r = await gh.api("user")
    return (r.data or {}).get("login") if r.ok else None


async def find_pull(gh: GhCli, repo: str, branch: str) -> dict[str, Any] | None:
    """The newest pull request (any state) whose head is ``branch`` in ``repo``: the GitHub-side dedupe check."""
    owner = repo.split("/", 1)[0]
    pulls = _ok(await gh.api(f"repos/{repo}/pulls?head={owner}:{branch}&state=all&per_page=10")) or []
    return pulls[0] if pulls else None


def pull_state(pull: dict[str, Any]) -> Literal["open", "closed", "merged"]:
    if pull.get("merged") or pull.get("merged_at"):
        return "merged"
    return "open" if pull.get("state") == "open" else "closed"


def review_decision(reviews: list[dict[str, Any]]) -> ReviewDecision:
    """GitHub's rule: each reviewer's latest approve / request-changes counts; any request wins."""
    latest: dict[str, str] = {}
    commented = False
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[_login(r.get("user"))] = state
        elif state == "COMMENTED":
            commented = True
    if "CHANGES_REQUESTED" in latest.values():
        return "changes_requested"
    if "APPROVED" in latest.values():
        return "approved"
    return "commented" if commented else "pending"


def _review_view(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": r["id"], "user": _login(r.get("user")), "state": r.get("state") or "COMMENTED",
            "body": r.get("body") or "", "submitted_at": r.get("submitted_at")}


async def _checks(gh: GhCli, repo: str, sha: str) -> dict[str, Any]:
    """Combined commit statuses and check runs of the head commit, folded into one state."""
    states: list[str] = []
    status = await gh.api(f"repos/{repo}/commits/{sha}/status")
    if status.ok and status.data:
        states += [s.get("state") or "pending" for s in status.data.get("statuses") or []]
    runs = await gh.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    if runs.ok and runs.data:
        for run in runs.data.get("check_runs") or []:
            if run.get("status") != "completed":
                states.append("pending")
            else:
                ok = run.get("conclusion") in ("success", "neutral", "skipped")
                states.append("success" if ok else "failure")
    if not states:
        return {"state": None, "total": 0}
    state = "failure" if {"failure", "error"} & set(states) else "pending" if "pending" in states else "success"
    return {"state": state, "total": len(states)}


async def pull_detail(gh: GhCli, repo: str, number: int) -> dict[str, Any]:
    pull = _ok(await gh.api(f"repos/{repo}/pulls/{number}"))
    reviews = await _pages(gh, f"repos/{repo}/pulls/{number}/reviews")
    me = await viewer(gh)
    author = _login(pull.get("user"))
    head_sha = (pull.get("head") or {}).get("sha") or ""
    return {
        "number": pull["number"],
        "title": pull.get("title") or "",
        "body": pull.get("body") or "",
        "url": pull.get("html_url") or "",
        "state": pull_state(pull),
        "draft": bool(pull.get("draft")),
        "author": author,
        "viewer": me,
        "viewer_is_author": bool(me) and me.lower() == author.lower(),
        "head": {"ref": (pull.get("head") or {}).get("ref") or "", "sha": head_sha},
        "base": {"ref": (pull.get("base") or {}).get("ref") or ""},
        "mergeable": pull.get("mergeable"),
        "mergeable_state": pull.get("mergeable_state") or "unknown",
        "additions": pull.get("additions") or 0,
        "deletions": pull.get("deletions") or 0,
        "changed_files": pull.get("changed_files") or 0,
        "created_at": pull.get("created_at"),
        "updated_at": pull.get("updated_at"),
        "merged_at": pull.get("merged_at"),
        "review_decision": review_decision(reviews),
        "reviews": [_review_view(r) for r in reviews],
        "checks": await _checks(gh, repo, head_sha) if head_sha else {"state": None, "total": 0},
    }


async def pull_files(gh: GhCli, repo: str, number: int) -> list[dict[str, Any]]:
    return [{
        "filename": f["filename"],
        "previous_filename": f.get("previous_filename"),
        "status": f.get("status") or "modified",
        "additions": f.get("additions") or 0,
        "deletions": f.get("deletions") or 0,
        "patch": f.get("patch"),
    } for f in await _pages(gh, f"repos/{repo}/pulls/{number}/files")]


def _comment_view(c: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": c["id"],
        "kind": kind,
        "user": _login(c.get("user")),
        "body": c.get("body") or "",
        "created_at": c.get("created_at") or c.get("submitted_at"),
        "path": c.get("path") if kind == "review_comment" else None,
        "line": (c.get("line") or c.get("original_line")) if kind == "review_comment" else None,
        "state": c.get("state") if kind == "review" else None,
        "url": c.get("html_url"),
    }


async def pull_comments(gh: GhCli, repo: str, number: int) -> list[dict[str, Any]]:
    """Conversation and inline comments, plus reviews that carry a verdict or text, oldest first."""
    issue = await _pages(gh, f"repos/{repo}/issues/{number}/comments")
    inline = await _pages(gh, f"repos/{repo}/pulls/{number}/comments")
    reviews = await _pages(gh, f"repos/{repo}/pulls/{number}/reviews")
    items = [_comment_view(c, "issue") for c in issue] + [_comment_view(c, "review_comment") for c in inline]
    items += [_comment_view(r, "review") for r in reviews if r.get("body") or r.get("state") != "COMMENTED"]
    return sorted(items, key=lambda c: c["created_at"] or "")


async def add_comment(gh: GhCli, repo: str, number: int, body: str, *, path: str | None = None,
                      line: int | None = None) -> dict[str, Any]:
    if path and line:
        pull = _ok(await gh.api(f"repos/{repo}/pulls/{number}"))
        c = _ok(await gh.api(f"repos/{repo}/pulls/{number}/comments", method="POST", fields={
            "body": body, "commit_id": pull["head"]["sha"], "path": path, "line": line, "side": "RIGHT"}))
        return _comment_view(c, "review_comment")
    return _comment_view(_ok(await gh.comment(repo, number, body)), "issue")


async def submit_review(gh: GhCli, repo: str, number: int, event: str, body: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"event": event}
    if body:
        fields["body"] = body
    return _review_view(_ok(await gh.api(f"repos/{repo}/pulls/{number}/reviews", method="POST", fields=fields)))


async def merge(gh: GhCli, repo: str, number: int, method: str) -> dict[str, Any]:
    data = _ok(await gh.api(f"repos/{repo}/pulls/{number}/merge", method="PUT", fields={"merge_method": method}))
    return {"merged": bool(data.get("merged")), "sha": data.get("sha"), "message": data.get("message") or ""}
