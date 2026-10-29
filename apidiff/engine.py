"""Diff two API surfaces and turn the result into a Change Record (TDD §8, source 2 "spec or type diff")."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from apidiff.model import ApiChange, ApiSurface, ApiSymbol, ChangeType, SymbolKind
from services.hearth_agent.models import ChangeKind, ChangeRecord, ChangeSource, PackageRef, Severity, SymbolChange

RENAME_THRESHOLD = 0.72
_ENUM_RE = re.compile(r"^([^()|]*)\(([^()]*)\)((?:\[\])*)$")  # "string(a|b)", "string(a|b)[]"


def _enum_delta(before: str, after: str) -> tuple[set[str], set[str]] | None:
    """(removed, added) enum values when two types differ only in their enum lists, else ``None``.

    Types are in ``openapi._type_of`` form; unions are joined by `` | `` and compared piece by piece.
    """
    a_parts, b_parts = before.split(" | "), after.split(" | ")
    if len(a_parts) != len(b_parts):
        return None
    removed: set[str] = set()
    added: set[str] = set()
    for x, y in zip(a_parts, b_parts, strict=True):
        if x == y:
            continue
        mx, my = _ENUM_RE.match(x), _ENUM_RE.match(y)
        if not (mx and my and mx[1] == my[1] and mx[3] == my[3]):
            return None
        vx, vy = set(mx[2].split("|")), set(my[2].split("|"))
        removed |= vx - vy
        added |= vy - vx
    return (removed, added) if removed or added else None


_NAME_RE = re.compile(r"[\w.]+")


@dataclass
class _DiffContext:
    """What type comparison needs beyond the two symbols: detected renames and both sides' named types."""

    old_types: dict[str, ApiSymbol]
    new_types: dict[str, ApiSymbol]
    renames: dict[str, str] = field(default_factory=dict)  # renamed type name -> new name


def _named_types(surface: ApiSurface) -> dict[str, ApiSymbol]:
    return {s.name: s for s in surface.symbols.values() if s.kind in (SymbolKind.TYPE, SymbolKind.CLASS)}


def _apply_renames(type_str: str, renames: dict[str, str]) -> str:
    """Rewrite type names that were detected as renamed, so ``Old[] -> New[]`` is not a second change."""
    for old, new in renames.items():
        type_str = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w.])", new, type_str)
    return type_str


def _superset_of(old_name: str, new_name: str, ctx: _DiffContext) -> bool:
    """``new_name`` keeps every field of ``old_name`` with the same type and adds no required field."""
    a, b = ctx.old_types.get(old_name), ctx.new_types.get(new_name)
    if a is None or b is None or not a.fields:  # field-less types (enums, aliases) can't be compared this way
        return False
    return (all(f in b.fields and _apply_renames(t, ctx.renames) == b.fields[f] for f, t in a.fields.items())
            and not (b.required_fields - a.required_fields))


def _compatible_swap(before: str, after: str, ctx: _DiffContext) -> bool:
    """Types differ only by named types swapped for structural supersets (``Old[] -> NewSuperset[]``)."""
    if _NAME_RE.sub("", before) != _NAME_RE.sub("", after):
        return False
    swaps = [(x, y) for x, y in zip(_NAME_RE.findall(before), _NAME_RE.findall(after), strict=False) if x != y]
    return bool(swaps) and all(_superset_of(x, y, ctx) for x, y in swaps)


def _has_ancestor_in(name: str, names: set[str]) -> bool:
    """``a.b[].c`` has ancestors ``a`` and ``a.b``; a child of an added/removed parent is not its own change."""
    parts = name.split(".")
    return any(".".join(parts[:i]).removesuffix("[]") in names for i in range(1, len(parts)))


def _type_change(
    breaking_type: ChangeType, name: str, before: str, after: str, ctx: _DiffContext
) -> tuple[ChangeType, str] | None:
    """A type change, downgraded to ENUM_VALUES_ADDED when the new type only adds enum values.

    ``None`` when the types only differ by a detected rename (already reported as RENAMED) or by a named
    type swapped for a structural superset of itself, neither of which needs a change in calling code.
    """
    if _apply_renames(before, ctx.renames) == after or _compatible_swap(before, after, ctx):
        return None
    delta = _enum_delta(before, after)
    if delta is None:
        return breaking_type, f"{name}: {before} -> {after}"
    removed, added = delta
    detail = f"{name}: " + ", ".join([*(f"-{v}" for v in sorted(removed)), *(f"+{v}" for v in sorted(added))])
    return (breaking_type if removed else ChangeType.ENUM_VALUES_ADDED), detail


