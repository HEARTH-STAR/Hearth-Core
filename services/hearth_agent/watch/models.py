"""Records of the API watch: watched specs, their check history, detections and the fix behind each.

Kept as JSON in ``$HEARTH_STATE_DIR/watch/state.json`` (see ``store.py``). A detection's id is its
Change Record id, which is stable for one diff: that id is the dedupe key, so one breaking change gets
one detection and at most one PR.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from services.hearth_agent.models import ChangeRecord, ImpactMatch, ValidationResult

REPO_RE = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
BRANCH_RE = r"^[^\x00-\x20\x7f~^:?*\[\\-][^\x00-\x20\x7f~^:?*\[\\]{0,254}$"  # what git accepts, minus a leading "-" (an option)

WatchState = Literal["idle", "checking", "error"]
CheckResult = Literal["baseline", "unchanged", "changed", "error"]
FixStatus = Literal[
    "detected", "queued", "syncing", "scanning", "baseline", "fixing", "opening_pr",
    "pr_open", "merged", "closed", "no_impact", "failed",
]
RUNNING: frozenset[str] = frozenset({"queued", "syncing", "scanning", "baseline", "fixing", "opening_pr"})
ReviewDecision = Literal["approved", "changes_requested", "commented", "pending"]
HISTORY_LIMIT = 20


class WatchCheck(BaseModel):
    at: datetime
    result: CheckResult
    from_version: str | None = None
    to_version: str | None = None
    breaking: int = 0
    total: int = 0
    detection_id: str | None = None
    error: str | None = None


class WatchStatus(BaseModel):
    state: WatchState = "idle"
    last_checked_at: datetime | None = None
    last_changed_at: datetime | None = None
    next_check_at: datetime | None = None
    last_error: str | None = None
    spec_version: str | None = None
    spec_sha: str | None = None  # sha256 of the baseline spec, first 12 hex
    etag: str | None = None
    last_modified: str | None = None


def _commands(value: list[str]) -> list[str]:
    value = [c.strip() for c in value if c.strip()]
    if not value:
        raise ValueError("at least one validation command is required")
    if any(len(c) > 500 for c in value):
        raise ValueError("a validation command is longer than 500 characters")
    return value


class WatchInput(BaseModel):
    """What the owner sets when adding a watch."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=80)
    spec_url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://\S+$")
    repo: str = Field(max_length=200, pattern=REPO_RE)
    base_branch: str | None = Field(None, pattern=BRANCH_RE)
    validate_commands: list[str] = Field(min_length=1, max_length=10)
    interval_minutes: int = Field(60, ge=5, le=1440)
    auto_pr: bool = True

    @field_validator("validate_commands")
    @classmethod
    def _check_commands(cls, value: list[str]) -> list[str]:
        return _commands(value)


class WatchPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(None, min_length=1, max_length=80)
    spec_url: str | None = Field(None, min_length=8, max_length=2048, pattern=r"^https?://\S+$")
    base_branch: str | None = Field(None, pattern=BRANCH_RE)
    validate_commands: list[str] | None = Field(None, min_length=1, max_length=10)
    interval_minutes: int | None = Field(None, ge=5, le=1440)
    auto_pr: bool | None = None
    enabled: bool | None = None

    @field_validator("validate_commands")
    @classmethod
    def _check_commands(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _commands(value)


class Watch(BaseModel):
    id: str
    name: str
    spec_url: str
    repo: str
    base_branch: str | None = None
    validate_commands: list[str]
    interval_minutes: int = 60
    auto_pr: bool = True
    enabled: bool = True
    created_at: datetime
    status: WatchStatus = Field(default_factory=WatchStatus)
    history: list[WatchCheck] = Field(default_factory=list)

    def record(self, check: WatchCheck) -> None:
        self.history = [check, *self.history][:HISTORY_LIMIT]


class PullSummary(BaseModel):
    number: int
    url: str
    title: str
    state: Literal["open", "closed", "merged"] = "open"
    review_decision: ReviewDecision | None = None
    head_sha: str | None = None
    checked_at: datetime | None = None


class Fix(BaseModel):
    status: FixStatus = "detected"
    message: str | None = None
    updated_at: datetime
    attempts: int = 0
    branch: str | None = None
    pr: PullSummary | None = None
    agent: str | None = None
    agent_summary: str | None = None
    needs_human_attention: list[str] = Field(default_factory=list)
    baseline: ValidationResult | None = None
    validation: ValidationResult | None = None
    impact: list[ImpactMatch] = Field(default_factory=list)


class Detection(BaseModel):
    id: str  # == change_record.id
    watch_id: str
    provider: str
    detected_at: datetime
    from_version: str
    to_version: str
    from_sha: str
    to_sha: str
    summary: str
    severity: str
    breaking_count: int
    total_count: int
    change_record: ChangeRecord
    changes: list[dict[str, Any]] = Field(default_factory=list)  # every ApiChange of the diff, breaking first
    fix: Fix

    def summary_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"change_record", "changes", "from_sha", "to_sha"})

    def detail_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"change_record", "from_sha", "to_sha"})
