#!/usr/bin/env python3
"""Non-paid functional checks of the running stack for the lab cycle. Prints booleans, counts and aliases only:
no row content, no secret values, no LLM calls. Entities are shown as E1..En (sorted), never by name.

  cycle_checks.py --src SRC --cfg CFG --state ST --project P --expect-host H --backup-root R --out FILE.json
                  [--expect-images FILE.json] [--smoke] [--export]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycle_core as C  # noqa: E402
from cycle_core import Layout, pg_psql, run_text  # noqa: E402

R: dict = {}


def rec(name, ok, detail=""):
    R[name] = {"result": "PASS" if ok else "FAIL", "detail": detail}
    print("%-34s %s %s" % (name, "PASS" if ok else "FAIL", detail), flush=True)


def dx(ct, args, inp=None):
    return run_text(["docker", "exec", "-i", ct] + args, input=inp, check=False)


def probe_json(result):
    """Captured subprocess output is input data, never evidence text."""
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
        return value if isinstance(value, dict) and result.returncode == 0 else {}
    except (ValueError, IndexError):
        return {}


def decrypt_summary(result):
    value = probe_json(result)
    return {**{k: C.count_value(value.get(k)) for k in ("decrypted", "failed", "plain")},
            "app_db_connect": value.get("app_db_connect") is True}


def export_summary(result):
    m = re.search(r"^EXPORT_OK packs=(\d+)$", result.stdout, re.M)
    return {"rc": result.returncode, "packs": int(m.group(1)) if m else None}


PROBE = r'''
import gzip, json, os, urllib.request
base = "http://127.0.0.1:7860"; out = {}
def req(path, data=None, headers=None):
    raw = urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers or {}), timeout=180).read()
    if raw[:2] == bytes([31, 139]): raw = gzip.decompress(raw)
    return json.loads(raw.decode())
try:
    h = {"x-api-key": os.environ["LANGFLOW_API_KEY"]}
    fl = req("/api/v1/flows/?get_all=true&header_flows=true", None, h)
    out["flows"] = len(fl if isinstance(fl, list) else fl.get("items", []))
    out["variables"] = len(req("/api/v1/variables/", None, h))
    out["api_key_ok"] = True
except Exception as e:
    out["api_key_error"] = type(e).__name__
print(json.dumps(out))
'''

DECRYPT = r'''
import json, os, sys
import sqlalchemy as sa
from langflow.services.deps import get_settings_service
from langflow.services.auth.utils import get_fernet
fernet = get_fernet(get_settings_service())
url = os.environ["LANGFLOW_DATABASE_URL"]
eng = sa.create_engine(url.replace("postgresql://", "postgresql+psycopg2://", 1))
vals, ok, bad, plain = {}, 0, 0, 0
with eng.connect() as c:
    for name, typ, val in c.execute(sa.text("select name, type, value from variable where value is not null")):
        if not val.startswith("gAAAA"):
            plain += 1
            if name == "POSTGRES_DB_USER": vals[name] = val
            continue
        try:
            plain_val = fernet.decrypt(val.encode()).decode(); ok += 1
            if name == "POSTGRES_PASSWORD": vals[name] = plain_val
        except Exception:
            bad += 1
res = {"decrypted": ok, "failed": bad, "plain": plain}
try:
    u, p = vals["POSTGRES_DB_USER"], vals["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DB_SERVER", "edu_ai_postgres")
    e2 = sa.create_engine("postgresql+psycopg2://%s:%s@%s:5432/edu_ai" % (u, p, host))
    with e2.connect() as c2:
        res["app_db_connect"] = c2.execute(sa.text("select count(*) from edu_ai.legal_entity")).scalar() > 0
except Exception as e:
    res["app_db_connect"] = False; res["app_db_error"] = type(e).__name__
print(json.dumps(res))
'''

ASK = r'''
import json, sys, urllib.request
ent = sys.argv[1]
body = json.dumps({"question": "Jak se jmenuje nase skola?", "legal_entity": ent, "user_role": "STUDENT"}).encode()
try:
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8090/v1/ask", data=body, headers={"content-type": "application/json"}), timeout=60)
    d = json.loads(r.read().decode())
    print(json.dumps({"http": r.status, "outcome": d.get("outcome") or d.get("status"), "has_answer": bool(d.get("answer"))}))
except Exception as e:
    print(json.dumps({"error": type(e).__name__ + str(getattr(e, "code", ""))}))
'''


def main():
    ap = argparse.ArgumentParser()
    for k in ("src", "cfg", "state", "project", "expect-host", "backup-root"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--expect-images")
    ap.add_argument("--running-from")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--export", action="store_true")
    a = ap.parse_args()
    L = Layout(src=a.src, cfg=a.cfg, state=a.state, project=a.project, host=a.expect_host, backup_root=a.backup_root)
    try:
        ident = C.gate(L, expect_wd=a.running_from)
        rec("gate_own_stack", True, json.dumps(ident))
    except C.Abort as exc:
        rec("gate_own_stack", False, str(exc))
        json.dump(R, open(a.out, "w"), indent=1)
        return 1
    ps = C.image_ids(L)
    health = {}
    for n in ps:
        h = run_text(["docker", "inspect", n, "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"]).stdout.strip()
        health[n] = (ps[n]["status"], h)
    bad = [n for n, (s, h) in health.items() if s != "running" or h not in ("healthy", "none")]
    nohc = sorted(n for n, (s, h) in health.items() if h == "none")
    healthy = sum(1 for s, h in health.values() if h == "healthy")
    rec("containers", len(ps) == 9 and not bad and healthy == 8 and nohc == ["edu_ai_pgadmin"], "%d services, healthy=%d, running without healthcheck=%s, not ok=%s" % (len(ps), healthy, nohc, bad))
    if a.expect_images:
        want = json.load(open(a.expect_images))
        diff = [n for n, v in want.items() if ps.get(n, {}).get("id") != (v["id"] if isinstance(v, dict) else v)]
        rec("image_ids_expected", not diff, "%d containers compared; different: %s" % (len(want), diff))
    R["image_ids"] = {n: ps[n]["id"] for n in ps}
    rec("egress_rules_present", all(C.egress_present(L).values()), json.dumps(C.egress_present(L)))
    pr = C.egress_probes(L)
    rec("egress_probes", all(v == "BLOCKED" for k, v in pr.items() if not k.startswith("control")) and pr["control_default_bridge"] == "CONNECTED", json.dumps(pr))
    rv = run_text(["python3", "scripts/db_roles.py", "verify", "--container", L.pg_container], cwd=L.src, check=False)
    rec("role_contract_verify", rv.returncode == 0 and "MATCH" in rv.stdout, "rc=%d MATCH=%s" % (rv.returncode, "MATCH" in rv.stdout))
    env = C.read_env(L.env_path)
    rec("langflow_url_is_this_copy", env.get("LANGFLOW_URL") == "http://127.0.0.1:8444" and ps["edu_ai_nginx"]["status"] == "running", "LANGFLOW_URL points at the local nginx of this stack")
    r = dx("edu_ai", ["python3", "-"], PROBE)
    lo = probe_json(r)
    lo = {"api_key_ok": lo.get("api_key_ok") is True,
          "flows": C.count_value(lo.get("flows")), "variables": C.count_value(lo.get("variables"))}
    rec("langflow_api_key", lo.get("api_key_ok") is True, "x-api-key from .env accepted (password login is not exercised: with blocked egress it can hold the worker until models.dev times out)")
    owned = C.count_value(pg_psql(L, "langflow", "select count(*) from flow where user_id is not null"))
    nvar = C.count_value(pg_psql(L, "langflow", "select count(*) from variable"))
    rec("langflow_counts_match_db", owned is not None and nvar is not None and lo.get("flows") == owned and lo.get("variables") == nvar,
        "flows api=%s db=%s, variables api=%s db=%s" % (lo.get("flows"), owned, lo.get("variables"), nvar))
    r = dx("edu_ai", ["python3", "-"], DECRYPT)
    dd = decrypt_summary(r)
    rec("fernet_decrypt_and_app_db_connect", dd.get("failed") == 0 and (dd.get("decrypted") or 0) > 0 and dd.get("app_db_connect") is True, json.dumps(dd))
    s = dx("edu_ai_search", ["python3", "-c", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=10).status)"])
    rec("search_health", s.returncode == 0 and s.stdout.strip() == "200", "rc=%d HTTP200=%s" % (s.returncode, s.stdout.strip() == "200"))
    n = dx("edu_ai_nginx", ["wget", "-qO", "-", "http://127.0.0.1/health"])
    rec("nginx_health", n.returncode == 0, "rc=%d" % n.returncode)
    ents = pg_psql(L, "edu_ai", "select legal_entity_code from edu_ai.legal_entity where is_active order by 1").split()
    alias = {e: "E%d" % (i + 1) for i, e in enumerate(ents)}
    asks = {}
    for e in ents:
        r = dx("edu_ai_search", ["python3", "-c", ASK, e])
        value = probe_json(r)
        http = C.count_value(value.get("http"))
        asks[alias[e]] = {"http": http if http and 100 <= http <= 599 else None,
                          "has_answer": value.get("has_answer") is True}
    rec("entities_answer_without_llm", ents and all("error" not in v and v.get("http") == 200 for v in asks.values()), json.dumps(asks))
    if a.smoke:
        env2 = dict(C.clean_env())
        r = subprocess.run(["python3", "scripts/smoke.py", "--source-root", L.src, "--config-dir", L.cfg, "--state-dir", L.state, "--project", L.project],
                           cwd=L.src, capture_output=True, text=True, env=env2)
        m = None
        for line in r.stdout.splitlines():
            m = re.match(r"(\d+) passed, (\d+) failed, (\d+) skipped", line) or m
        rec("smoke", r.returncode == 0 and m is not None and m.group(2) == "0", "rc=%d %s" % (r.returncode, "/".join(m.groups()) if m else "no summary"))
        R["smoke_counts"] = dict(zip(("passed", "failed", "skipped"), map(int, m.groups()))) if m else None
    if a.export:
        api = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cycle_export.py")
        r = subprocess.run(["python3", api, "--cfg", L.cfg, "--state", L.state, "--project", L.project, "--src", L.src,
                            "--expect-host", L.host, "--backup-root", L.backup_root,
                            *(["--running-from", a.running_from] if a.running_from else [])], capture_output=True, text=True)
        summary = export_summary(r)
        rec("sqlite_export_matches_db", summary["rc"] == 0 and (summary["packs"] or 0) > 0, json.dumps(summary))
    json.dump(R, open(a.out, "w"), indent=1)
    return 0 if all(v["result"] == "PASS" for v in R.values() if isinstance(v, dict) and "result" in v) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("CHECKS_FAILED type=%s" % type(exc).__name__)
        sys.exit(1)
