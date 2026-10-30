"""Turn a detection into a validated pull request.

    syncing     find an existing PR for this change on GitHub (dedupe), else fetch the repo (bare mirror)
    scanning    export the base commit (no remote, no credential) and find the call sites
    baseline    run the watch's validation commands on the untouched repo, in a clean checkout
    fixing      the active agent edits the export; its ``finish`` is accepted only when the same commands,
                re-run on a *fresh* checkout with only the agent's diff applied, show no regression
    opening_pr  dedupe again, push ``hearth/<provider>-<version>-<hash>`` and open the PR

The agent never pushes (policy denies git remotes and gh); this module does, with gh's credential helper.
Nothing is pushed unless the validation that accepted the agent's ``finish`` passed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.hearth_agent.adapters.base import HarnessHooks
from services.hearth_agent.connectors.github.gh_api import GhCli
from services.hearth_agent.connectors.github.pulls import branch_name, render_pr
from services.hearth_agent.connectors.github.repo import GitError, RepoMirror
from services.hearth_agent.engine.dispatcher import AgentDispatcher
from services.hearth_agent.models import ImpactReport, JobSpec, ValidationCommandResult, ValidationResult
from services.hearth_agent.watch import impact, review, validation
from services.hearth_agent.watch.models import Detection, FixStatus, PullSummary, Watch
from services.hearth_agent.watch.store import WatchStore
from services.storage import layout
from services.storage.paths import hearth_state_dir
from utils.commands import run

log = logging.getLogger(__name__)

REPO_INSTRUCTIONS = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTIONS = 20_000
MAX_LISTED_CHANGES = 40


def _now() -> datetime:
    return datetime.now(UTC)


def _rmtree(path: Path) -> None:
    """Remove a work dir, including git's read-only object files (which Windows refuses to delete as is)."""
    def retry(func, p, _exc) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if path.exists():
        shutil.rmtree(path, onerror=retry)


def _repo_instructions(root: Path) -> str | None:
    parts = []
    for name in REPO_INSTRUCTIONS:
        f = root / name
        if f.is_file():
            parts.append(f"# {name}\n\n" + f.read_text(encoding="utf-8", errors="replace")[:MAX_INSTRUCTIONS])
    return "\n\n".join(parts) or None


def link_pull(detection: Detection, pull: dict[str, Any], reviews: list[dict[str, Any]] | None = None) -> None:
    """Point the detection at ``pull`` and move its fix to pr_open / merged / closed."""
    state = review.pull_state(pull)
    previous = detection.fix.pr
    detection.fix.pr = PullSummary(
        number=pull["number"], url=pull.get("html_url") or "", title=pull.get("title") or "", state=state,
        review_decision=review.review_decision(reviews) if reviews is not None
        else (previous.review_decision if previous else None),
        head_sha=(pull.get("head") or {}).get("sha"), checked_at=_now(),
    )
    detection.fix.status = {"open": "pr_open", "merged": "merged", "closed": "closed"}[state]
    detection.fix.updated_at = _now()


def _pr_body(detection: Detection, body: str) -> str:
    """render_pr's body, minus the ``/hearth`` bot-command footer (not served here), plus every breaking change."""
    lines = [line for line in body.splitlines() if not line.startswith("Comment `/hearth")]
    breaking = [c for c in detection.changes if c.get("breaking")]
    table = ["", "## Breaking changes in the spec", "",
             f"`{detection.from_version}` → `{detection.to_version}`", "",
             "| Change | Previous | Current |", "|---|---|---|"]
    cell = lambda s: (s or "—").replace("|", "\\|").replace("\n", " ")[:200]  # noqa: E731
    for c in breaking[:MAX_LISTED_CHANGES]:
        table.append(f"| {cell(c.get('description'))} | `{cell(c.get('before'))}` | `{cell(c.get('after'))}` |")
    if len(breaking) > MAX_LISTED_CHANGES:
        table.append(f"\n…and {len(breaking) - MAX_LISTED_CHANGES} more.")
    footer = len(lines)
    while footer and not lines[footer - 1].startswith("---"):
        footer -= 1
    at = footer - 1 if footer else len(lines)
    return "\n".join([*lines[:at], *table, "", *lines[at:]])


