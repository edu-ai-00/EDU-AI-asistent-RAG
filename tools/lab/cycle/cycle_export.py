#!/usr/bin/env python3
"""Real SQLite export of one active pack per active entity through the Langflow flow (key read inside the container,
never printed), compared with the database. Entities are shown as E1..En. Free of LLM calls.
"""
from __future__ import annotations

import argparse
import json
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cycle_core as C

RUN = r'''
import json, os, sys, urllib.request
ent, pack = sys.argv[1], sys.argv[2]
tw = {"ExportQAPack-qa010": {"pack_id": pack, "output_dir": "/data/sqllite_export", "legal_entity": ent}}
body = json.dumps({"output_type": "text", "input_type": "text", "input_value": "x", "tweaks": tw}).encode()
req = urllib.request.Request("http://127.0.0.1:7860/api/v1/run/edu-ai-export-qa-pack?stream=false", data=body, method="POST",
                             headers={"Content-Type": "application/json", "x-api-key": os.environ["LANGFLOW_API_KEY"]})
try:
    print(urllib.request.urlopen(req, timeout=300).status)
except Exception as e:
    print("ERR", type(e).__name__, getattr(e, "code", ""))
'''
CHECK = r'''
import sqlite3, sys
d = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print(d.execute("select count(*) from entries").fetchone()[0], d.execute("select count(*) from variants").fetchone()[0],
      d.execute("pragma integrity_check").fetchone()[0], d.execute("select count(*) from pack_manifest where subject is null and grade is null").fetchone()[0])
'''


def sh(argv, inp=None):
    return C.run_text(argv, input=inp)


def psql(q):
    return sh(["docker", "exec", "edu_ai_postgres", "psql", "-X", "-At", "-U", "edu_ai_admin", "-d", "edu_ai", "-c", q]).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    for k in ("cfg", "state", "project", "src"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--expect-host", required=True)
    ap.add_argument("--backup-root", required=True)
    ap.add_argument("--running-from")
    a = ap.parse_args()
    C.gate(C.Layout(a.src, a.cfg, a.state, a.project, a.expect_host, a.backup_root), expect_wd=a.running_from)
    fail, shown = 0, 0
    for i, ent in enumerate(psql("select legal_entity_code from edu_ai.legal_entity where is_active order by 1").split(), start=1):
        # Keep the export contract's entity alphabet without imposing the
        # marker/bundle identifier's separate 64-character bound on school data.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", ent):
            raise C.Abort("invalid entity code from the register")
        pack = psql("select pack_id from edu_ai.qa_pack_manifest where legal_entity_code='%s' and is_active order by pack_id limit 1" % ent)
        if not pack:
            print("E%d: no active pack" % i)
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", pack) or pack in (".", "..") or pack.endswith("."):
            raise C.Abort("invalid pack code from the register")
        res = sh(["docker", "exec", "-i", "edu_ai", "python3", "-", ent, pack], RUN).stdout.strip()
        f = "%s/sqllite_export/%s/%s.sqlite" % (a.state, ent, pack)
        r = sh(["sudo", "python3", "-c", CHECK, f]).stdout.split()
        de = psql("select count(*) from edu_ai.qa_entries where pack_id='%s' and legal_entity_code='%s' and is_active" % (pack, ent))
        dv = psql("select count(*) from edu_ai.qa_variants v join edu_ai.qa_entries e using(entry_id) where e.pack_id='%s' and e.legal_entity_code='%s' and e.is_active and v.is_active" % (pack, ent))
        ok = res == "200" and len(r) == 4 and r[0] == de and r[1] == dv and r[2] == "ok" and r[3] == "1"
        fail |= not ok
        shown += 1
        print("E%d: http200=%s entries sqlite=%s db=%s variants sqlite=%s db=%s integrity_ok=%s nullsubjectgrade=%s %s" %
              (i, res == "200", C.count_value(r[0]) if r else None, C.count_value(de),
               C.count_value(r[1]) if len(r) > 1 else None, C.count_value(dv), len(r) > 2 and r[2] == "ok",
               C.count_value(r[3]) if len(r) > 3 else None, "MATCH" if ok else "DIFF"))
    left = sh(["sudo", "find", a.state + "/sqllite_export", "-name", ".qa-export-*"]).stdout.split()
    print("leftover temp files: %d" % len(left))
    if fail or not shown:
        print("EXPORT_FAIL")
        return 1
    print("EXPORT_OK packs=%d" % shown)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("EXPORT_FAIL type=%s" % type(exc).__name__)
        sys.exit(1)