def _param_shape(sym: ApiSymbol) -> tuple:
    return tuple((p.name, p.required, p.type, p.location) for p in sym.params)


def _rename_score(a: ApiSymbol, b: ApiSymbol) -> float:
    """How likely ``b`` is ``a`` under a new name: same kind & parent, similar name, same params/fields."""
    if a.kind != b.kind:
        return 0.0
    parent_a, _, _ = a.id.rpartition(".")
    parent_b, _, _ = b.id.rpartition(".")
    if a.kind != SymbolKind.OPERATION and parent_a != parent_b:
        return 0.0
    name_sim = SequenceMatcher(None, a.name.lower(), b.name.lower()).ratio()
    if a.kind == SymbolKind.TYPE or a.kind == SymbolKind.CLASS:
        fa, fb = set(a.fields), set(b.fields)
        body_sim = len(fa & fb) / len(fa | fb) if fa | fb else 0.5
    else:
        pa, pb = _param_shape(a), _param_shape(b)
        body_sim = 1.0 if pa == pb else SequenceMatcher(None, pa, pb).ratio()
        if a.returns != b.returns:
            body_sim *= 0.8
    return 0.4 * name_sim + 0.6 * body_sim


def _diff_params(old: ApiSymbol, new: ApiSymbol, ctx: _DiffContext) -> list[ApiChange]:
    out: list[ApiChange] = []
    before = {p.name: p for p in old.params}
    after = {p.name: p for p in new.params}
    gone, new_names = before.keys() - after.keys(), after.keys() - before.keys()
    for name, p in before.items():
        if name not in after:
            if not _has_ancestor_in(name, gone):
                out.append(ApiChange(ChangeType.PARAM_REMOVED, old.id, old.kind, old.signature, new.signature, name))
            continue
        q = after[name]
        if q.required and not p.required:
            out.append(ApiChange(ChangeType.PARAM_BECAME_REQUIRED, old.id, old.kind, old.signature, new.signature, name))
        if p.type and q.type and p.type != q.type:
            if tc := _type_change(ChangeType.PARAM_TYPE_CHANGED, name, p.type, q.type, ctx):
                out.append(ApiChange(tc[0], old.id, old.kind, old.signature, new.signature, tc[1]))
    for name, q in after.items():
        if name not in before and not _has_ancestor_in(name, new_names):
            t = ChangeType.PARAM_ADDED_REQUIRED if q.required else ChangeType.PARAM_ADDED_OPTIONAL
            out.append(ApiChange(t, old.id, old.kind, old.signature, new.signature, name))
    if old.returns and new.returns and old.returns != new.returns and old.kind != SymbolKind.TYPE:
        if tc := _type_change(ChangeType.RETURN_TYPE_CHANGED, "returns", old.returns, new.returns, ctx):
            out.append(ApiChange(tc[0], old.id, old.kind, old.signature, new.signature, tc[1]))
    return out


def _diff_fields(old: ApiSymbol, new: ApiSymbol, ctx: _DiffContext) -> list[ApiChange]:
    out: list[ApiChange] = []
    gone, new_names = old.fields.keys() - new.fields.keys(), new.fields.keys() - old.fields.keys()
    for f, t in old.fields.items():
        if f not in new.fields:
            if not _has_ancestor_in(f, gone):
                out.append(ApiChange(ChangeType.FIELD_REMOVED, old.id, old.kind, old.signature, new.signature, f))
        elif new.fields[f] != t:
            if tc := _type_change(ChangeType.FIELD_TYPE_CHANGED, f, t, new.fields[f], ctx):
                out.append(ApiChange(tc[0], old.id, old.kind, old.signature, new.signature, tc[1]))
    for f in sorted(new_names):
        if f in new.required_fields and not _has_ancestor_in(f, new_names):
            out.append(ApiChange(ChangeType.FIELD_ADDED_REQUIRED, old.id, old.kind, old.signature, new.signature, f))
    return out


