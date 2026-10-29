"""Run a watch's validation commands and compare a candidate with the baseline.

Each command runs with ``sh -c`` in a clean checkout (never the agent's workspace), with gh's token
variables unset and ``CI=1``. A command that already failed on the untouched repo cannot prove anything,
so it is ignored in the comparison; a command that passed there and fails on the candidate is a regression.
A baseline where *no* command passes is refused: there would be nothing to validate the fix with.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from services.hearth_agent.connectors.github.gh_api import STORE_BYPASS_ENV
from services.hearth_agent.models import ValidationCommandResult, ValidationResult
from utils.commands import redact, run

LOG_TAIL = 4000


def _timeout() -> float:
    try:
        return float(os.environ.get("HEARTH_WATCH_COMMAND_TIMEOUT", "1200"))
    except ValueError:
        return 1200.0


def _shell() -> str:
    return shutil.which("sh") or shutil.which("bash") or "sh"


async def run_commands(commands: list[str], cwd: Path, phase: Literal["baseline", "candidate"]) -> ValidationResult:
    results: list[ValidationCommandResult] = []
    for command in commands:
        r = await run([_shell(), "-c", command], cwd=cwd, timeout=_timeout(), env={"CI": "1"},
                      unset_env=STORE_BYPASS_ENV)
        if r.timed_out:
            code, tail = 124, f"timed out after {_timeout():.0f}s\n" + r.output[-LOG_TAIL:]
        elif r.binary_missing:
            code, tail = 127, "no shell to run the command"
        else:
            code, tail = r.returncode, r.output[-LOG_TAIL:]
        results.append(ValidationCommandResult(command=command, exit_code=code, duration_seconds=round(
            r.duration_seconds, 1), log_tail=redact(tail)))
    passed = all(c.exit_code == 0 for c in results)
    return ValidationResult(phase=phase, passed=passed, commands=results)


def usable_baseline(baseline: ValidationResult) -> bool:
    return any(c.exit_code == 0 for c in baseline.commands)


def compare(baseline: ValidationResult, candidate: ValidationResult) -> ValidationResult:
    """The candidate, with ``regressions`` = commands that passed at baseline and fail now."""
    ok_before = {c.command for c in baseline.commands if c.exit_code == 0}
    regressions = [c.command for c in candidate.commands if c.exit_code != 0 and c.command in ok_before]
    return candidate.model_copy(update={"regressions": regressions, "passed": not regressions})
