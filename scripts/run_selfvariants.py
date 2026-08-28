#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-08-20).
"""Each stored QA variant, asked verbatim, must return its OWN entry.

This tests the INGEST, not the search. `qa_variants` carries norm_text, lemma_text
and stem_text, and they must be produced by the SAME reducers the query side uses
(the engines in the active settings). When they drift apart, `qa_exact` — plain
string equality — silently never fires, `qa_lexical` catches the query anyway, the
answer still arrives, and nobody notices. Only the STEP that matched reveals it,
which is why it is reported per step and not just as a pass rate.

Asking the variant verbatim is the strongest form: if the exact stored phrasing
cannot find its own entry, no user phrasing will.

    python3 scripts/run_selfvariants.py
"""
from __future__ import annotations
import collections, json, sys, urllib.request

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
    cases = json.load(open("eval/selfvariant_cases.json", encoding="utf-8"))["cases"]
    print(f"  {len(cases)} uložených variant, každá se ptá doslova\n")
    rows = []
    for i, c in enumerate(cases, 1):
        r = ask(c)
        got = r.get("matched_entry_id")
        src = r.get("answer_source") or "none"
        hit = got == c["expected_entry_id"]
        rows.append({**c, "outcome": r.get("outcome"), "branch": src,
                     "matched_entry_id": got, "match_score": r.get("match_score"),
                     "own_entry": hit})
        print(f"  {'✓' if hit else '✗'} {i:>3}/{len(cases)} {src:<12}"
              f" {str(r.get('match_score')):<7} {c['question'][:58]}")
    json.dump({"cases": rows}, open("eval/selfvariant_run.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    n = len(rows)
    hit = sum(1 for r in rows if r["own_entry"])
    print(f"\n  trefilo SVOU položku: {hit}/{n} ({100.0*hit/n:.0f} %)")
    print("\n  kterým krokem:")
    for k, v in collections.Counter(r["branch"] for r in rows).most_common():
        print(f"    {k:<14} {v:>3}")
    # qa_exact is the parity signal: a stored phrasing asked verbatim SHOULD be an
    # exact match. A low count here with a high overall pass rate is exactly the
    # silent drift this test exists for.
    ex = sum(1 for r in rows if r["branch"] == "qa_exact")
    print(f"\n  přes qa_exact: {ex}/{n} ({100.0*ex/n:.0f} %)"
          f"   ← nízké číslo při vysoké úspěšnosti = rozejité reducery")
    bad = [r for r in rows if not r["own_entry"]]
    if bad:
        print("\n  ── netrefilo svou položku ──")
        for r in bad[:20]:
            print(f"   [{r['pack_id']}] {r['question'][:62]}")
            print(f"      {r['branch']} → {r['matched_entry_id']} (čekáno {r['expected_entry_id']})")
        by = collections.Counter(r["pack_id"] for r in bad)
        print(f"\n  po balíčcích: {dict(by.most_common(8))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