class Fixer:
    def __init__(self, store: WatchStore, *, gh: GhCli | None = None,
                 dispatcher_factory: Callable[[], AgentDispatcher] = AgentDispatcher):
        self.store = store
        self.gh = gh or GhCli()
        self.dispatcher_factory = dispatcher_factory

    def _step(self, d: Detection, status: FixStatus, message: str | None = None) -> None:
        d.fix.status, d.fix.message, d.fix.updated_at = status, message, _now()
        self.store.save()

    async def run(self, detection_id: str) -> None:
        d = self.store.detections.get(detection_id)
        watch = self.store.watches.get(d.watch_id) if d else None
        if d is None or watch is None:
            return
        fix = d.fix
        fix.attempts += 1
        fix.agent_summary, fix.needs_human_attention, fix.baseline, fix.validation = None, [], None, None
        fix.branch = branch_name(d.change_record)
        job_id = f"{d.id}-{fix.attempts}"
        dirs = [layout.workspaces_dir() / f"{job_id}{suffix}" for suffix in ("", "-baseline", "-check")]
        dirs.append(layout.checkouts_dir() / job_id)
        try:
            await self._run(d, watch, job_id, *dirs[:3], dirs[3])
        except review.GitHubError as exc:
            self._step(d, "failed", f"GitHub: {exc.detail}")
        except GitError as exc:
            self._step(d, "failed", str(exc))
        except Exception as exc:
            log.exception("fix %s failed", d.id)
            self._step(d, "failed", f"{type(exc).__name__}: {exc}"[:500])
        finally:
            for path in dirs:
                await asyncio.to_thread(_rmtree, path)

    async def _run(self, d: Detection, watch: Watch, job_id: str, ws: Path, base_dir: Path, check_dir: Path,
                   checkout: Path) -> None:
        fix = d.fix
        assert fix.branch
        self._step(d, "syncing", f"Looking for an existing pull request, then fetching {watch.repo}")
        if existing := await review.find_pull(self.gh, watch.repo, fix.branch):
            link_pull(d, existing)
            self._step(d, fix.status, f"This change already has pull request #{existing['number']}.")
            return
        mirror = RepoMirror(hearth_state_dir(), watch.repo)
        await mirror.sync()
        base = watch.base_branch or await mirror.default_branch()
        commit = await mirror.resolve(base)

        self._step(d, "scanning", f"Finding call sites in {watch.repo}@{base}")
        for path in (ws, base_dir, check_dir):
            await asyncio.to_thread(_rmtree, path)
        await mirror.export_tree(commit, ws)
        fix.impact = await asyncio.to_thread(impact.scan, ws, d.changes, watch.packages)
        if not fix.impact:
            self._step(d, "no_impact", f"No call sites of the changed endpoints or fields were found in "
                                       f"{watch.repo}@{base}, so there is nothing to fix.")
            return

        self._step(d, "baseline", "Running the validation commands on the untouched repo")
        await mirror.export_tree(commit, base_dir)
        baseline = fix.baseline = await validation.run_commands(watch.validate_commands, base_dir, "baseline")
        if not validation.usable_baseline(baseline):
            self._step(d, "failed", f"Every validation command already fails on {base}, so a fix could not be "
                                    "validated. Fix the commands (or the repo) and retry.")
            return

        dispatcher = self.dispatcher_factory()
        fix.agent = dispatcher.agent_name
        self._step(d, "fixing", f"{fix.agent} is updating {len({m.path for m in fix.impact})} file(s)")

        async def validate_candidate() -> ValidationResult:
            patch = await RepoMirror.diff_against_baseline(ws)
            await asyncio.to_thread(_rmtree, check_dir)
            await mirror.export_tree(commit, check_dir)
            if patch.strip() and not await _apply(check_dir, patch):
                return ValidationResult(phase="candidate", passed=False, regressions=["git apply"], commands=[
                    ValidationCommandResult(command="git apply", exit_code=1, duration_seconds=0,
                                            log_tail="the agent's diff does not apply to the base commit")])
            candidate = await validation.run_commands(watch.validate_commands, check_dir, "candidate")
            return validation.compare(baseline, candidate)

        report = ImpactReport(change_record_id=d.id, repository=watch.repo, commit=commit, matches=fix.impact)
        job = JobSpec(job_id=job_id, intelligence=fix.agent, change_record=d.change_record, impact_report=report,
                      repo_instructions=_repo_instructions(ws), validate_commands=watch.validate_commands,
                      allowed_paths=report.files)
        result = await dispatcher.run(job, ws, HarnessHooks(validate_candidate=validate_candidate))
        fix.agent_summary, fix.needs_human_attention = result.summary or None, result.needs_human_attention
        fix.validation = result.validation
        if result.status != "passed":
            self._step(d, "failed", f"The agent stopped without a validated fix ({result.status})."
                       + (f" {result.summary}" if result.summary else ""))
            return
        if not (result.validation and result.validation.passed and not result.validation.regressions):
            self._step(d, "failed", "The fix was not validated; no pull request was opened.")
            return
        patch = await RepoMirror.diff_against_baseline(ws)
        if not patch.strip():
            self._step(d, "failed", "The agent finished without changing any file.")
            return

        self._step(d, "opening_pr", f"Pushing {fix.branch} and opening the pull request")
        if existing := await review.find_pull(self.gh, watch.repo, fix.branch):  # opened meanwhile elsewhere
            link_pull(d, existing)
            self._step(d, fix.status, f"This change already has pull request #{existing['number']}.")
            return
        content = render_pr(d.change_record, report, result, baseline=baseline)
        await mirror.push_patch(checkout_dir=checkout, base_commit=commit, branch=fix.branch, patch=patch,
                                message=content.title)
        pull = review._ok(await self.gh.create_pull_request(
            watch.repo, head=fix.branch, base=base, title=content.title, body=_pr_body(d, content.body),
            labels=content.labels))
        link_pull(d, pull, [])
        self._step(d, fix.status, f"Opened pull request #{pull['number']}.")


async def _apply(checkout: Path, patch: str) -> bool:
    patch_file = checkout.parent / f".{checkout.name}.patch"
    patch_file.write_text(patch, encoding="utf-8", newline="")  # as is: no \r\n on Windows
    try:
        r = await run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=checkout, timeout=120)
        return r.ok
    finally:
        patch_file.unlink(missing_ok=True)
