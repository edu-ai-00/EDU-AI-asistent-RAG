# last change in code by Jan Drozd (2026-07-27).
"""Static invariants for the Langflow flow JSON — the config the flows carry.

    python3 tests/test_flow_invariants.py            # all
    python3 tests/test_flow_invariants.py hidden     # selected

Standard library only. No Docker, no running stack, no network — so this can run
before a deploy, over a pull request, or right after someone re-exports a flow
from the Langflow UI. That last case is the whole point: the flows carry 197
hand-maintained `load_from_db` attributes and 186 `override_skip` attributes,
and the UI is not guaranteed to preserve them. Losing one fails SILENTLY at
runtime (see `hidden` below), which is why a static check earns its keep.

What each invariant is protecting, and why breaking it is not obvious:

  free_text  A Credential-typed global variable cannot go into a non-secret
             field — Langflow rejects it outright ("Cannot use a Credential-typed
             global variable in 'pg_host'"). Since the env→variable bridge only
             ever creates Credential variables, non-secret connectivity must
             stay plain env and its names must never be secrets. See the two
             rules spelled out at the top of the Postgres block in .env.example.

  secrets    The mirror image: only the two genuinely secret names may sit in
             SecretStrInput fields, because those ARE env-sourced as Credential.

  hidden     `should_skip_field` (lfx/graph/vertex/param_handler.py) skips every
             field without `show` — unless `override_skip` is set. A skipped
             field is never resolved at all, so the component falls back to the
             default in its own code: empty host means a local unix socket,
             empty password means "no password supplied". No error names the
             variable, so this reads as a Postgres problem, not a config one.

  named      A `load_from_db` field whose value is empty is skipped just as
             quietly (`if field not in params or not params[field]: continue`).

Adding a legitimate new variable means updating the two tuples below. That is
deliberate: it forces the "is this a secret?" decision to be written down
somewhere a reviewer sees it.
"""

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLOW_DIR = ROOT / "langflow_flows"

# Non-secret connectivity. Resolved via load_from_db → os.getenv fallback, and
# deliberately absent from LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT so they are
# never turned into (Credential) global variables.
NON_SECRET_VARS = (
    "POSTGRES_DB",
    "POSTGRES_DB_SCHEMA",
    "POSTGRES_DB_SERVER",
    "POSTGRES_DB_USER",
    "POSTGRES_SERVER_PORT",
)

# Env-sourced into Credential global variables; valid ONLY in password fields.
SECRET_VARS = (
    "OPENAI_API_KEY",
    "POSTGRES_PASSWORD",
)

# Field names that must ALWAYS resolve from a variable, never hold a literal.
#
# This list exists because every other check here iterates fields that already
# have load_from_db=True — so a field that LOSES the flag drops out of their view
# entirely instead of failing, and passes by being invisible.
#
# Measured against the live stack: editing component code in the UI does NOT
# cause that. /api/v1/custom_component merges the rebuilt template with the
# existing node (endpoints.py → update_frontend_node_with_template_values), and
# the merge keeps load_from_db and the variable name; it only resets `show` and
# `override_skip`. A bare rebuild does drop everything, but that needs an empty
# frontend_node, which the UI does not send.
#
# So this check guards the case nothing else covers — a hand-edited flow, a
# future Langflow whose merge keeps less, or a component code change that drops
# an input entirely. Cheap, and the only invariant that notices a field which
# stopped being variable-bound.
CONNECTIVITY_FIELDS = (
    "api_key",
    "openai_api_key",
    "pg_database",
    "pg_host",
    "pg_password",
    "pg_port",
    "pg_schema",
    "pg_user",
)


# ── helpers ───────────────────────────────────────────────────

def flow_files() -> list[pathlib.Path]:
    files = sorted(FLOW_DIR.rglob("*.json"))
    if not files:
        raise AssertionError(f"no flow JSON found under {FLOW_DIR}")
    return files


def flow_documents():
    """Yield (path, parsed flow JSON) for every flow."""
    for path in flow_files():
        yield path, json.loads(path.read_text(encoding="utf-8"))


