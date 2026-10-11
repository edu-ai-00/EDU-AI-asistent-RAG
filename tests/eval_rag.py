#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-08-22).
"""Regression eval over the RAG cascade, with per-question METADATA.

    python3 tests/eval_rag.py                       # run + write a snapshot
    python3 tests/eval_rag.py --repeat 3            # ask each question 3x, keep the majority
    python3 tests/eval_rag.py --baseline tests/eval_out/<file>.json

Unlike eval_search.py (one question per line, no metadata), every case here
carries the role, grade and entity — the three inputs that decide the answer as
much as the wording does, and the reason a reported finding cannot be reproduced
from a bare question list.

A case may also carry `domains` (a list of DOMAIN codes), restricting the search
to those thematic areas. It is OMITTED from the request when absent, so the
default set keeps measuring exactly what it measured before the field existed;
the snapshot records both what was SENT and what effective_scope said was USED,
so a scoped run that came back unrestricted is reported as a scope failure
rather than as a set of outcomes. Scoped cases live in their own file, so the
default regression measurement is not diluted by them:

    python3 tests/eval_rag.py --questions tests/eval_rag_domains.jsonl --repeat 3

The snapshot is the point: a fix is only credible if the whole set is re-measured,
and `--baseline` prints exactly what moved (outcome, cited sources, answer text).

`--repeat N` is the answer to the set's NOISE FLOOR: two runs of identical code
disagree on 3-4 of 38 cases (the answer LLM decides borderline ones differently),
so a diff of one case says nothing. Each question is asked N times and the case
keeps the MAJORITY (outcome, n_sources, first source) plus `unstable` — how many
of the N runs disagreed with it. A snapshot taken this way makes a real one-case
improvement visible, and the diff prints the flapping cases separately instead of
mixing them into the verdict.
"""

import argparse
import datetime as _dt
import json
import os
import pathlib
import time
import urllib.error
import urllib.request

SEARCH = os.getenv("SEARCH_URL", "http://127.0.0.1:8090/v1/ask")
ROOT = pathlib.Path(__file__).resolve().parent
QFILE = ROOT / "eval_rag_regression.jsonl"
OUTDIR = ROOT / "eval_out"


def load_cases(path):
    cases = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            cases.append(json.loads(s))
    return cases


def build_payload(case) -> dict:
    """The request body for one case — the ONE place a case field becomes a field.

    Extracted from `ask` so `domains` cannot be added to the JSONL and quietly
    not sent, which is exactly what would otherwise have happened and is the
    failure the plan for this change named outright ("Pokud přidáš domains jen do
    JSONL, server je nedostane"). tests/test_eval_payload.py asserts against
    this function, so the harness is checked rather than assumed.

    `domains` is OMITTED when the case does not name it — never sent as null. An
    unrestricted body therefore stays byte-identical to the one every existing
    snapshot was measured with, so an older baseline is still comparable.
    """
    body = {"question": case["question"], "legal_entity": case["entity"],
            "user_role": case["role"]}
    if case.get("grade"):
        body["user_school_grade"] = case["grade"]
    if case.get("domains") is not None:
        body["domains"] = case["domains"]
    return body


