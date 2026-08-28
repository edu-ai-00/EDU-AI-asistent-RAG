#!/usr/bin/env python3
# last change in code by Martin Safranek (2026-08-20).
"""Run eval/doc_questions.json through the cascade and score QA↔document interplay.

The 30-case suite answers "does the cascade work". This one answers a different
question: for 170 questions derived FROM the corpus — where every question is known
to be answerable from ONE specific chunk of ONE specific document — does the search
actually land there, and does the QA branch help or hijack?

Three things are scored per case, and they are deliberately separate:

  doc_hit    the expected DOCUMENT contributed a chunk to the pool
  chunk_hit  the expected CHUNK itself did
  branch     which branch answered (qa_* = curated, rag = generated, none)

`doc_hit` without `chunk_hit` is the interesting failure: the right document was
found and the wrong passage answered. A pass/fail count hides exactly that.

A QA answer to a document-derived question is NOT scored as a failure on its own —
a curated entry covering the same ground is a legitimate better answer. It IS
reported separately, because a QA branch that answers questions whose ground truth
lives in a document is how a stale pack silently replaces the corpus.

    python3 scripts/run_doc_questions.py                     # all
    python3 scripts/run_doc_questions.py --limit 20           # smoke
    python3 scripts/run_doc_questions.py --entity SKOLA_NOVA_VES_ZS
    python3 scripts/run_doc_questions.py --out eval/doc_run.json
"""
from __future__ import annotations
import argparse, collections, json, subprocess, sys, time, urllib.request

URL = "http://127.0.0.1:8090/v1/ask"
PG = ["docker", "exec", "-i", "edu_ai_postgres", "psql", "-U", "edu_ai", "-d", "edu_ai", "-tA"]

# a staff user per tenant, so the caller is verified rather than anonymous
UID = {"SKOLA_NOVA_VES_ZS": "066d9206-cfe2-4f60-9f02-79bfea57c2bb",
       "SKOLA_NOVA_VES_SOS": "3cd81a92-957e-4208-90aa-dcf0f7207286",
       "SKOLA_NOVA_VES": "422dae54-27f6-4625-87a8-c62a61e911e1",
       "ZAKLADNI_SKOLA_LIPOVA": "be164723-a616-4a8b-a900-c9c555cb8326"}


def roles_of(chunk_ids: set[int]) -> dict[int, list[str]]:
    """chunk id → audience_roles, for the whole run in one query.

    The role is a HARD filter at query time, so a caller role the expected chunk
    does not admit makes the case unanswerable BY DESIGN and the run measures the
    harness, not the search. Found the hard way: the first full run scored 0/5 on
    three documents, all EDUin policy releases tagged {PARENT,TEACHER} or {PARENT},
    because every question was asked as STUDENT.
    """
    if not chunk_ids:
        return {}
    ids = ",".join(str(int(i)) for i in chunk_ids)
    out = subprocess.run(
        PG + ["-c", f"SELECT id, array_to_string(audience_roles, ',') "
                    f"FROM edu_ai.document_chunks WHERE id IN ({ids})"],
        capture_output=True, text=True)
    if out.returncode:
        return {}
    m = {}
    for line in out.stdout.strip().splitlines():
        if "|" in line:
            a, b = line.split("|", 1)
            m[int(a)] = [x for x in b.split(",") if x]
    return m


def pick_role(admitted: list[str] | None) -> str:
    """A role the expected chunk admits. STUDENT first — it is the strictest and
    the primary audience — then PARENT, then TEACHER."""
    adm = admitted or []
    for r in ("STUDENT", "PARENT", "TEACHER"):
        if r in adm:
            return r
    return "STUDENT"


def ask(case: dict) -> dict:
    body = {"question": case["question"], "legal_entity": case["legal_entity"],
            "user_role": case.get("user_role") or "STUDENT", "debug": True}
    if UID.get(case["legal_entity"]):
        body["user_id"] = UID[case["legal_entity"]]
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            return json.load(r)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def chunk_docs(chunk_ids: set[int]) -> dict[int, str]:
    """chunk id → file_uuid, resolved in ONE query for the whole run.

    Sources in the response are titles, not ids, and a title is not a key — two
    revisions of one page share it. The pool's chunk ids are the only unambiguous
    way to say "this document contributed".
    """
    if not chunk_ids:
        return {}
    ids = ",".join(str(int(i)) for i in chunk_ids)
    out = subprocess.run(
        PG + ["-c", f"SELECT id, file_uuid::text FROM edu_ai.document_chunks "
                    f"WHERE id IN ({ids})"], capture_output=True, text=True)
    if out.returncode:
        print(f"  ! chunk→dokument mapping failed: {out.stderr.strip()[:200]}",
              file=sys.stderr)
        return {}
    m = {}
    for line in out.stdout.strip().splitlines():
        if "|" in line:
            a, b = line.split("|", 1)
            m[int(a)] = b
    return m


