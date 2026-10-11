"""The folders of the machine-local state root (``~/.hearth/`` or ``$HEARTH_STATE_DIR``).

    ~/.hearth/
      secrets/      token_store's token.json (credential *metadata*; the GitHub token itself lives in gh)
      mirrors/      bare repo mirrors the PR Service fetches with gh's credential helper
      checkouts/    per-job worktrees used only to apply a diff and push
      workspaces/   sandbox workspaces (one dir per job step)
      jobs/         per-job spec / result files
      audit/        daily JSONL audit files
      workspace/    chat sessions' workspace, unless HEARTH_WORKSPACE_DIR points elsewhere
                    (``/workspace`` in a space container); see ``services/storage/workspace.py``
"""

from __future__ import annotations

from pathlib import Path

from services.storage.paths import hearth_state_dir


def secrets_dir() -> Path:
    return hearth_state_dir() / "secrets"


def mirrors_dir() -> Path:
    return hearth_state_dir() / "mirrors"


def checkouts_dir() -> Path:
    return hearth_state_dir() / "checkouts"


def workspaces_dir() -> Path:
    return hearth_state_dir() / "workspaces"


def jobs_dir() -> Path:
    return hearth_state_dir() / "jobs"


def audit_dir() -> Path:
    return hearth_state_dir() / "audit"


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
