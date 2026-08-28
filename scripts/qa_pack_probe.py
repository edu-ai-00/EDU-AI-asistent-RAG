#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-08-21).
"""Probe the curated QA packs: ask each case as its caller, check WHICH pack answered.

The search eval harness (scripts/eval_search.py) measures retrieval — one entity
for the whole file, and it grades on the source URL. Neither fits here: a QA pack
answers with no source at all, and the whole point is that the answer came from
the CALLER'S school rather than from the organisation or another tenant. So this
runs per-case entity/role/ročník and grades on the [SCOPE] tag in the answer.

    python3 scripts/qa_pack_probe.py                 # all cases against :8090
    python3 scripts/qa_pack_probe.py --only scope    # one family
    python3 scripts/qa_pack_probe.py --json          # machine-readable
    SEARCH_URL=http://127.0.0.1:8090 python3 scripts/qa_pack_probe.py

A leak is the failure that matters: every case forbids the tags of the four
schools that are not the caller's, so a cross-tenant answer fails wherever the
cascade produced it.
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.request

SEARCH_URL = os.getenv("SEARCH_URL", "http://127.0.0.1:8090").rstrip("/")
CASES = os.getenv("QA_PROBE_CASES",
                  os.path.join(os.path.dirname(__file__), "..", "QA", "probe_cases.json"))


def _ask(c: dict) -> dict:
    body = {"question": c["query"], "legal_entity": c["legal_entity"]}
    if c.get("role"):
        body["user_role"] = c["role"]
    if c.get("grade") is not None:
        body["user_school_grade"] = c["grade"]
    req = urllib.request.Request(f"{SEARCH_URL}/v1/ask",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    resp["_ms"] = int((time.time() - t0) * 1000)
    return resp


def _check(c: dict, r: dict) -> list[str]:
    """Every expectation the case states. Absent expectations are not checked —
    a case may assert only that nothing leaked."""
    bad, answer = [], r.get("answer") or ""
    src, outcome = r.get("answer_source"), r.get("outcome")

    if c.get("expect_outcome") and outcome != c["expect_outcome"]:
        bad.append(f"outcome={outcome} want {c['expect_outcome']}")
    if c.get("expect_outcome_in") and outcome not in c["expect_outcome_in"]:
        bad.append(f"outcome={outcome} want one of {c['expect_outcome_in']}")
    if c.get("expect_answer_source") and src != c["expect_answer_source"]:
        bad.append(f"source={src} want {c['expect_answer_source']}")
    if c.get("expect_answer_source_in") and src not in c["expect_answer_source_in"]:
        bad.append(f"source={src} want one of {c['expect_answer_source_in']}")
    if src in (c.get("forbid_answer_source") or []):
        bad.append(f"source={src} is forbidden")
    for frag in c.get("expect_answer_contains") or []:
        if frag not in answer:
            bad.append(f"answer lacks {frag!r}")
    for frag in c.get("forbid_answer_contains") or []:
        if frag in answer:
            bad.append(f"LEAK: answer carries {frag!r}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="run only cases whose id starts with this")
    ap.add_argument("--limit", type=int, help="stop after N cases")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print failures only")
    a = ap.parse_args()

    cases = json.load(open(CASES, encoding="utf-8"))["cases"]
    if a.only:
        cases = [c for c in cases if c["id"].startswith(a.only)]
    if a.limit:
        cases = cases[:a.limit]
    if not cases:
        print("  žádné případy k testování"); return 1

    rows, fams = [], {}
    for c in cases:
        try:
            r = _ask(c)
        except Exception as e:
            rows.append({"id": c["id"], "fail": [f"{type(e).__name__}: {e}"], "ms": 0})
            continue
        bad = _check(c, r)
        rows.append({"id": c["id"], "fail": bad, "outcome": r.get("outcome"),
                     "source": r.get("answer_source"), "ms": r["_ms"],
                     "answer": (r.get("answer") or "")[:90],
                     "leak": any(f.startswith("LEAK") for f in bad),
                     "entity": c["legal_entity"], "role": c.get("role"),
                     "grade": c.get("grade"), "query": c["query"]})
        if not a.quiet and not a.json:
            print(f"  {'✓' if not bad else '✗'} {c['id']:<26} "
                  f"{str(r.get('answer_source')):<12} {r['_ms']:>6}ms"
                  + ("" if not bad else "  " + "; ".join(bad)))

    for r in rows:
        f = r["id"].split("_")[0]
        d = fams.setdefault(f, {"n": 0, "pass": 0, "leak": 0})
        d["n"] += 1
        d["pass"] += int(not r["fail"])
        d["leak"] += int(r.get("leak", False))

    failed = [r for r in rows if r["fail"]]
    summary = {"search_url": SEARCH_URL, "n": len(rows),
               "pass": len(rows) - len(failed),
               "leaks": sum(f["leak"] for f in fams.values()),
               "families": fams}
    if a.json:
        print(json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2))
        return 1 if failed else 0

    print("\n" + "=" * 74)
    for f, d in sorted(fams.items()):
        mark = "✓" if d["pass"] == d["n"] else "✗"
        print(f"  {mark} {f:<8} {d['pass']:>4}/{d['n']:<4} pass"
              + (f"   {d['leak']} LEAK" if d["leak"] else ""))
    if failed:
        print(f"\n  {len(failed)} selhalo:")
        for r in failed:
            print(f"    ✗ {r['id']:<26} {r.get('entity','')} "
                  f"{r.get('role') or '-'}/{r.get('grade') if r.get('grade') is not None else '-'}"
                  f"  {'; '.join(r['fail'])}")
            print(f"        q: {r.get('query','')}")
            print(f"        a: {r.get('answer','')}")
    print(f"\n  {summary['pass']}/{summary['n']} pass, {summary['leaks']} leaks — {SEARCH_URL}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
