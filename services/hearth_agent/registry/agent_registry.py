"""
Agent manifest registry.

Loads JSON manifests from ``config/agents/<agent>/manifest.json`` — one
subdirectory per supported agent tool. Each manifest declares the binary name,
filesystem layout, model defaults, SDK flags, secrets and provider recipes.
Call sites go through ``get_active_agent()`` so the binary, paths and options are
never hardcoded.

Which manifest is active is resolved in this order:

1. ``AGENT_NAME`` env var — runtime override (takes precedence when set).
2. ``DEFAULT_AGENT`` env var — baseline default (what ships in ``.env.example``).
3. If neither is set and only one manifest exists, that one is used.
4. If neither is set, the one manifest that says ``"default": true`` is used.
5. Otherwise loading raises — we do not guess.

Shape mirrors xo-space ``registry/agent_registry.py``. HEARTH ships two agents
(``claude_code``, ``cursor``); claude_code's manifest carries ``"default": true``,
so a space whose environment names no agent runs Claude Code.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]  # registry/ → hearth_agent/ → services/ → repo root
_MANIFEST_DIR = _REPO_ROOT / "config" / "agents"


def _expand(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expanduser(os.path.expandvars(value)))


@dataclass(frozen=True)
class AgentManifest:
    """In-memory view of one agent manifest file.

    Paths are pre-expanded (``~`` → home). ``raw`` keeps the original JSON so call
    sites can reach into flags / provider recipes without the loader knowing their shape.
    """

    name: str
    binary: str
    home_dir: Path
    env_file: Path
    config_file: Path
    agents_dir: Path
    workspace_dir: Path | None
    cwd: Path
    cli_timeout_seconds: int
    model_default: str
    model_prefix: str
    model_capabilities: dict
    flags: dict
    providers: dict[str, dict]
    raw: dict

    def flag(self, key: str, default: Any = None) -> Any:
        return self.flags.get(key, default)


def _build_manifest(path: Path) -> AgentManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))

    def req(key: str) -> Any:
        if key not in raw:
            raise ValueError(f"manifest {path} is missing required field '{key}'")
        return raw[key]

    model = raw.get("model") or {}
    return AgentManifest(
        name=req("name"),
        binary=req("binary"),
        home_dir=_expand(req("home_dir")),
        env_file=_expand(req("env_file")),
        config_file=_expand(req("config_file")),
        agents_dir=_expand(req("agents_dir")),
        workspace_dir=_expand(raw.get("workspace_dir")),
        cwd=_expand(raw.get("cwd", "~")),
        cli_timeout_seconds=int(raw.get("cli_timeout_seconds", 300)),
        model_default=os.getenv(model.get("env", "")) or model.get("default", ""),
        model_prefix=raw.get("model_prefix", raw["name"]),
        model_capabilities=dict(raw.get("model_capabilities") or {}),
        flags=dict(raw.get("flags") or {}),
        providers=dict(raw.get("providers") or {}),
        raw=raw,
    )


def _discover_manifests() -> dict[str, AgentManifest]:
    if not _MANIFEST_DIR.exists():
        raise FileNotFoundError(
            f"agent manifest directory not found: {_MANIFEST_DIR}. "
            "Add a `config/agents/<name>/manifest.json` describing the agent tool."
        )
    manifests: dict[str, AgentManifest] = {}
    for subdir in sorted(p for p in _MANIFEST_DIR.iterdir() if p.is_dir()):
        manifest_path = subdir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            raw_check = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if "binary" not in raw_check:  # adapter config, not an agent manifest
            continue
        manifest = _build_manifest(manifest_path)
        if manifest.name in manifests:
            raise ValueError(f"duplicate manifest name '{manifest.name}' in {_MANIFEST_DIR}")
        manifests[manifest.name] = manifest
    if not manifests:
        raise FileNotFoundError(f"no agent manifests found in {_MANIFEST_DIR}")
    return manifests


_MANIFESTS: dict[str, AgentManifest] | None = None
_DEFAULT: AgentManifest | None = None


def _ensure_loaded() -> None:
    global _MANIFESTS, _DEFAULT
    if _MANIFESTS is not None:
        return
    _MANIFESTS = _discover_manifests()
    agent_name = (os.getenv("AGENT_NAME") or "").strip()
    default_agent = (os.getenv("DEFAULT_AGENT") or "").strip()
    requested = agent_name or default_agent
    if requested:
        if requested not in _MANIFESTS:
            src = "AGENT_NAME" if agent_name else "DEFAULT_AGENT"
            raise ValueError(f"{src}='{requested}' does not match any manifest. Available: {', '.join(sorted(_MANIFESTS))}")
        _DEFAULT = _MANIFESTS[requested]
    elif len(_MANIFESTS) == 1:
        _DEFAULT = next(iter(_MANIFESTS.values()))
    elif len(marked := [m for m in _MANIFESTS.values() if m.raw.get("default") is True]) == 1:
        _DEFAULT = marked[0]
    else:
        raise ValueError(
            f"multiple agent manifests found ({', '.join(sorted(_MANIFESTS))}) "
            "but neither AGENT_NAME nor DEFAULT_AGENT is set, and not exactly one says \"default\": true."
        )


def reset_cache() -> None:
    """Forget loaded manifests (tests, or after editing config/agents at runtime)."""
    global _MANIFESTS, _DEFAULT
    _MANIFESTS = None
    _DEFAULT = None


def get_active_agent() -> AgentManifest:
    """Return the manifest for the active agent (AGENT_NAME → DEFAULT_AGENT → sole manifest)."""
    _ensure_loaded()
    assert _DEFAULT is not None
    return _DEFAULT


def get_agent(name: str) -> AgentManifest:
    """Return a specific manifest by name (raises if unknown)."""
    _ensure_loaded()
    assert _MANIFESTS is not None
    if name not in _MANIFESTS:
        raise KeyError(f"unknown agent '{name}'. Available: {', '.join(sorted(_MANIFESTS))}")
    return _MANIFESTS[name]


def all_agents() -> list[AgentManifest]:
    _ensure_loaded()
    assert _MANIFESTS is not None
    return list(_MANIFESTS.values())
