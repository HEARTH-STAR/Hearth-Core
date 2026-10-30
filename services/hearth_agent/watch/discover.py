"""Find what to watch for a repo: the API providers its code depends on, and how to validate it.

A repo is read from the Space's bare mirror (gh's credentials, no checkout). Its dependency manifests
(``package.json``, ``requirements*.txt``, ``pyproject.toml``, ``Pipfile``) are matched against
:data:`CATALOG`, the providers whose public OpenAPI spec HEARTH knows; each match becomes one watch.
The validation commands come from the repo itself: the package manager its lockfile names, then its own
build / test scripts.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field

from services.hearth_agent.connectors.github.repo import RepoMirror
from services.storage.paths import hearth_state_dir

MAX_MANIFESTS = 40
SKIP_PARTS = {"node_modules", "vendor", "dist", "build", ".venv", "venv", "site-packages", "examples", "example"}
MANIFESTS = ("package.json", "pyproject.toml", "Pipfile")


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    spec_url: str
    npm: tuple[str, ...] = ()
    pypi: tuple[str, ...] = ()


# Providers with a public OpenAPI document the diff engine reads (checked 2026-10-30).
CATALOG: tuple[Provider, ...] = (
    Provider("stripe", "Stripe", "https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.json",
             ("stripe",), ("stripe",)),
    Provider("openai", "OpenAI", "https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml",
             ("openai",), ("openai",)),
    Provider("github", "GitHub", "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/"
             "api.github.com/api.github.com.json",
             ("@octokit/rest", "@octokit/core", "octokit", "@octokit/app"), ("pygithub", "githubkit")),
    Provider("twilio", "Twilio", "https://raw.githubusercontent.com/twilio/twilio-oai/main/spec/json/"
             "twilio_api_v2010.json", ("twilio",), ("twilio",)),
    Provider("slack", "Slack", "https://raw.githubusercontent.com/slackapi/slack-api-specs/master/web-api/"
             "slack_web_openapi_v2.json", ("@slack/web-api", "@slack/bolt"), ("slack-sdk", "slack-bolt")),
    Provider("plaid", "Plaid", "https://raw.githubusercontent.com/plaid/plaid-openapi/master/2020-09-14.yml",
             ("plaid",), ("plaid-python",)),
    Provider("resend", "Resend", "https://raw.githubusercontent.com/resend/resend-openapi/main/resend.yaml",
             ("resend",), ("resend",)),
    Provider("square", "Square", "https://raw.githubusercontent.com/square/connect-api-specification/master/api.json",
             ("square",), ("squareup",)),
    Provider("digitalocean", "DigitalOcean", "https://raw.githubusercontent.com/digitalocean/openapi/main/"
             "specification/DigitalOcean-public.v2.yaml", (), ("pydo",)),
    Provider("cloudflare", "Cloudflare", "https://raw.githubusercontent.com/cloudflare/api-schemas/main/openapi.json",
             ("cloudflare",), ("cloudflare",)),
)


class DiscoveryError(RuntimeError):
    pass


@dataclass
class Found:
    provider: Provider
    packages: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


@dataclass
class Discovery:
    repo: str
    branch: str
    commit: str
    found: list[Found]
    validate_commands: list[str]
    manifests: list[str]


def _pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _npm_deps(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return set()
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(data.get(key), dict):
            out |= set(data[key])
    return out


_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _py_deps(path: str, text: str) -> set[str]:
    """Package names from requirements files, pyproject (PEP 621 / Poetry) and Pipfile, without a TOML parser."""
    out: set[str] = set()
    if path.endswith(".txt"):
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-") and (m := _REQ_NAME.match(line)):
                out.add(_pypi(m.group(1)))
        return out
    # Quoted requirement strings (PEP 621 dependencies = ["stripe>=9", ...]) and `name = "..."` keys
    # under Poetry / Pipfile dependency tables.
    for m in re.finditer(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*?)?["']""", text):
        out.add(_pypi(m.group(1)))
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        elif "dependencies" in section or section in ("[packages]", "[dev-packages]"):
            if m := re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", stripped):
                out.add(_pypi(m.group(1)))
    return out


def _wanted(path: str) -> bool:
    parts = path.split("/")
    if any(p in SKIP_PARTS or p.startswith(".") for p in parts[:-1]):
        return False
    name = parts[-1]
    return name in MANIFESTS or (name.startswith("requirements") and name.endswith(".txt"))


