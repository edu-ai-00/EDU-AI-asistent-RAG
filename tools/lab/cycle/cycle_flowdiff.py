#!/usr/bin/env python3
"""Read-only comparison of the canonical flow exports (A and B directories) with the live Langflow flows.

Per flow alias: canonical A == B?, live nodes equal to canonical (type, code, description, metadata, field_order) and
the node ids/keys that differ. Never prints code, values or keys. Run from a checkout (needs its .env).

  cycle_flowdiff.py --from-dir DIR_A --to-dir DIR_B
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from urllib.parse import urlsplit

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cycle_core as C
SCRIPTS = pathlib.Path.cwd() / "scripts"
sys.path.insert(0, str(SCRIPTS))
import flow_rollout as FR  # noqa: E402
from envfile import read_env  # noqa: E402

KEYS = ("type", "template.code", "node.description", "node.metadata", "node.field_order")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-dir", required=True, type=pathlib.Path)
    ap.add_argument("--to-dir", required=True, type=pathlib.Path)
    for key in ("src", "cfg", "state", "project", "expect-host", "backup-root"):
        ap.add_argument("--" + key, required=True)
    ap.add_argument("--running-from")
    a = ap.parse_args()
    C.gate(C.Layout(a.src, a.cfg, a.state, a.project, a.expect_host, a.backup_root), expect_wd=a.running_from)
    env = read_env(pathlib.Path(a.cfg) / ".env")
    url = FR.setting("LANGFLOW_URL", "", env)
    target = urlsplit(url)
    if (target.scheme != "http" or target.hostname != "127.0.0.1" or target.port != 8444
            or target.username or target.password or target.path not in ("", "/") or target.query or target.fragment):
        raise C.Abort("Langflow URL is not this lab's loopback nginx endpoint")
    api = FR.Api(url, FR.setting("LANGFLOW_API_KEY", "", env))
    fa, fb = FR.read_flow_files(a.from_dir), FR.read_flow_files(a.to_dir)
    rows = api.call("GET", "/api/v1/flows/?get_all=true&header_flows=true")
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    by_alias = {r.get("endpoint_name"): r["id"] for r in rows if r.get("endpoint_name")}
    summary = {"canonical_files": len(fb), "canonical_A_equals_B": fa == fb, "live_flows_total": len(rows), "per_flow": {}}
    bad = 0
    for name in sorted(fb):
        flow = fb[name]
        alias = flow.get("endpoint_name")
        fid = by_alias.get(alias)
        if not fid:
            summary["per_flow"][alias] = "NOT_INSTALLED"
            bad += 1
            continue
        live = api.call("GET", "/api/v1/flows/%s" % fid)
        live_nodes = {n["id"]: n for n in live["data"]["nodes"]}
        diff = {}
        for n in flow["data"]["nodes"]:
            ln = live_nodes.get(n["id"])
            if ln is None:
                diff[n["id"]] = ["missing"]
                continue
            keys = [k for k in KEYS if FR.get_key(ln, k) != FR.get_key(n, k)]
            if keys:
                diff[n["id"]] = keys
        same_graph = len(live_nodes) == len(flow["data"]["nodes"]) and live["data"].get("edges") == flow["data"].get("edges")
        summary["per_flow"][alias] = {"nodes": len(flow["data"]["nodes"]), "nodes_equal_to_canonical": len(flow["data"]["nodes"]) - len(diff),
                                      "differing": diff, "same_node_count_and_edges": same_graph,
                                      "live_data_sha256": hashlib.sha256(json.dumps(live["data"], sort_keys=True).encode()).hexdigest()[:16]}
        bad += bool(diff)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("FLOWDIFF_FAILED type=%s" % type(exc).__name__)
        sys.exit(1)