def qa_entry_docs(entry_ids: set[str]) -> dict[str, str]:
    """qa_entries.entry_id → file_uuid of the document the entry was derived from.

    Without it a QA answer is unattributable: the pool is EMPTY when the QA branch
    resolves early, so doc_hit/chunk_hit go false and the run cannot tell "QA
    answered from this document's own entry" from "QA answered from somewhere
    else". Generated entries always carry file_uuid (design rule 3); a hand-written
    pack has none, and comes back as None.
    """
    ids = [i for i in entry_ids if i]
    if not ids:
        return {}
    lst = ",".join("'" + i.replace("'", "") + "'" for i in ids)
    out = subprocess.run(
        PG + ["-c", f"SELECT entry_id::text, coalesce(file_uuid::text,'') "
                    f"FROM edu_ai.qa_entries WHERE entry_id::text IN ({lst})"],
        capture_output=True, text=True)
    if out.returncode:
        return {}
    m = {}
    for line in out.stdout.strip().splitlines():
        if "|" in line:
            a, b = line.split("|", 1)
            m[a] = b or None
    return m


def pool_ids(resp: dict) -> list[int]:
    ids = []
    for st in (resp.get("trace") or []):
        for i in (st.get("added_chunk_ids") or []):
            if i not in ids:
                ids.append(int(i))
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="eval/doc_questions.json")
    ap.add_argument("--out", default="eval/doc_run.json")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--entity")
    a = ap.parse_args()

    cases = json.load(open(a.suite, encoding="utf-8"))["cases"]
    # resolve the caller role BEFORE asking: it is a hard filter, so asking with a
    # role the target chunk excludes measures nothing
    _roles = roles_of({c["expected_chunk_id"] for c in cases})
    for c in cases:
        c["chunk_roles"] = _roles.get(c["expected_chunk_id"]) or []
        c["user_role"] = pick_role(c["chunk_roles"])
    if a.entity:
        cases = [c for c in cases if c["legal_entity"] == a.entity]
    if a.limit:
        cases = cases[:a.limit]
    _rc = collections.Counter(c["user_role"] for c in cases)
    print(f"  případů: {len(cases)}   role volajícího: {dict(_rc)}\n")

    results = []
    for i, c in enumerate(cases, 1):
        t0 = time.time()
        r = ask(c)
        ms = int((time.time() - t0) * 1000)
        if "_error" in r:
            results.append({**c, "outcome": "ERROR", "branch": "error",
                            "error": r["_error"], "ms": ms, "pool": []})
            print(f"  {i:>3}/{len(cases)} {c['id']:<12} ERROR {r['_error'][:50]}")
            continue
        pool = pool_ids(r)
        src = r.get("answer_source") or "none"
        results.append({**c, "outcome": r.get("outcome"), "branch": src,
                        "pool": pool, "ms": ms,
                        "answer_head": (r.get("answer") or "")[:120],
                        "matched_entry_id": r.get("matched_entry_id")})
        print(f"  {i:>3}/{len(cases)} {c['id']:<12} {str(r.get('outcome')):<10}"
              f" {src:<12} pool={len(pool):<4} {ms:>6} ms")

    # one mapping query for every chunk any case saw
    m = chunk_docs({i for x in results for i in x["pool"]})
    qd = qa_entry_docs({x.get("matched_entry_id") for x in results})
    for x in results:
        docs = {m.get(i) for i in x["pool"]}
        x["doc_hit"] = x["expected_file_uuid"] in docs
        x["chunk_hit"] = x["expected_chunk_id"] in x["pool"]
        # for a QA answer: did the answering ENTRY come from the expected document?
        x["qa_entry_doc"] = qd.get(x.get("matched_entry_id") or "")
        x["qa_from_expected_doc"] = (
            x["qa_entry_doc"] == x["expected_file_uuid"]
            if str(x["branch"]).startswith("qa") else None)

    json.dump({"suite": a.suite, "cases": results},
              open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n = len(results)
    def pct(k):
        return f"{k}/{n} ({100.0*k/n:.0f} %)" if n else "—"
    doc = sum(1 for x in results if x["doc_hit"])
    ch = sum(1 for x in results if x["chunk_hit"])
    ans = sum(1 for x in results if x["outcome"] == "answer")
    qa = sum(1 for x in results if str(x["branch"]).startswith("qa"))
    rag = sum(1 for x in results if x["branch"] == "rag")
    none = sum(1 for x in results if x["branch"] in ("none", "error"))
    print(f"\n  ── SOUHRN ──")
    print(f"  odpovězeno            {pct(ans)}")
    print(f"  správný dokument      {pct(doc)}")
    print(f"  správná část          {pct(ch)}")
    print(f"  dokument OK, část NE  {pct(doc - ch)}   ← nejzajímavější selhání")
    print(f"  odpověděla QA větev   {pct(qa)}")
    print(f"  odpověděly dokumenty  {pct(rag)}")
    print(f"  bez odpovědi / chyba  {pct(none)}")
    if qa:
        _qok = sum(1 for x in results if x.get("qa_from_expected_doc") is True)
        _qno = sum(1 for x in results if x.get("qa_from_expected_doc") is False)
        print(f"\n  z {qa} QA odpovědí:")
        print(f"    z položky ODVOZENÉ Z OČEKÁVANÉHO dokumentu   {_qok}")
        print(f"    z položky z JINÉHO dokumentu                {_qno}   ← k prověření")
    print(f"\n  → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
