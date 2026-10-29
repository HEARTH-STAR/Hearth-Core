"""OpenAPI 3.x (and Swagger 2.0, best effort) surface extraction.

Operations become OPERATION symbols keyed by ``METHOD /path`` (path params normalised to ``{}`` so a
renamed path variable is not a removal). Component schemas become TYPE symbols with their properties.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from apidiff.model import ApiSurface, ApiSymbol, Param, SymbolKind

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MAX_DEPTH = 10  # nesting levels of inline objects flattened into dotted param / field names


def parse_spec_text(text: str) -> dict[str, Any]:
    """Parse spec text (JSON or YAML). ``ValueError`` when it is not an OpenAPI / Swagger document.

    Never treats ``text`` as a file path, so it is the one to use on untrusted input (the HTTP route).
    """
    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            spec = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"not valid JSON or YAML: {exc}") from exc
    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec):
        raise ValueError("not an OpenAPI or Swagger document (no top-level 'openapi' / 'swagger' key)")
    return spec


def _is_file(source: str) -> bool:
    try:
        return Path(source).is_file()
    except OSError:  # e.g. ENAMETOOLONG on Linux for a long single-line (minified) spec
        return False


def load_spec(source: str | Path | dict) -> dict[str, Any]:
    """Accept a parsed dict, a file path, or the spec text itself (JSON or YAML)."""
    if isinstance(source, dict):
        return source
    if isinstance(source, Path) or ("\n" not in source and _is_file(source)):
        return parse_spec_text(Path(source).read_text(encoding="utf-8"))
    return parse_spec_text(source)


def _resolve(spec: dict, node: Any, depth: int = 0) -> Any:
    if depth > 20 or not isinstance(node, dict) or "$ref" not in node:
        return node
    ref = node["$ref"]
    if not ref.startswith("#/"):
        return node  # external refs not followed
    target: Any = spec
    for part in ref[2:].split("/"):
        target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
    return _resolve(spec, target, depth + 1)


def _type_of(spec: dict, schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    schema = _resolve(spec, schema)
    for key in ("oneOf", "anyOf", "allOf"):
        if key in schema:
            sep = " & " if key == "allOf" else " | "
            return sep.join(sorted(_type_of(spec, s) for s in schema[key]))
    t = schema.get("type", "object")
    if isinstance(t, list):
        t = "|".join(sorted(str(x) for x in t))
    if t == "array":
        return f"{_type_of(spec, schema.get('items', {}))}[]"
    if "enum" in schema:
        return f"{t}({'|'.join(sorted(map(str, schema['enum'])))})"
    return str(t)


def _inline_objects(schema: Any) -> Iterator[tuple[dict, str]]:
    """Inline (non-``$ref``) object schemas inside a property: itself, array items, any/one/allOf branches.

    Yields ``(object schema, suffix)``; the suffix is ``"[]"`` per array level between property and object.
    ``$ref``'d objects are not descended into: they are component schemas, diffed as their own symbols.
    """
    if not isinstance(schema, dict) or "$ref" in schema:
        return
    if isinstance(schema.get("properties"), dict):
        yield schema, ""
    if isinstance(schema.get("items"), dict):
        for sub, suffix in _inline_objects(schema["items"]):
            yield sub, "[]" + suffix
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(key) or []:
            yield from _inline_objects(branch)


def _properties(spec: dict, schema: dict, prefix: str = "", depth: int = 0) -> dict[str, tuple[bool, str]]:
    """``{dotted name: (required within its own object, type)}`` for a schema's properties, nested ones included.

    ``payment_method_options.card.network`` is a property of an inline object under ``payment_method_options``.
    The first definition of a name wins when several any/oneOf branches declare it.
    """
    out: dict[str, tuple[bool, str]] = {}
    req = set(schema.get("required", []))
    for name, prop in (schema.get("properties") or {}).items():
        full = prefix + name
        out.setdefault(full, (name in req, _type_of(spec, prop)))
        if depth < MAX_DEPTH:
            for sub, suffix in _inline_objects(prop):
                for k, v in _properties(spec, sub, f"{full}{suffix}.", depth + 1).items():
                    out.setdefault(k, v)
    return out


def _norm_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def extract_openapi(source: str | Path | dict) -> ApiSurface:
    spec = load_spec(source)
    surface = ApiSurface("openapi")
    paths = spec.get("paths") or {}
    if not isinstance(paths, dict):
        raise ValueError("'paths' must be a mapping")
    for path, item in paths.items():
        item = _resolve(spec, item)
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params: list[Param] = []
            for p in [*shared, *op.get("parameters", [])]:
                p = _resolve(spec, p)
                if not isinstance(p, dict) or "name" not in p:
                    continue
                schema = p.get("schema", p)
                params.append(Param(p["name"], bool(p.get("required") or p.get("in") == "path"),
                                    _type_of(spec, schema), p.get("in")))
                for sub, suffix in _inline_objects(schema):  # deepObject-style query params, e.g. created[gte]
                    for name, (req, typ) in _properties(spec, sub, f"{p['name']}{suffix}.", 1).items():
                        params.append(Param(name, req, typ, p.get("in")))
            body = _resolve(spec, op.get("requestBody") or {})
            for media in (body.get("content") or {}).values():
                bschema = _resolve(spec, media.get("schema") or {})
                if isinstance(bschema, dict):
                    for name, (req, typ) in _properties(spec, bschema).items():
                        params.append(Param(name, req, typ, "body"))
                break  # first media type is representative
            seen: set[str] = set()
            params = [p for p in params if not (p.name in seen or seen.add(p.name))]
            ok = next((r for code, r in (op.get("responses") or {}).items() if str(code).startswith("2")), None)
            returns = None
            if isinstance(ok, dict):
                ok = _resolve(spec, ok)
                for media in (ok.get("content") or {}).values():
                    returns = _type_of(spec, media.get("schema") or {})
                    break
            key = f"{method.upper()} {_norm_path(path)}"
            surface.add(ApiSymbol(
                id=key,
                kind=SymbolKind.OPERATION,
                name=op.get("operationId") or key,
                params=params,
                returns=returns,
                deprecated=bool(op.get("deprecated")),
                signature=f"{method.upper()} {path}" + (f" ({op['operationId']})" if op.get("operationId") else ""),
            ))
    schemas = (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}
    for name, schema in schemas.items():
        schema = _resolve(spec, schema)
        if not isinstance(schema, dict):
            continue
        props = _properties(spec, schema)
        surface.add(ApiSymbol(
            id=f"schema:{name}",
            kind=SymbolKind.TYPE,
            name=name,
            deprecated=bool(schema.get("deprecated")),
            signature=f"schema {name}",
            fields={k: typ for k, (_, typ) in props.items()},
            required_fields={k for k, (req, _) in props.items() if req},
        ))
    return surface