def match(manifests: dict[str, str]) -> list[Found]:
    """The catalog providers ``manifests`` (path -> text) depend on."""
    found: dict[str, Found] = {}
    for path, text in manifests.items():
        if path.endswith("package.json"):
            deps, key = _npm_deps(text), "npm"
        else:
            deps, key = _py_deps(path, text), "pypi"
        for provider in CATALOG:
            names = provider.npm if key == "npm" else tuple(_pypi(n) for n in provider.pypi)
            hits = deps & set(names)
            if hits:
                entry = found.setdefault(provider.id, Found(provider))
                entry.packages |= hits
                entry.files.add(path)
    return [found[p.id] for p in CATALOG if p.id in found]


def _js_commands(directory: str, package: dict, files: set[str]) -> list[str]:
    def has(name: str) -> bool:
        return posixpath.join(directory, name) in files if directory else name in files

    if has("pnpm-lock.yaml"):
        pm, install = "pnpm", "pnpm install --frozen-lockfile"
    elif has("yarn.lock"):
        pm, install = "yarn", "yarn install --frozen-lockfile"
    elif has("bun.lockb") or has("bun.lock"):
        pm, install = "bun", "bun install"
    elif has("package-lock.json"):
        pm, install = "npm", "npm ci"
    else:
        pm, install = "npm", "npm install"
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    commands = [install]
    for script in ("build", "typecheck", "lint", "test"):
        body = str(scripts.get(script) or "")
        if body and "no test specified" not in body:
            commands.append(f"{pm} test" if script == "test" else f"{pm} run {script}")
    if len(commands) == 1 and has("tsconfig.json"):
        commands.append("npx tsc --noEmit")
    return commands


def _py_commands(directory: str, files: set[str]) -> list[str]:
    def has(name: str) -> bool:
        return posixpath.join(directory, name) in files if directory else name in files

    if has("requirements.txt"):
        install = "python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt"
    elif has("pyproject.toml"):
        install = "python3 -m venv .venv && .venv/bin/pip install -q -e ."
    else:
        install = "python3 -m venv .venv"
    commands = [install, "python3 -m compileall -q -x '(^|/)(\\.venv|node_modules)/' ."]
    prefix = f"{directory}/" if directory else ""
    if any(f.startswith(f"{prefix}tests/") or f.startswith(f"{prefix}test/") or
           posixpath.basename(f).startswith("test_") for f in files if f.startswith(prefix)):
        commands.append(".venv/bin/pip install -q pytest && .venv/bin/python -m pytest -q")
    return commands


def validation_commands(manifests: dict[str, str], files: set[str], found: list[Found]) -> list[str]:
    """Build / test commands for the project that depends on the first found provider (root first)."""
    paths = sorted({f for entry in found for f in entry.files}, key=lambda p: (p.count("/"), p))
    for path in paths:
        directory = posixpath.dirname(path)
        if path.endswith("package.json"):
            try:
                package = json.loads(manifests[path])
            except ValueError:
                package = {}
            commands = _js_commands(directory, package if isinstance(package, dict) else {}, files)
        else:
            commands = _py_commands(directory, files)
        return [f"cd {directory} && {c}" for c in commands] if directory else commands
    return []


async def discover(repo: str, branch: str | None = None) -> Discovery:
    mirror = RepoMirror(hearth_state_dir(), repo)
    await mirror.sync()
    branch = branch or await mirror.default_branch()
    commit = await mirror.resolve(branch)
    files = await mirror.list_files(commit)
    manifests: dict[str, str] = {}
    for path in [f for f in files if _wanted(f)][:MAX_MANIFESTS]:
        text = await mirror.read_file(commit, path)
        if text is not None:
            manifests[path] = text
    found = match(manifests)
    if not found:
        known = ", ".join(p.name for p in CATALOG)
        looked = ", ".join(sorted(manifests)) or "no package.json, requirements*.txt, pyproject.toml or Pipfile"
        raise DiscoveryError(f"{repo}@{branch} uses none of the APIs HEARTH knows ({known}). Looked at: {looked}.")
    return Discovery(repo, branch, commit, found, validation_commands(manifests, set(files), found),
                     sorted(manifests))
