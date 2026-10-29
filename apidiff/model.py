"""Language-neutral API surface and change model.

Every extractor (OpenAPI, Python, TypeScript) produces an :class:`ApiSurface`: a flat map from a stable
symbol id to an :class:`ApiSymbol`. Diffing two surfaces is then language-independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SymbolKind(StrEnum):
    OPERATION = "operation"  # OpenAPI method+path
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    TYPE = "type"  # interface / type alias / schema
    PROPERTY = "property"
    CONSTANT = "constant"


@dataclass(frozen=True)
class Param:
    name: str
    required: bool = True
    type: str | None = None
    location: str | None = None  # OpenAPI "in": query/path/header/body


@dataclass
class ApiSymbol:
    id: str  # e.g. "POST /v1/charges", "stripe.Charges.create", "Stripe.charges.create"
    kind: SymbolKind
    name: str
    params: list[Param] = field(default_factory=list)
    returns: str | None = None
    deprecated: bool = False
    signature: str = ""  # human-readable, used in before/after text
    fields: dict[str, str] = field(default_factory=dict)  # for TYPE: field -> type ("?" suffix = optional)
    required_fields: set[str] = field(default_factory=set)


@dataclass
class ApiSurface:
    language: str  # "openapi" | "python" | "typescript"
    symbols: dict[str, ApiSymbol] = field(default_factory=dict)

    def add(self, sym: ApiSymbol) -> None:
        self.symbols[sym.id] = sym


class ChangeType(StrEnum):
    REMOVED = "removed"
    RENAMED = "renamed"
    ADDED = "added"
    DEPRECATED = "deprecated"
    PARAM_REMOVED = "param_removed"
    PARAM_ADDED_REQUIRED = "param_added_required"
    PARAM_ADDED_OPTIONAL = "param_added_optional"
    PARAM_BECAME_REQUIRED = "param_became_required"
    PARAM_TYPE_CHANGED = "param_type_changed"
    RETURN_TYPE_CHANGED = "return_type_changed"
    FIELD_REMOVED = "field_removed"
    FIELD_ADDED_REQUIRED = "field_added_required"
    FIELD_TYPE_CHANGED = "field_type_changed"
    ENUM_VALUES_ADDED = "enum_values_added"  # widening an enum is additive, not breaking


BREAKING_TYPES = {
    ChangeType.REMOVED,
    ChangeType.RENAMED,
    ChangeType.PARAM_REMOVED,
    ChangeType.PARAM_ADDED_REQUIRED,
    ChangeType.PARAM_BECAME_REQUIRED,
    ChangeType.PARAM_TYPE_CHANGED,
    ChangeType.RETURN_TYPE_CHANGED,
    ChangeType.FIELD_REMOVED,
    ChangeType.FIELD_ADDED_REQUIRED,
    ChangeType.FIELD_TYPE_CHANGED,
}


@dataclass
class ApiChange:
    type: ChangeType
    symbol: str
    kind: SymbolKind
    before: str | None = None
    after: str | None = None
    detail: str = ""
    renamed_to: str | None = None
    confidence: float = 1.0

    @property
    def breaking(self) -> bool:
        return self.type in BREAKING_TYPES

    def describe(self) -> str:
        if self.type == ChangeType.RENAMED:
            return f"{self.symbol} renamed to {self.renamed_to}"
        return f"{self.symbol}: {self.type.value.replace('_', ' ')}" + (f" ({self.detail})" if self.detail else "")