def template_fields():
    """Yield (flow_name, node_id, field_name, field) for every template field."""
    for path, data in flow_documents():
        for node in data.get("data", {}).get("nodes", []):
            template = node.get("data", {}).get("node", {}).get("template", {})
            for field_name, field in template.items():
                if isinstance(field, dict):
                    yield path.name, node.get("id", "<no id>"), field_name, field


def load_from_db_fields():
    """Yield (flow_name, node_id, field_name, field) for every load_from_db field.

    Walks each node's whole subtree rather than just `template`, so a field that
    moves in a future Langflow schema is still seen instead of silently dropping
    out of coverage.
    """
    for path in flow_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        for node in data.get("data", {}).get("nodes", []):
            node_id = node.get("id", "<no id>")

            def walk(obj):
                if isinstance(obj, dict):
                    if obj.get("load_from_db") is True:
                        yield path.name, node_id, obj.get("name", "<unnamed>"), obj
                    for value in obj.values():
                        yield from walk(value)
                elif isinstance(obj, list):
                    for value in obj:
                        yield from walk(value)

            yield from walk(node)


def is_secret_field(field: dict) -> bool:
    """True for password-typed inputs — the runtime signal, not the label.

    Langflow's own checks read `password` (`getattr(input_obj, "password", False)`
    in _wrap_if_secret and in the Credential rejection), so that is what decides
    here. `_input_type` is only the display-side name; test_signals asserts the
    two never disagree.
    """
    return field.get("password") is True


def report(violations: list[str], checked: int, what: str) -> None:
    if violations:
        raise AssertionError(
            f"{len(violations)} of {checked} {what}:\n  " + "\n  ".join(violations)
        )
    print(f"   {checked} {what} ok")


# ── invariants ────────────────────────────────────────────────

def test_free_text_fields_reference_non_secret_variables():
    """Non-secret fields must not name a Credential variable (hard runtime error)."""
    violations, checked = [], 0
    for flow, node, name, field in load_from_db_fields():
        if is_secret_field(field):
            continue
        checked += 1
        value = field.get("value")
        if value not in NON_SECRET_VARS:
            violations.append(
                f"{flow} :: {node} :: {name} → {value!r} is not a known non-secret "
                f"variable. If it is a secret, the field must be a SecretStrInput; "
                f"if it is new and non-secret, add it to NON_SECRET_VARS and to .env."
            )
    report(violations, checked, "free-text load_from_db fields")


def test_secret_fields_reference_credential_variables():
    """Password fields must name one of the two env-sourced Credential variables."""
    violations, checked = [], 0
    for flow, node, name, field in load_from_db_fields():
        if not is_secret_field(field):
            continue
        checked += 1
        value = field.get("value")
        if value not in SECRET_VARS:
            violations.append(
                f"{flow} :: {node} :: {name} → {value!r} is not one of "
                f"{list(SECRET_VARS)}. A secret field resolves from a Credential "
                f"global variable, which only exists for names listed in "
                f"LANGFLOW_VARIABLES_TO_GET_FROM_ENVIRONMENT."
            )
    report(violations, checked, "secret load_from_db fields")


def test_hidden_fields_carry_override_skip():
    """A hidden load_from_db field without override_skip is never resolved."""
    violations, checked = [], 0
    for flow, node, name, field in load_from_db_fields():
        if field.get("show"):
            continue
        checked += 1
        if field.get("override_skip") is not True:
            violations.append(
                f"{flow} :: {node} :: {name} is hidden (show=False) but has no "
                f"override_skip=True, so should_skip_field() drops it and the "
                f"component silently falls back to its own default. Re-exported "
                f"from the Langflow UI?"
            )
    report(violations, checked, "hidden load_from_db fields")


