"""Find the call sites a spec change touches in a checked-out repo (the Impact Report).

Heuristic and text-based, like the change itself: an operation ``POST /v1/payment_intents/{}`` matches
string literals of that path (path variables match any segment, a leading ``/v<n>`` is optional because
clients often keep it in the base URL). Params and fields that changed are then looked up by name in the
files that call an affected operation; distinctive names (``payment_method_types``) are also looked up
everywhere, at lower confidence. When the watch knows the SDK packages the repo uses (``packages``), files that
import one are also searched for the operation's resource as an SDK member (``stripe.paymentIntents.create``,
``client.payment_intents.create``), and distinctive names are looked up only in those files.
No match means no PR: there is nothing in the repo to fix.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from services.hearth_agent.models import ImpactMatch

SOURCE_SUFFIXES = {
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte", ".py", ".rb", ".go",
    ".java", ".kt", ".kts", ".scala", ".cs", ".php", ".rs", ".swift", ".ex", ".exs", ".dart", ".graphql",
}
SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "vendor", ".venv", "venv", "__pycache__", ".next", ".nuxt",
    "coverage", "target", ".hearth", ".turbo", ".cache",
}
MAX_FILE_BYTES = 1024 * 1024
MAX_MATCHES = 200
_SEGMENT = r"[^/\s'\"`?#]+"


def _path_patterns(symbol: str) -> list[re.Pattern[str]]:
    """``POST /v1/customers/{}`` -> regexes for ``/v1/customers/<anything>`` and ``/customers/<anything>``."""
    _, _, path = symbol.partition(" ")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return []
    variants = [parts]
    if re.fullmatch(r"v\d+", parts[0]) and len(parts) > 1:
        variants.append(parts[1:])
    out = []
    for segs in variants:
        body = "/".join(_SEGMENT if s == "{}" else re.escape(s) for s in segs)
        out.append(re.compile(rf"(?<![\w/-])/{body}(?![\w/-])"))
    return out


def _leaf_names(change: dict[str, Any]) -> set[str]:
    """Param / field names a change is about: ``payment_method_options.card.moto`` -> ``moto``."""
    if not change.get("type", "").startswith(("param_", "field_")):
        return set()
    detail = str(change.get("detail") or "")
    name = detail.split(":", 1)[0].strip()
    leaf = name.rsplit(".", 1)[-1].removesuffix("[]")
    return {leaf} if re.fullmatch(r"[A-Za-z_][\w-]{2,}", leaf) else set()


def _camel(snake: str) -> str:
    head, *rest = re.split(r"[_-]", snake)
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def _resource_patterns(symbol: str) -> list[re.Pattern[str]]:
    """``POST /v1/payment_intents/{}/confirm`` -> SDK member access ``.paymentIntents`` / ``.payment_intents``
    (and the same for its last literal segment, ``.confirm``)."""
    _, _, path = symbol.partition(" ")
    literal = [p for p in path.split("/") if p and p != "{}" and not re.fullmatch(r"v\d+", p)
               and re.fullmatch(r"[A-Za-z][\w-]*", p)]
    names: set[str] = set()
    for seg in literal[-2:]:
        camel = _camel(seg)
        pascal = camel[:1].upper() + camel[1:]
        # payment_intents -> .payment_intents, .paymentIntents, and stripe-python's .PaymentIntent class
        names |= {seg.replace("-", "_"), camel, pascal, pascal.removesuffix("s")}
    return [re.compile(rf"\.{re.escape(n)}\b") for n in sorted(names) if len(n) > 2]


# The module a PyPI distribution is imported as, where the two differ.
_PY_MODULES = {"pygithub": "github", "slack-sdk": "slack_sdk", "slack-bolt": "slack_bolt", "plaid-python": "plaid",
               "squareup": "square"}


def _sdk_import(packages: Sequence[str]) -> re.Pattern[str] | None:
    """A file that imports one of ``packages``: ``require("stripe")``, ``from "@octokit/rest"``, ``import stripe``."""
    if not packages:
        return None
    npm = "|".join(re.escape(p) for p in packages)
    py = "|".join(re.escape(_PY_MODULES.get(p, p.replace("-", "_"))) for p in packages)
    return re.compile(rf"""["'](?:{npm})(?:/[^"']*)?["']|^\s*(?:from|import)\s+(?:{py})\b""", re.M)


def _distinctive(name: str) -> bool:
    return len(name) >= 8 and ("_" in name or any(c.isupper() for c in name[1:]))


def _source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in SOURCE_SUFFIXES:
                try:
                    if path.stat().st_size <= MAX_FILE_BYTES:
                        yield path
                except OSError:
                    continue


def scan(root: Path, changes: list[dict[str, Any]], packages: Sequence[str] = ()) -> list[ImpactMatch]:
    """Matches for the breaking ``changes`` (``Detection.changes`` entries) under ``root``; ``packages`` are the
    provider's SDKs the repo depends on."""
    breaking = [c for c in changes if c.get("breaking")]
    ops: list[tuple[str, list[re.Pattern[str]]]] = []
    names_by_op: dict[str, set[str]] = {}
    for c in breaking:
        if c.get("kind") == "operation":
            sym = c["symbol"]
            if sym not in names_by_op:
                ops.append((sym, _path_patterns(sym)))
                names_by_op[sym] = set()
            names_by_op[sym] |= _leaf_names(c)
    global_names = {n for c in breaking for n in _leaf_names(c) if _distinctive(n)}
    name_re = {n: re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])") for n in global_names | set().union(*names_by_op.values())}
    resources = [(sym, _resource_patterns(sym)) for sym, _ in ops]
    sdk_import = _sdk_import(packages)

    matches: list[ImpactMatch] = []
    seen: set[tuple[str, int, str]] = set()

    def add(rel: str, lineno: int, symbol: str, line: str, confidence: float) -> None:
        key = (rel, lineno, symbol)
        if key not in seen and len(matches) < MAX_MATCHES:
            seen.add(key)
            matches.append(ImpactMatch(path=rel, line=lineno, symbol=symbol, snippet=line.strip()[:300],
                                       confidence=confidence))

    for path in _source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        rel = path.relative_to(root).as_posix()
        uses_sdk = bool(sdk_import and sdk_import.search(text))
        calling: set[str] = set()
        for i, line in enumerate(lines, 1):
            for sym, patterns in ops:
                if any(p.search(line) for p in patterns):
                    calling.add(sym)
                    add(rel, i, sym, line, 0.9)
            if uses_sdk:
                for sym, patterns in resources:
                    if any(p.search(line) for p in patterns):
                        calling.add(sym)
                        add(rel, i, sym, line, 0.6)
        names_here = set().union(*(names_by_op[s] for s in calling)) if calling else set()
        # With known SDKs, distinctive names count only in the files that import one.
        searched = global_names if (uses_sdk or sdk_import is None) else set()
        for i, line in enumerate(lines, 1):
            for name in names_here | searched:
                if name_re[name].search(line):
                    add(rel, i, name, line, 0.8 if name in names_here else 0.5)
        if len(matches) >= MAX_MATCHES:
            break
    return sorted(matches, key=lambda m: (m.path, m.line))