def ask(case, timeout=240):
    body = build_payload(case)
    t0 = time.time()
    req = urllib.request.Request(SEARCH, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        # THE STATUS AND THE ENVELOPE, not just "something failed". A scoped case
        # that 422s because a code was retired is a different finding from a
        # timeout, and one `except Exception` collapsed the two into an ERROR row
        # with a truncated message.
        try:
            _b = json.loads(e.read().decode("utf-8", "replace"))
            _err = ((_b or {}).get("error") or {})
        except Exception:  # noqa: BLE001
            _err = {}
        return {"outcome": "ERROR", "http_status": e.code,
                "error_code": _err.get("code"),
                "error": (_err.get("message") or str(e))[:160],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"outcome": "ERROR", "error": str(e)[:120],
                "ms": int((time.time() - t0) * 1000)}
    src = d.get("sources")
    src = json.loads(src) if isinstance(src, str) else (src or [])
    _scope = d.get("effective_scope") or {}
    return {"outcome": d.get("outcome"), "answer_source": d.get("answer_source"),
            "n_sources": len(src),
            "sources": [(s.get("url") or s.get("title") or "") for s in src],  # celé URL: zkrácení mrzačilo odkazy v reportech
            "answer": (d.get("answer") or "").strip(),
            # WHAT SCOPE THE SERVER SAID IT USED. Recorded in the snapshot so a
            # scoped run can be checked and diffed on it — a run whose cases name
            # areas and whose results all say null sent nothing, and without this
            # column that looks like a passing run.
            "domains_sent": body.get("domains"),
            "domains_effective": _scope.get("domains"),
            "ms": int((time.time() - t0) * 1000)}


def _majority(runs: list[dict]) -> dict:
    """The result N runs agree on, plus how many of them dissented.

    The vote is on the triple the diff compares — (outcome, n_sources, 1st source)
    — because that is what a regression is judged by; the returned dict is the
    FIRST run holding the winning triple, so its answer text and trace belong
    together. `unstable` counts the runs outside the majority, `runs` keeps every
    triple so a flapping case can be read from the snapshot without re-running.
    """
    from collections import Counter

    def key(r):
        return (r.get("outcome"), r.get("n_sources"),
                (r.get("sources") or [None])[0])

    votes = Counter(key(r) for r in runs)
    win, n = votes.most_common(1)[0]
    best = next(r for r in runs if key(r) == win)
    return {**best,
            "ms": sorted(r.get("ms", 0) for r in runs)[len(runs) // 2],
            "runs": [list(key(r)) for r in runs],
            "unstable": len(runs) - n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="snapshot JSON to compare against")
    ap.add_argument("--only", help="run only ids starting with this (M / N / M3)")
    ap.add_argument("--questions", default=str(QFILE))
    ap.add_argument("--repeat", type=int, default=1,
                    help="kolikrát se každá otázka zeptá; výsledek je většina (default 1)")
    args = ap.parse_args()

    cases = load_cases(args.questions)
    if args.only:
        cases = [c for c in cases if c["id"].startswith(args.only)]
    results = {}
    print(f"{'id':5} {'outcome':10} {'odkud':10} {'dok':3} {'ms':>6}  otázka → 1. zdroj")
    for c in cases:
        r = ask(c) if args.repeat <= 1 else _majority([ask(c) for _ in range(args.repeat)])
        results[c["id"]] = {**c, **r}
        first = (r.get("sources") or ["—"])[0] if r.get("n_sources") else "—"
        print(f"{c['id']:5} {str(r.get('outcome')):10} {str(r.get('answer_source') or '-'):10} "
              f"{str(r.get('n_sources','-')):3} {r.get('ms',0):>6}  {c['question'][:38]:38} → {first[:40]}")

    # ── DID THE SCOPE ACTUALLY APPLY? ─────────────────────────────
    # Checked, not assumed. A case that names areas and comes back with
    # effective_scope.domains = null means the field never reached the server,
    # and every outcome in the run would then be measuring the unrestricted
    # cascade while the report claimed otherwise.
    _scoped = [(k, r) for k, r in results.items() if r.get("domains_sent")]
    if _scoped:
        _bad = [k for k, r in _scoped
                if r.get("outcome") != "ERROR"
                and sorted(r.get("domains_effective") or []) !=
                sorted({d.strip().upper() for d in r["domains_sent"]})]
        print(f"\n==== scope: {len(_scoped)} případů s doménami ====")
        if _bad:
            print("  !! server NEPOUŽIL zadané domény u: " + ", ".join(sorted(_bad)))
            print("     (effective_scope.domains se neshoduje s odeslanými kódy —"
                  " výsledky tohoto běhu nejsou scoped)")
        else:
            print("  ok — effective_scope.domains odpovídá odeslaným kódům u všech")
        _errs = [(k, r) for k, r in _scoped if r.get("outcome") == "ERROR"]
        if _errs:
            print("  chyby: " + ", ".join(
                f"{k}={r.get('http_status') or '-'}/{r.get('error_code') or 'net'}"
                for k, r in sorted(_errs)))

    from collections import Counter
    oc = Counter(r.get("outcome") for r in results.values())
    print(f"\n==== {len(results)} otázek ====")
    for k, v in oc.most_common():
        print(f"  {str(k):10} {v:3}  ({100*v//len(results)} %)")
    src = Counter(r.get("answer_source") for r in results.values())
    print("  --- odkud odpověď přišla ---")
    for k, v in src.most_common():
        print(f"  {str(k):12} {v:3}")
    _rag = [r.get("ms", 0) for r in results.values() if r.get("answer_source") == "rag"]
    if _rag:
        _rag.sort()
        print(f"  latence RAG: median {_rag[len(_rag)//2]} ms · max {_rag[-1]} ms ({len(_rag)} dotazů)")
    _unst = [k for k, r in results.items() if r.get("unstable")]
    if _unst:
        print(f"  --- kolísavé případy ({len(_unst)}): " + ", ".join(sorted(_unst)) + " ---")
    lat = sorted(r.get("ms", 0) for r in results.values())
    if lat:
        print(f"  latence: median {lat[len(lat)//2]} ms · max {lat[-1]} ms")

    OUTDIR.mkdir(exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = OUTDIR / f"{stamp}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nsnapshot: {out}")

    if args.baseline:
        base = json.loads(pathlib.Path(args.baseline).read_text(encoding="utf-8"))
        print(f"\n==== ZMĚNY vs {pathlib.Path(args.baseline).name} ====")
        moved = 0
        for qid, now in results.items():
            was = base.get(qid)
            if not was:
                print(f"  {qid}: NOVÝ případ"); continue
            diffs = []
            if was.get("outcome") != now.get("outcome"):
                diffs.append(f"outcome {was.get('outcome')} → {now.get('outcome')}")
            if was.get("n_sources") != now.get("n_sources"):
                diffs.append(f"zdrojů {was.get('n_sources')} → {now.get('n_sources')}")
            if (was.get("sources") or [None])[:1] != (now.get("sources") or [None])[:1]:
                diffs.append("jiný 1. zdroj")
            # a moved SCOPE is a different question, not a regression — it has to
            # be named separately or it reads as a ranking change
            if (was.get("domains_effective") or None) != (now.get("domains_effective") or None):
                diffs.append(f"scope {was.get('domains_effective')} → "
                             f"{now.get('domains_effective')}")
            if diffs:
                moved += 1
                _flap = ""
                if now.get("unstable") or was.get("unstable"):
                    _flap = (f"  [kolísá: teď {now.get('unstable') or 0},"
                             f" dřív {was.get('unstable') or 0}]")
                print(f"  {qid} «{now['question'][:38]}»: " + "; ".join(diffs) + _flap)
        print(f"  {moved} z {len(results)} případů se změnilo"
              + (" — nic se nepohnulo" if not moved else ""))


if __name__ == "__main__":
    main()
