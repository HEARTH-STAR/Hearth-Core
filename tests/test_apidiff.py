import unittest

from apidiff import (
    ChangeType,
    diff_surfaces,
    extract_openapi,
    extract_python_module,
    extract_typescript,
    to_change_record,
)
from services.hearth_agent.models import ChangeKind, PackageRef

OLD_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/v1/charges": {"post": {"operationId": "createCharge", "requestBody": {"content": {"application/json": {
            "schema": {"type": "object", "required": ["amount"], "properties": {
                "amount": {"type": "integer"}, "source": {"type": "string"}}}}}},
            "responses": {"200": {"description": "ok"}}}},
        "/v1/customers/{id}": {"get": {"parameters": [{"name": "id", "in": "path", "schema": {"type": "string"}}],
                                       "responses": {"200": {"description": "ok"}}}},
    },
    "components": {"schemas": {"Customer": {"type": "object", "required": ["id"], "properties": {
        "id": {"type": "string"}, "email": {"type": "string"}}}}},
}

NEW_SPEC = {
    "openapi": "3.0.0",
    "paths": {
        "/v1/customers/{customer}": {"get": {
            "deprecated": True,
            "parameters": [{"name": "customer", "in": "path", "schema": {"type": "string"}},
                           {"name": "expand", "in": "query", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}}}},
        "/v1/payment_intents": {"post": {"operationId": "createPaymentIntent",
                                         "responses": {"200": {"description": "ok"}}}},
    },
    "components": {"schemas": {"Customer": {"type": "object", "required": ["id"], "properties": {
        "id": {"type": "integer"}}}}},
}


def _types(changes):
    return {(c.type, c.symbol) for c in changes}