def diff_surfaces(old: ApiSurface, new: ApiSurface, *, detect_renames: bool = True) -> list[ApiChange]:
    changes: list[ApiChange] = []
    removed = [old.symbols[k] for k in old.symbols.keys() - new.symbols.keys()]
    added = {k: new.symbols[k] for k in new.symbols.keys() - old.symbols.keys()}
    base_conf = 0.85 if old.language == "typescript" else 1.0
    ctx = _DiffContext(_named_types(old), _named_types(new))

    for sym in sorted(removed, key=lambda s: s.id):
        match: tuple[float, ApiSymbol] | None = None
        if detect_renames:
            for cand in added.values():
                s = _rename_score(sym, cand)
                if s >= RENAME_THRESHOLD and (match is None or s > match[0]):
                    match = (s, cand)
        if match:
            score, cand = match
            del added[cand.id]
            if sym.kind in (SymbolKind.TYPE, SymbolKind.CLASS):
                ctx.renames[sym.name] = cand.name
            changes.append(ApiChange(ChangeType.RENAMED, sym.id, sym.kind, sym.signature, cand.signature,
                                     renamed_to=cand.id, confidence=round(score * base_conf, 2)))
        else:
            changes.append(ApiChange(ChangeType.REMOVED, sym.id, sym.kind, sym.signature, None, confidence=base_conf))

    for sym in sorted(added.values(), key=lambda s: s.id):
        changes.append(ApiChange(ChangeType.ADDED, sym.id, sym.kind, None, sym.signature, confidence=base_conf))

    for key in sorted(old.symbols.keys() & new.symbols.keys()):
        a, b = old.symbols[key], new.symbols[key]
        if b.deprecated and not a.deprecated:
            changes.append(ApiChange(ChangeType.DEPRECATED, key, a.kind, a.signature, b.signature, confidence=base_conf))
        sub = (_diff_fields(a, b, ctx) if a.kind in (SymbolKind.TYPE, SymbolKind.CLASS)
               else _diff_params(a, b, ctx))
        if a.kind == SymbolKind.TYPE and a.returns and b.returns and a.returns != b.returns:
            sub.append(ApiChange(ChangeType.FIELD_TYPE_CHANGED, key, a.kind, a.returns, b.returns, "alias"))
        for c in sub:
            c.confidence = base_conf
        changes.extend(sub)
    return changes


def extract_surface(path: str | Path, language: str | None = None) -> ApiSurface:
    """Pick an extractor by ``language`` or by file suffix / directory contents."""
    from apidiff.openapi import extract_openapi
    from apidiff.python_api import extract_python_module, extract_python_package
    from apidiff.typescript_api import extract_typescript, extract_typescript_dir

    p = Path(path)
    lang = language
    if lang is None:
        if p.is_dir():
            lang = "typescript" if any(p.rglob("*.d.ts")) else "python"
        elif p.name.endswith(".d.ts") or p.suffix == ".ts":
            lang = "typescript"
        elif p.suffix in (".py", ".pyi"):
            lang = "python"
        else:
            lang = "openapi"
    if lang == "openapi":
        return extract_openapi(p)
    if lang == "python":
        return extract_python_package(p) if p.is_dir() else extract_python_module(p.read_text("utf-8"), p.stem)
    if lang == "typescript":
        return extract_typescript_dir(p) if p.is_dir() else extract_typescript(p.read_text("utf-8"))
    raise ValueError(f"unknown language {lang!r}")


def to_change_record(
    changes: list[ApiChange], *, provider: str, package: PackageRef, source_url: str | None = None,
    include_additions: bool = False,
) -> ChangeRecord | None:
    """Fold a diff into one Change Record. ``None`` when nothing relevant changed."""
    breaking = [c for c in changes if c.breaking]
    deprecated = [c for c in changes if c.type == ChangeType.DEPRECATED]
    additive = (ChangeType.ADDED, ChangeType.ENUM_VALUES_ADDED)
    added = [c for c in changes if c.type in additive] if include_additions else []
    relevant = breaking or deprecated or added
    if not relevant:
        return None
    if breaking:
        kind, severity = ChangeKind.BREAKING, (Severity.HIGH if len(breaking) > 3 else Severity.MEDIUM)
    elif deprecated:
        kind, severity = ChangeKind.DEPRECATION, Severity.LOW
    else:
        kind, severity = ChangeKind.FEATURE, Severity.LOW
    shown = relevant[:5]
    summary = "; ".join(c.describe() for c in shown) + (f"; and {len(relevant) - 5} more" if len(relevant) > 5 else "")
    symbols = [SymbolChange(before=c.before or c.symbol, after=c.after, note=c.describe()) for c in relevant]
    confidence = min((c.confidence for c in relevant), default=1.0)
    digest = hashlib.sha1(
        f"{provider}|{package.name}|{package.from_version}|{package.to_version}|{summary}".encode()
    ).hexdigest()[:12]
    return ChangeRecord(
        id=f"cr_{digest}",
        provider=provider,
        package=package,
        kind=kind,
        severity=severity,
        summary=summary,
        symbols=symbols,
        source=ChangeSource.SPEC_DIFF,
        source_url=source_url,
        confidence=confidence,
        review_status="auto" if confidence >= 0.8 else "pending",
    )
