#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-08-20).
"""Baseline snapshot of the 30+2 eval cases: outcome, source and the CHUNK IDS
each answer was built from. Run before and after a retrieval change; the chunk
id lists are the regression signal a pass/fail count hides.

Why this exists next to scripts/eval_search.py: that harness answers "did the
case pass". This one answers "did the SAME chunks build the answer" — a change
can keep every pass and still swap the evidence underneath, which is exactly
what a retrieval/ranking change does. Run it before a change and after, then
diff the chunk_ids per case.

Usage:
  python3 scripts/eval_snapshot.py eval/baseline_before.json
  python3 scripts/eval_snapshot.py eval/baseline_after.json
  # then: python3 scripts/eval_snapshot.py --diff before.json after.json

  # against production, from the server host (8090 is not published there —
  # take the address from `docker inspect edu_ai_search`):
  SEARCH_URL=http://172.20.0.3:8090 python3 scripts/eval_snapshot.py out.json

Write the file somewhere PERSISTENT. Run it inside the search container and the
baseline dies with the next rebuild, which is exactly the rebuild it was taken to
be compared across.
"""
import json, os, sys, urllib.request, time

# 127.0.0.1 is right for a dev machine running the compose override, which
# publishes 8090. On the production host that port is NOT published, so the only
# place the hardcoded value worked was INSIDE the search container — whose
# filesystem is wiped by the next `up -d`. A baseline written there is lost by
# the very rebuild it exists to be compared across; that happened on 2026-08-20
# and cost the before-snapshot of the 153-158 deploy. SEARCH_URL lets it run on
# the host against the container's bridge address, so the file lands on a disk
# that survives:
#     SEARCH_URL=http://172.20.0.3:8090 python3 scripts/eval_snapshot.py out.json
URL = os.getenv("SEARCH_URL", "http://127.0.0.1:8090").rstrip("/") + "/v1/ask"
SPEC = json.load(open("eval/search_cases_v2.json", encoding="utf-8"))
ENT = SPEC["default_legal_entity"]
UID = {"SKOLA_NOVA_VES_ZS": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
       "SKOLA_NOVA_VES_SOS": "3cd81a92-957e-4208-90aa-dcf0f7207286",
       "SKOLA_NOVA_VES": "422dae54-27f6-4625-87a8-c62a61e911e1"}


def ask(q, ent, role=None, grade=None):
    body = {"question": q, "legal_entity": ent, "debug": True}
    if UID.get(ent):
        body["user_id"] = UID[ent]
    if role:
        body["user_role"] = role
    if grade is not None:
        body["user_school_grade"] = grade
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def chunk_ids(resp):
    """Every chunk id the trace touched, per step, in order — plus the union."""
    per_step, seen = [], []
    for st in (resp.get("trace") or []):
        ids = (st.get("added_chunk_ids") or []) + (st.get("annotated_chunk_ids") or [])
        if ids:
            per_step.append({"step": st.get("step"), "ids": ids})
            for i in ids:
                if i not in seen:
                    seen.append(i)
    return per_step, seen


def diff(a_path, b_path):
    """Case-by-case delta: outcome, source and the chunk id set."""
    A = {r["id"]: r for r in json.load(open(a_path, encoding="utf-8"))}
    B = {r["id"]: r for r in json.load(open(b_path, encoding="utf-8"))}
    changed = 0
    for cid in [k for k in A if k in B]:
        a, b = A[cid], B[cid]
        da = []
        if a.get("outcome") != b.get("outcome"):
            da.append(f"outcome {a.get('outcome')} -> {b.get('outcome')}")
        if a.get("source") != b.get("source"):
            da.append(f"source {a.get('source')} -> {b.get('source')}")
        sa, sb = set(a.get("chunk_ids") or []), set(b.get("chunk_ids") or [])
        if sa != sb:
            da.append(f"chunks +{sorted(sb - sa)} -{sorted(sa - sb)}")
        elif (a.get("chunk_ids") or []) != (b.get("chunk_ids") or []):
            da.append("chunks same set, DIFFERENT order")
        if da:
            changed += 1
            print(f"  {cid:<26} " + "; ".join(da))
    only = sorted(set(A) ^ set(B))
    if only:
        print(f"  cases only in one file: {only}")
    print(f"\n  {changed} of {len(A)} case(s) changed")


def main():
    if "--diff" in sys.argv:
        i = sys.argv.index("--diff")
        return diff(sys.argv[i + 1], sys.argv[i + 2])
    cases = [dict(c, _set="v2") for c in SPEC["cases"]] + \
            [dict(c, _set="level") for c in SPEC["level_cases"]]
    out = []
    for i, c in enumerate(cases, 1):
        ent = c.get("legal_entity") or ENT
        t0 = time.time()
        try:
            r = ask(c["query"], ent, c.get("role"), c.get("grade"))
        except Exception as e:
            out.append({"id": c["id"], "error": f"{type(e).__name__}: {e}"})
            print(f"  {i:2}/{len(cases)} {c['id']:<26} ERROR {e}")
            continue
        per_step, union = chunk_ids(r)
        row = {"id": c["id"], "set": c["_set"], "query": c["query"],
               "entity": ent, "role": c.get("role"), "grade": c.get("grade"),
               "outcome": r.get("outcome"), "source": r.get("answer_source"),
               "strategies": [s.get("step") for s in (r.get("trace") or [])
                              if s.get("status") == "ran"],
               "sources": [s.get("title") or s.get("url") for s in (r.get("sources") or [])],
               "chunk_ids": union, "chunk_ids_by_step": per_step,
               "answer_head": (r.get("answer") or "")[:120],
               "ms": int((time.time() - t0) * 1000)}
        out.append(row)
        print(f"  {i:2}/{len(cases)} {c['id']:<26} {str(r.get('outcome')):<10} "
              f"{str(r.get('answer_source')):<16} chunks={len(union):<3} {row['ms']:>6} ms")
    path = sys.argv[1] if len(sys.argv) > 1 else "eval/baseline.json"
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for r in out if r.get("outcome") == "answer")
    print(f"\n  answer={ok}/{len(out)}  → {path}")


if __name__ == "__main__":
    main()