class OpenApiDiffTests(unittest.TestCase):
    def test_diff(self):
        t = _types(diff_surfaces(extract_openapi(OLD_SPEC), extract_openapi(NEW_SPEC)))
        self.assertIn((ChangeType.REMOVED, "POST /v1/charges"), t)
        self.assertIn((ChangeType.ADDED, "POST /v1/payment_intents"), t)
        # a renamed path variable ({id} -> {customer}) is the same operation, not a removal
        self.assertIn((ChangeType.DEPRECATED, "GET /v1/customers/{}"), t)
        self.assertIn((ChangeType.PARAM_ADDED_REQUIRED, "GET /v1/customers/{}"), t)
        self.assertIn((ChangeType.FIELD_REMOVED, "schema:Customer"), t)
        self.assertIn((ChangeType.FIELD_TYPE_CHANGED, "schema:Customer"), t)

    def test_yaml_text(self):
        surface = extract_openapi("openapi: 3.0.0\npaths:\n  /x:\n    get:\n      responses: {}\n")
        self.assertIn("GET /x", surface.symbols)

    def test_change_record_folding(self):
        changes = diff_surfaces(extract_openapi(OLD_SPEC), extract_openapi(NEW_SPEC))
        cr = to_change_record(changes, provider="stripe",
                              package=PackageRef(ecosystem="npm", name="stripe", **{"from": "16", "to": "17"}))
        self.assertIsNotNone(cr)
        self.assertEqual(cr.kind, ChangeKind.BREAKING)
        self.assertTrue(cr.id.startswith("cr_") and cr.symbols)
        self.assertIsNone(to_change_record([], provider="x", package=PackageRef(ecosystem="npm", name="x")))

    def test_enum_widening_is_not_breaking(self):
        def spec(param_enum, field_enum):
            return {"openapi": "3.0.0",
                    "paths": {"/v1/x": {"get": {"parameters": [
                        {"name": "type", "in": "query", "schema": {"type": "string", "enum": param_enum}},
                        {"name": "reason", "in": "query", "schema": {"type": "string"}}]}}},
                    "components": {"schemas": {"Obj": {"type": "object", "properties": {
                        "status": {"type": "array", "items": {"type": "string", "enum": field_enum}}}}}}}

        changes = diff_surfaces(extract_openapi(spec(["card", "sepa"], ["open"])),
                                extract_openapi(spec(["card", "sepa", "upi"], ["open", "paid"])))
        self.assertEqual([(c.type, c.detail, c.breaking) for c in changes], [
            (ChangeType.ENUM_VALUES_ADDED, "type: +upi", False),
            (ChangeType.ENUM_VALUES_ADDED, "status: +paid", False),
        ])
        cr = to_change_record(changes, provider="x", package=PackageRef(ecosystem="npm", name="x"),
                              include_additions=True)
        self.assertEqual(cr.kind, ChangeKind.FEATURE)

        removed = diff_surfaces(extract_openapi(spec(["card", "sepa"], ["open"])),
                                extract_openapi(spec(["card", "upi"], ["open"])))
        self.assertEqual([(c.type, c.detail, c.breaking) for c in removed],
                         [(ChangeType.PARAM_TYPE_CHANGED, "type: -sepa, +upi", True)])

    def test_free_string_narrowed_to_enum_is_breaking(self):
        def spec(schema):
            return {"openapi": "3.0.0", "paths": {"/v1/x": {"post": {"parameters": [
                {"name": "reason", "in": "query", "schema": schema}]}}}}

        changes = diff_surfaces(extract_openapi(spec({"type": "string"})),
                                extract_openapi(spec({"type": "string", "enum": ["fraud", "other"]})))
        self.assertEqual([(c.type, c.breaking) for c in changes], [(ChangeType.PARAM_TYPE_CHANGED, True)])

    def test_nested_request_params(self):
        def spec(card_props, extra=None):
            body = {"type": "object", "properties": {
                "payment_method_options": {"type": "object", "properties": {
                    # Stripe "emptyable" shape: an inline object, or "" to clear it
                    "card": {"anyOf": [{"type": "object", "properties": card_props, "required": ["network"]},
                                       {"type": "string", "enum": [""]}]}}},
                "line_items": {"type": "array", "items": {"type": "object", "properties": {
                    "price": {"type": "string"}, "tax_rates": {"type": "array", "items": {"type": "string"}}}}},
                **(extra or {})}}
            return {"openapi": "3.0.0", "paths": {"/v1/pi": {"post": {"requestBody": {"content": {
                "application/x-www-form-urlencoded": {"schema": body}}}}}}}

        old = spec({"network": {"type": "string"}, "moto": {"type": "boolean"}})
        new = spec({"network": {"type": "string"}, "installments": {"type": "object", "required": ["plan"],
                                                                    "properties": {"plan": {"type": "string"}}}},
                   extra={"line_items": {"type": "array", "items": {"type": "object", "properties": {
                       "price": {"type": "string"}}}}})
        self.assertEqual([(c.type, c.detail) for c in diff_surfaces(extract_openapi(old), extract_openapi(new))], [
            (ChangeType.PARAM_REMOVED, "payment_method_options.card.moto"),
            (ChangeType.PARAM_REMOVED, "line_items[].tax_rates"),
            # only the new parent object is reported, not its (required-within-it) child "plan"
            (ChangeType.PARAM_ADDED_OPTIONAL, "payment_method_options.card.installments"),
        ])

    def test_renamed_or_superset_schema_refs_are_not_type_changes(self):
        def spec(schemas, ref):
            return {"openapi": "3.0.0", "paths": {}, "components": {"schemas": {
                **schemas, "Charge": {"type": "object", "properties": {
                    "details": {"$ref": f"#/components/schemas/{ref}"},
                    "history": {"type": "array", "items": {"$ref": f"#/components/schemas/{ref}"}}}}}}}

        card = {"type": "object", "required": ["brand"], "properties": {"brand": {"type": "string"},
                                                                        "last4": {"type": "string"}}}
        superset = {**card, "properties": {**card["properties"], "iin": {"type": "string"}}}
        narrower = {"type": "object", "properties": {"brand": {"type": "string"}}}

        renamed = diff_surfaces(extract_openapi(spec({"CardDetails": card}, "CardDetails")),
                                extract_openapi(spec({"CardDetail": card}, "CardDetail")))
        self.assertEqual([c.type for c in renamed], [ChangeType.RENAMED])  # no second "field type changed"

        swapped = diff_surfaces(extract_openapi(spec({"Card": card}, "Card")),
                                extract_openapi(spec({"Card": card, "RecordCard": superset}, "RecordCard")))
        self.assertEqual([c.type for c in swapped], [ChangeType.ADDED])

        narrowed = diff_surfaces(extract_openapi(spec({"Card": card}, "Card")),
                                 extract_openapi(spec({"Card": card, "Slim": narrower}, "Slim")))
        self.assertEqual([(c.type, c.detail) for c in narrowed if c.breaking], [
            (ChangeType.FIELD_TYPE_CHANGED, "details: Card -> Slim"),
            (ChangeType.FIELD_TYPE_CHANGED, "history: Card[] -> Slim[]"),
        ])

    def test_rejects_non_openapi_text(self):
        for text in ("", "hello world", "a: [1, 2", "title: not a spec\n", "openapi: 3.0.0\npaths: [1, 2]\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_openapi(text)

    def test_long_single_line_spec_is_text_not_path(self):
        import json
        spec = {"openapi": "3.0.0", "paths": {f"/v1/r{i}": {"get": {}} for i in range(400)}}
        self.assertEqual(len(extract_openapi(json.dumps(spec)).symbols), 400)


class PythonDiffTests(unittest.TestCase):
    def test_diff_and_rename(self):
        old = '''
__all__ = ["Client", "create_charge"]
def create_charge(amount: int, source: str, *, idempotency_key=None) -> dict: ...
class Client:
    timeout: float
    def list_customers(self, limit: int = 10) -> list: ...
    def get(self, id: str) -> dict: ...
'''
        new = '''
__all__ = ["Client"]
class Client:
    timeout: float
    def list_customers(self, limit: int, starting_after: str | None = None) -> list: ...
    def retrieve(self, id: str) -> dict: ...
'''
        changes = diff_surfaces(extract_python_module(old, "sdk"), extract_python_module(new, "sdk"))
        t = _types(changes)
        self.assertIn((ChangeType.REMOVED, "sdk.create_charge"), t)
        self.assertIn((ChangeType.PARAM_BECAME_REQUIRED, "sdk.Client.list_customers"), t)
        self.assertIn((ChangeType.PARAM_ADDED_OPTIONAL, "sdk.Client.list_customers"), t)
        renamed = [c for c in changes if c.type == ChangeType.RENAMED]
        self.assertEqual([(c.symbol, c.renamed_to) for c in renamed], [("sdk.Client.get", "sdk.Client.retrieve")])


class TypeScriptDiffTests(unittest.TestCase):
    def test_diff(self):
        old = '''
export declare class Charges {
  create(params: ChargeCreateParams, options?: RequestOptions): Promise<Charge>;
  list(params?: ListParams): Promise<Charge[]>;
}
export interface ChargeCreateParams { amount: number; currency: string; source?: string; }
export declare function constructEvent(payload: string, header: string, secret: string): Event;
export type Mode = "live" | "test";
'''
        new = '''
export declare class Charges {
  /** @deprecated use paymentIntents */
  list(params?: ListParams): Promise<Charge[]>;
}
export interface ChargeCreateParams { amount: number; currency: string; payment_method: string; }
export declare function constructEvent(payload: string | Buffer, header: string, secret: string): Event;
export type Mode = "live" | "test" | "sandbox";
'''
        changes = diff_surfaces(extract_typescript(old, "Stripe"), extract_typescript(new, "Stripe"))
        t = _types(changes)
        self.assertIn((ChangeType.REMOVED, "Stripe.Charges.create"), t)
        self.assertIn((ChangeType.DEPRECATED, "Stripe.Charges.list"), t)
        self.assertIn((ChangeType.FIELD_REMOVED, "Stripe.ChargeCreateParams"), t)
        self.assertIn((ChangeType.FIELD_ADDED_REQUIRED, "Stripe.ChargeCreateParams"), t)
        self.assertIn((ChangeType.PARAM_TYPE_CHANGED, "Stripe.constructEvent"), t)
        self.assertIn((ChangeType.FIELD_TYPE_CHANGED, "Stripe.Mode"), t)
        self.assertTrue(all(c.confidence < 1 for c in changes))  # heuristic scanner never claims certainty


if __name__ == "__main__":
    unittest.main()
