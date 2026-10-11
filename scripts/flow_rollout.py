#!/usr/bin/env python3
"""Targeted flow rollout A -> B for an INSTALLED Langflow: patch only what changed, refuse what drifted.

Bootstrap preserves existing flows and `import_flows.py` (without --missing-only) overwrites all of them; neither is a
safe way to ship a small flow change. This compares the canonical exports of release A and release B node by node and
patches ONLY the nodes and keys that differ (node type, template field values, new template fields, node description,
metadata and field_order), and ONLY when every such key of the live node is still exactly A (or already B). Any other live value
(a user edit of that code, type or setting) refuses that flow and nothing in it is written. Other nodes, edges, flows
and drafts (flows without an endpoint alias) are never written.

    flow_rollout.py --from-dir DIR_A --to-dir DIR_B (--check | --apply)

Credentials and URL come from .env like import_flows.py (LANGFLOW_URL, LANGFLOW_API_KEY; the environment wins). The
output has node ids, key names and verdicts only: never code and never the key.
--apply also needs --confirm-maintenance-window: the PATCH replaces the whole graph and Langflow offers no version
check, so the flow is read again right before the write and refused if it changed, but only a window without
concurrent editors closes the remaining gap.
Exit status: 0 done (or nothing to do) | 1 a flow was refused or the result did not verify | 3 Langflow unreachable.
Standard library only.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from envfile import is_placeholder, read_env  # noqa: E402
from http_safe import open_url  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
ABSENT = object()


def setting(name: str, default: str, env: dict) -> str:
    for candidate in (os.getenv(name), env.get(name)):
        if candidate and not is_placeholder(candidate):
            return candidate
    return default


class Api:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-api-key": self.key})
        with open_url(request, timeout=120) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw)


def code_of(node: dict) -> str:
    field = node["data"]["node"]["template"].get("code")
    return field.get("value", "") if isinstance(field, dict) else ""


def leaf(value):
    """A template field is compared by its value; the surrounding field metadata is Langflow's to normalise."""
    return value.get("value") if isinstance(value, dict) and "value" in value else value


def get_key(node: dict, key: str):
    data = node["data"]
    if key == "type":
        return data.get("type", ABSENT)
    if key.startswith("template."):
        field = data["node"]["template"].get(key[9:], ABSENT)
        return field if field is ABSENT else leaf(field)
    return data["node"].get(key[5:], ABSENT)


def plan_for(a_flow: dict, b_flow: dict) -> list[tuple[str, list[str]]]:
    """[(node id, [keys])] derived from the two canonical files; refuses anything it does not know how to patch."""
    an = {n["id"]: n for n in a_flow["data"]["nodes"]}
    bn = {n["id"]: n for n in b_flow["data"]["nodes"]}
    if set(an) != set(bn) or a_flow["data"].get("edges") != b_flow["data"].get("edges"):
        raise SystemExit("REFUSED: canonical A and B differ in the node set or the edges; this is not a node-level rollout")
    items = []
    for node_id in sorted(an):
        if an[node_id] == bn[node_id]:
            continue
        x, y = an[node_id]["data"], bn[node_id]["data"]
        keys = []
        if x.get("type") != y.get("type"):
            keys.append("type")
        tx, ty = x["node"]["template"], y["node"]["template"]
        for k in sorted(set(tx) | set(ty)):
            if tx.get(k) == ty.get(k):
                continue
            a_field, b_field = tx.get(k, ABSENT), ty.get(k, ABSENT)
            both = isinstance(a_field, dict) and isinstance(b_field, dict)
            if both and {kk: vv for kk, vv in a_field.items() if kk != "value"} != {kk: vv for kk, vv in b_field.items() if kk != "value"}:
                raise SystemExit(f"REFUSED: node {node_id} field {k} changes more than its value")
            keys.append("template." + k)
        for k in ("description", "metadata", "field_order"):
            if x["node"].get(k) != y["node"].get(k):
                keys.append("node." + k)
        unexplained = [k for k in set(x) | set(y) if k not in ("type", "node") and x.get(k) != y.get(k)]
        other = [k for k in set(x["node"]) | set(y["node"]) if k not in ("template", "description", "metadata", "field_order") and x["node"].get(k) != y["node"].get(k)]
        if unexplained or other or not keys:
            raise SystemExit(f"REFUSED: node {node_id} differs in parts this tool does not patch: {sorted(unexplained + other)}")
        items.append((node_id, keys))
    return items


def apply_key(live_node: dict, key: str, a_node: dict, b_node: dict) -> None:
    data = live_node["data"]
    if key == "type":
        data["type"] = b_node["data"]["type"]
    elif key.startswith("template."):
        name = key[9:]
        b_field = b_node["data"]["node"]["template"].get(name, ABSENT)
        if b_field is ABSENT:
            data["node"]["template"].pop(name, None)
        elif name in data["node"]["template"] and isinstance(data["node"]["template"][name], dict) and isinstance(b_field, dict):
            data["node"]["template"][name]["value"] = copy.deepcopy(b_field.get("value"))
        else:
            data["node"]["template"][name] = copy.deepcopy(b_field)
    else:
        name = key[5:]
        b_value = b_node["data"]["node"].get(name, ABSENT)
        if b_value is ABSENT:
            data["node"].pop(name, None)
        else:
            data["node"][name] = copy.deepcopy(b_value)