def test_every_field_names_a_variable():
    """An empty value on a load_from_db field is skipped without any error."""
    violations, checked = [], 0
    for flow, node, name, field in load_from_db_fields():
        checked += 1
        if not field.get("value"):
            violations.append(
                f"{flow} :: {node} :: {name} has load_from_db=True but an empty "
                f"value, so no variable name is looked up and the field keeps "
                f"whatever default the component code has."
            )
    report(violations, checked, "load_from_db fields")


def test_signals_agree():
    """`password` and `_input_type` must not disagree about what is a secret."""
    violations, checked = [], 0
    for flow, node, name, field in load_from_db_fields():
        checked += 1
        by_password = field.get("password") is True
        by_type = field.get("_input_type") == "SecretStrInput"
        if by_password != by_type:
            violations.append(
                f"{flow} :: {node} :: {name} — password={field.get('password')!r} "
                f"but _input_type={field.get('_input_type')!r}. The runtime trusts "
                f"`password`; a reader trusts `_input_type`. Hand-edited JSON?"
            )
    report(violations, checked, "load_from_db fields with consistent secret signals")


def test_connectivity_fields_are_variable_bound():
    """A connectivity field that lost load_from_db silently holds a literal."""
    violations, checked = [], 0
    for flow, node, name, field in template_fields():
        if name not in CONNECTIVITY_FIELDS:
            continue
        checked += 1
        if field.get("load_from_db") is not True:
            violations.append(
                f"{flow} :: {node} :: {name} has no load_from_db, so {field.get('value')!r} "
                f"is used as a literal and the field no longer tracks .env. This is what "
                f"a template rebuild from the component code produces — did someone edit "
                f"or refresh this component in the Langflow UI?"
            )
    report(violations, checked, "connectivity fields bound to a variable")


def test_no_edge_targets_a_variable_field():
    """An edge into a load_from_db field overwrites the variable name."""
    violations, checked = [], 0
    for path, data in flow_documents():
        bound: dict[tuple[str, str], bool] = {}
        for node in data.get("data", {}).get("nodes", []):
            template = node.get("data", {}).get("node", {}).get("template", {})
            for field_name, field in template.items():
                if isinstance(field, dict) and field.get("load_from_db") is True:
                    bound[node.get("id"), field_name] = True

        for edge in data.get("data", {}).get("edges", []):
            field_name = (edge.get("data", {}).get("targetHandle") or {}).get("fieldName")
            if not field_name:
                continue
            checked += 1
            if (edge.get("target"), field_name) in bound:
                violations.append(
                    f"{path.name} :: edge {edge.get('source')} → {edge.get('target')}."
                    f"{field_name} targets a load_from_db field. Edge params win "
                    f"(vertex/base.py: params = {{**field_params, **edge_params}}), so the "
                    f"edge value replaces the VARIABLE NAME and resolution looks up "
                    f"whatever the edge produced."
                )
    report(violations, checked, "edges into component fields")


# ── runner (no pytest needed) ─────────────────────────────────
TESTS = {
    "free_text": test_free_text_fields_reference_non_secret_variables,
    "secrets": test_secret_fields_reference_credential_variables,
    "hidden": test_hidden_fields_carry_override_skip,
    "named": test_every_field_names_a_variable,
    "signals": test_signals_agree,
    "bound": test_connectivity_fields_are_variable_bound,
    "edges": test_no_edge_targets_a_variable_field,
}


def main(argv):
    picked = [a.lower() for a in argv] or list(TESTS)
    results = []
    for key in picked:
        fn = TESTS.get(key)
        if not fn:
            print(f"?? unknown test '{key}' (have: {', '.join(TESTS)})")
            continue
        print(f"\n▶ {key}: {fn.__doc__.splitlines()[0]}")
        try:
            fn()
            print(f"✅ {key} PASS")
            results.append((key, True))
        except AssertionError as e:
            print(f"❌ {key} FAIL: {e}")
            results.append((key, False))
        except Exception as e:  # noqa: BLE001
            print(f"💥 {key} ERROR: {type(e).__name__}: {e}")
            results.append((key, False))
    ok = sum(1 for _, p in results if p)
    print(f"\n==== {ok}/{len(results)} passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
