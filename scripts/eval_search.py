#!/usr/bin/env python3
# last change in code by Jan Drozd and Martin Safranek (2026-08-13).
"""Search eval harness — runs the golden cases (eval/search_cases.json, derived
from produktový vlastník's RAG test set) against /search and reports retrieval + outcome
metrics, so a retrieval change (synonyms, reranking, hybrid) can be measured
instead of eyeballed.

Metrics per case:
  outcome_ok  — the cascade outcome matches the expectation (answer vs no_answer;
                Q6 must say "nevím" because the info is not in the corpus)
  source_hit  — a returned source URL/title contains the expected fragment
                (retrieval recall — did the right document surface?)
  pass        — outcome_ok AND (source_hit for 'answer' cases; nothing extra for
                'no_answer' cases)

Aggregate: recall (answer cases), outcome accuracy (all), pass rate (all).
The trace (strategies) is printed so a miss can be read as "wrong retriever"
vs "document absent from the corpus".

Usage:
  python3 scripts/eval_search.py                     # default cases + :8090
  SEARCH_URL=http://127.0.0.1:8090 python3 scripts/eval_search.py
  python3 scripts/eval_search.py --json              # machine-readable summary
Requires only the Python standard library.
"""
from __future__ import annotations
import json, os, sys, time, urllib.request

SEARCH_URL = os.getenv("SEARCH_URL", "http://127.0.0.1:8090").rstrip("/")
CASES_PATH = os.getenv("EVAL_CASES",
                       os.path.join(os.path.dirname(__file__), "..", "eval", "search_cases.json"))


def _search(question, legal_entity, role=None, grade=None):
    body = {"question": question, "legal_entity": legal_entity}
    if role:
        body["user_role"] = role
    if grade is not None:
        body["user_school_grade"] = grade
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{SEARCH_URL}/v1/ask", data=data,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.load(r)
    resp["_ms"] = int((time.time() - t0) * 1000)
    return resp


def _source_strings(resp):
    raw = resp.get("sources") or []
    if isinstance(raw, str):            # pre-2026-08-13 builds sent a JSON string
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    out = []
    for s in raw:
        if isinstance(s, dict):
            out.append(" ".join(str(s.get(k) or "") for k in
                                 ("url", "source_url", "title", "file_name")))
        else:
            out.append(str(s))
    return [x.lower() for x in out]


def main():
    want_json = "--json" in sys.argv
    spec = json.load(open(CASES_PATH, encoding="utf-8"))
    entity = spec.get("legal_entity")
    cases = spec["cases"]

    rows, recall_den, recall_num, outcome_ok_n, pass_n = [], 0, 0, 0, 0
    for c in cases:
        try:
            resp = _search(c["query"], entity, c.get("role"), c.get("grade"))
        except Exception as e:
            rows.append({"id": c["id"], "error": f"{type(e).__name__}: {e}"})
            continue
        outcome = resp.get("outcome")
        srcs = _source_strings(resp)
        exp = [s.lower() for s in (c.get("expect_source") or [])]
        source_hit = any(any(frag in s for s in srcs) for frag in exp) if exp else None
        outcome_ok = (outcome == c["expect_outcome"])
        if c["expect_outcome"] == "answer":
            recall_den += 1
            if source_hit:
                recall_num += 1
            passed = bool(outcome_ok and source_hit)
        else:  # no_answer case: pass on the right refusal
            passed = bool(outcome_ok)
        outcome_ok_n += int(outcome_ok)
        pass_n += int(passed)
        rows.append({
            "id": c["id"], "outcome": outcome, "expect": c["expect_outcome"],
            "outcome_ok": outcome_ok, "source_hit": source_hit, "pass": passed,
            "strategies": resp.get("strategies"), "ms": resp.get("_ms"),
            "sources": [s[:70] for s in srcs][:4],
        })

    summary = {
        "search_url": SEARCH_URL, "n_cases": len(cases),
        "recall_answer_cases": (round(recall_num / recall_den, 3) if recall_den else None),
        "outcome_accuracy": round(outcome_ok_n / len(cases), 3) if cases else None,
        "pass_rate": round(pass_n / len(cases), 3) if cases else None,
    }
    if want_json:
        print(json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2))
        return

    print(f"\nSearch eval — {SEARCH_URL}  ({len(cases)} cases)\n" + "=" * 72)
    for r in rows:
        if "error" in r:
            print(f"  ✗ {r['id']:22} ERROR {r['error']}")
            continue
        mark = "✓" if r["pass"] else "✗"
        sh = {True: "hit", False: "MISS", None: "—"}[r["source_hit"]]
        print(f"  {mark} {r['id']:22} outcome={r['outcome']:>10} "
              f"(want {r['expect']:>10}) src={sh:>4}  {r['ms']}ms  {r['strategies']}")
        for s in r["sources"]:
            print(f"        · {s}")
    print("=" * 72)
    print(f"  recall (answer cases): {summary['recall_answer_cases']}   "
          f"outcome acc: {summary['outcome_accuracy']}   "
          f"PASS rate: {summary['pass_rate']}\n")


if __name__ == "__main__":
    main()