def node_state(live: dict, a_node: dict, b_node: dict, keys: list[str]) -> tuple[str, list[str]]:
    """GUARD_FAIL if any key is neither A nor B; ALREADY_B if all are B; else APPLY with the keys still at A."""
    todo, other = [], []
    # Identity is guarded even when this rollout only changes a setting.
    # User code/type is outside both canonical releases and must not be updated.
    for key in ("type", "template.code"):
        value = get_key(live, key)
        if value != get_key(a_node, key) and value != get_key(b_node, key):
            other.append(key)
    for key in keys:
        value, a_value, b_value = get_key(live, key), get_key(a_node, key), get_key(b_node, key)
        if value == b_value:
            continue
        if value == a_value:
            todo.append(key)
        else:
            other.append(key)
    if other:
        return "GUARD_FAIL", other
    return ("APPLY", todo) if todo else ("ALREADY_B", [])


def read_flow_files(directory: pathlib.Path) -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-dir", required=True, type=pathlib.Path)
    parser.add_argument("--to-dir", required=True, type=pathlib.Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-maintenance-window", action="store_true",
                        help="required with --apply: nobody edits these flows in the UI or through the API while this runs")
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_maintenance_window:
        # Langflow's whole-graph PATCH has no version check, so a concurrent editor's save would be overwritten.
        print("REFUSED: --apply needs --confirm-maintenance-window (no concurrent editors); nothing was read or written")
        return 1
    env = read_env(REPO / ".env")
    api = Api(setting("LANGFLOW_URL", "http://127.0.0.1:7860", env), setting("LANGFLOW_API_KEY", "", env))
    if not api.key:
        print("UNREACHABLE: no LANGFLOW_API_KEY (.env or environment)")
        return 3
    a_files, b_files = read_flow_files(args.from_dir), read_flow_files(args.to_dir)
    try:
        rows = api.call("GET", "/api/v1/flows/?get_all=true&header_flows=true")
        rows = rows if isinstance(rows, list) else rows.get("items", [])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("UNREACHABLE:", type(exc).__name__)
        return 3
    by_alias, seen_ids = {}, set()
    if not isinstance(rows, list):
        print("REFUSED: invalid installed flow inventory")
        return 1
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen_ids:
            print("REFUSED: invalid or duplicate installed flow identity")
            return 1
        seen_ids.add(row["id"])
        alias = row.get("endpoint_name")
        if alias is None or alias == "":
            continue  # drafts are preserved
        if not isinstance(alias, str) or alias in by_alias:
            print("REFUSED: invalid or duplicate installed flow alias; no flows written")
            return 1
        by_alias[alias] = row["id"]
    worst = 0
    for name in sorted(b_files):
        b_flow, a_flow = b_files[name], a_files.get(name)
        alias = b_flow.get("endpoint_name")
        if a_flow is None:
            print(f"{alias}: REFUSED no release-A export of this flow to compare with")
            worst = 1
            continue
        items = plan_for(a_flow, b_flow)
        if not items:
            print(f"{alias}: canonical A == B, nothing to do")
            continue
        flow_id = by_alias.get(alias)
        if not flow_id:
            print(f"{alias}: REFUSED not installed (no flow with this endpoint alias)")
            worst = 1
            continue
        live = api.call("GET", f"/api/v1/flows/{flow_id}")
        live_nodes = {n["id"]: n for n in live["data"]["nodes"]}
        a_nodes = {n["id"]: n for n in a_flow["data"]["nodes"]}
        b_nodes = {n["id"]: n for n in b_flow["data"]["nodes"]}
        todo, refused = [], False
        for node_id, keys in items:
            if node_id not in live_nodes:
                print(f"{alias}: node {node_id}: GUARD_FAIL node missing")
                refused = True
                continue
            state, which = node_state(live_nodes[node_id], a_nodes[node_id], b_nodes[node_id], keys)
            print(f"{alias}: node {node_id}: {state} {','.join(which)}".rstrip())
            refused |= state == "GUARD_FAIL"
            if state == "APPLY":
                todo.append((node_id, which))
        if refused:
            print(f"{alias}: REFUSED, flow left untouched")
            worst = 1
            continue
        if not todo:
            print(f"{alias}: nothing to patch")
            continue
        if args.check:
            print(f"{alias}: WOULD_PATCH {len(todo)} node(s)")
            continue
        again = api.call("GET", f"/api/v1/flows/{flow_id}")
        if again.get("data") != live.get("data") or again.get("updated_at") != live.get("updated_at"):
            print(f"{alias}: REFUSED, the flow changed while the rollout was planned; nothing written")
            worst = 1
            continue
        before = copy.deepcopy(live["data"])
        new = copy.deepcopy(live["data"])
        for node in new["nodes"]:
            for node_id, keys in todo:
                if node["id"] == node_id:
                    for key in keys:
                        apply_key(node, key, a_nodes[node_id], b_nodes[node_id])
        api.call("PATCH", f"/api/v1/flows/{flow_id}", {"data": new})
        after = api.call("GET", f"/api/v1/flows/{flow_id}")["data"]
        touched = {i for i, _ in todo}
        pick = lambda d: sorted((n for n in d["nodes"] if n["id"] not in touched), key=lambda n: n["id"])  # noqa: E731
        after_nodes = {n["id"]: n for n in after["nodes"]}
        patched_ok = all(node_state(after_nodes[i], a_nodes[i], b_nodes[i], keys)[0] == "ALREADY_B" for i, keys in todo)
        unchanged = pick(before) == pick(after) and before.get("edges") == after.get("edges")
        print(f"{alias}: PATCHED {len(todo)} node(s); patched_keys_are_B={patched_ok} other_nodes_and_edges_unchanged={unchanged}")
        if not (patched_ok and unchanged):
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
