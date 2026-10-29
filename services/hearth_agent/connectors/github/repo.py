"""Clone outside the sandbox, export a credential-free tree into it, push outside it (TDD §6 Credentials).

Layout on the space volume::

    mirrors/<owner>__<name>.git   bare mirror, fetched with gh's credential helper
    checkouts/<job_id>/           fresh worktree used only to apply the agent's diff and push

The sandbox receives :meth:`RepoMirror.export_tree` output: files at one commit, no ``.git`` dir, so it
has no remote and no credential. A throwaway local git repo is initialised there so the harness can
compute ``git diff`` of the agent's edits.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from utils.commands import CommandResult, run, safe_arg


class GitError(RuntimeError):
    def __init__(self, step: str, result: CommandResult):
        super().__init__(f"git {step} failed: {result.stderr.strip()[:500]}")
        self.step = step
        self.result = result


async def _git(*args: str, cwd: Path | None = None, timeout: float = 600) -> CommandResult:
    return await run(["git", *args], cwd=cwd, timeout=timeout, separate_stderr=True)


async def _check(step: str, coro) -> CommandResult:
    r = await coro
    if not r.ok:
        raise GitError(step, r)
    return r


@dataclass
class RepoMirror:
    root: Path  # space volume directory
    repo: str  # "owner/name"
    host: str = "github.com"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.repo}.git"  # no token, ever; gh supplies credentials

    @property
    def mirror_dir(self) -> Path:
        return self.root / "mirrors" / (self.repo.replace("/", "__") + ".git")

    async def sync(self) -> None:
        """Create or update the bare mirror."""
        if (self.mirror_dir / "HEAD").exists():
            await _check("fetch", _git("--git-dir", str(self.mirror_dir), "fetch", "--prune", "origin",
                                       "+refs/heads/*:refs/heads/*"))
            return
        self.mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        await _check("clone", _git("clone", "--bare", "--", self.url, str(self.mirror_dir)))

    async def resolve(self, ref: str) -> str:
        r = await _check("rev-parse", _git("--git-dir", str(self.mirror_dir), "rev-parse", "--verify",
                                           safe_arg(ref) + "^{commit}"))
        return r.stdout.strip()

    async def default_branch(self) -> str:
        r = await _check("symbolic-ref", _git("--git-dir", str(self.mirror_dir), "symbolic-ref", "--short", "HEAD"))
        return r.stdout.strip()

    async def export_tree(self, commit: str, dest: Path) -> None:
        """Write the tree at ``commit`` into ``dest`` with no remote and no credential.

        A local-only git repo with one baseline commit is created so ``diff_against_baseline`` works.
        """
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.tar"
        try:
            await _check("archive", _git("--git-dir", str(self.mirror_dir), "archive", "--format=tar",
                                         "-o", str(tmp), safe_arg(commit)))
            shutil.unpack_archive(str(tmp), str(dest), format="tar")
        finally:
            tmp.unlink(missing_ok=True)
        ident = ["-c", "user.name=hearth", "-c", "user.email=hearth@localhost", "-c", "commit.gpgsign=false"]
        await _check("init", _git("init", "-q", cwd=dest))
        await _check("add", _git("add", "-A", cwd=dest))
        await _check("commit", _git(*ident, "commit", "-q", "--allow-empty", "-m", f"baseline {commit}", cwd=dest))

    @staticmethod
    async def diff_against_baseline(workspace: Path) -> str:
        await _check("add", _git("add", "-A", cwd=workspace))
        r = await _check("diff", _git("diff", "--cached", "--binary", "HEAD", cwd=workspace))
        return r.stdout

    @staticmethod
    async def changed_files(workspace: Path) -> list[str]:
        await _check("add", _git("add", "-A", cwd=workspace))
        r = await _check("diff", _git("diff", "--cached", "--name-only", "HEAD", cwd=workspace))
        return [line for line in r.stdout.splitlines() if line]

    async def push_patch(
        self, *, checkout_dir: Path, base_commit: str, branch: str, patch: str, message: str,
        author_name: str = "hearth-bot", author_email: str = "hearth-bot@users.noreply.github.com",
    ) -> str:
        """Apply ``patch`` on a fresh worktree at ``base_commit`` and push ``branch``. Returns the new SHA."""
        if checkout_dir.exists():
            shutil.rmtree(checkout_dir)
        await _check("clone", _git("clone", "-q", "--no-checkout", "--", str(self.mirror_dir), str(checkout_dir)))
        await _check("set-url", _git("remote", "set-url", "origin", self.url, cwd=checkout_dir))
        await _check("checkout", _git("checkout", "-q", "-b", safe_arg(branch), safe_arg(base_commit), cwd=checkout_dir))
        patch_file = checkout_dir.parent / f".{checkout_dir.name}.patch"
        patch_file.write_text(patch, encoding="utf-8", newline="")  # as is: no \r\n on Windows
        try:
            await _check("apply", _git("apply", "--index", "--whitespace=nowarn", str(patch_file), cwd=checkout_dir))
        finally:
            patch_file.unlink(missing_ok=True)
        ident = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}"]
        await _check("commit", _git(*ident, "commit", "-q", "-m", message, cwd=checkout_dir))
        await _check("push", _git("push", "-q", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}",
                                  cwd=checkout_dir))
        r = await _check("rev-parse", _git("rev-parse", "HEAD", cwd=checkout_dir))
        return r.stdout.strip()
