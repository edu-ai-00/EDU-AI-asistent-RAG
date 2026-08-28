#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-08-20).
"""Negative test: every case here MUST come back no_answer.

Two groups, and they fail for different reasons:

  cizi_skola   a question drawn from ANOTHER tenant's corpus. An answer means the
               entity/visibility predicate leaked, which is the worst failure the
               system can have.
  mimo_korpus  a topic the corpus does not hold at all. An answer means a loose
               vector match was accepted — and on the QA path that is returned
               VERBATIM with no grounding check, so it reads as curated fact.

Why this test grew teeth today: the QA corpus went from 40 entries to 151, and
qa_vector accepts at cosine 0.65. More entries means more chances that an
off-topic question lands "close enough" to one of them.

    python3 scripts/run_negative.py
"""
from __future__ import annotations
import json, sys, urllib.request

URL = "http://127.0.0.1:8090/v1/ask"
UID = {"SKOLA_NOVA_VES_ZS": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
       "SKOLA_NOVA_VES_SOS": "3cd81a92-957e-4208-90aa-dcf0f7207286",
       "SKOLA_NOVA_VES": "422dae54-27f6-4625-87a8-c62a61e911e1",
       "ZAKLADNI_SKOLA_LIPOVA": "be164723-a616-4a8b-a900-c9c555cb8326"}


def ask(c):
    body = {"question": c["question"], "legal_entity": c["legal_entity"],
            "user_role": "STUDENT", "debug": True}
    if UID.get(c["legal_entity"]):
        body["user_id"] = UID[c["legal_entity"]]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.load(r)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def main():
    cases = json.load(open("eval/negative_cases.json", encoding="utf-8"))["cases"]
    print(f"  {len(cases)} případů — u všech se čeká no_answer\n")
    rows, fails = [], []
    for i, c in enumerate(cases, 1):
        r = ask(c)
        oc = r.get("outcome") or ("ERROR" if "_error" in r else None)
        src = r.get("answer_source") or "none"
        # a guard block is also a refusal — different reason, same outcome for us
        good = oc in ("no_answer", "blocked", "unusable")
        rows.append({**c, "outcome": oc, "branch": src,
                     "answer_head": (r.get("answer") or "")[:150],
                     "match_score": r.get("match_score"),
                     "matched_entry_id": r.get("matched_entry_id"), "ok": good})
        if not good:
            fails.append(rows[-1])
        print(f"  {'✓' if good else '✗'} {i:>2}/{len(cases)} {c['kind']:<13}"
              f" {str(oc):<10} {src:<12} {c['question'][:52]}")
    json.dump({"cases": rows}, open("eval/negative_run.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    n = len(rows)
    print(f"\n  správně odmítnuto: {n - len(fails)}/{n}")
    for g in ("cizi_skola", "mimo_korpus"):
        sub = [r for r in rows if r["kind"] == g]
        if sub:
            print(f"    {g:<13} {sum(1 for r in sub if r['ok'])}/{len(sub)}")
    if fails:
        print("\n  ── ODPOVĚDĚLO, ALE NEMĚLO ──")
        for f in fails:
            print(f"   [{f['kind']}] {f['question']}")
            print(f"      {f['branch']} skóre={f['match_score']}  {f['answer_head'][:110]}")
            print(f"      proč nemělo: {f['why']}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
