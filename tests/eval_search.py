# last change in code by Jan Drozd and Martin Safranek (2026-08-13).
"""Run a question set through /search and report the A/B/C outcome distribution.

    python3 tests/eval_search.py [questions_file]

Default file: tests/eval_prijimacky_questions.txt. Hits the search service on
loopback (bypasses nginx auth). Maps matched_entry_id → canonical_q via psql so
you can eyeball whether a committed 'answer' is topically right.
"""
import sys
import os
import subprocess
import requests

SEARCH = "http://127.0.0.1:8090/v1/ask"
QFILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__),
                                                           "eval_prijimacky_questions.txt")

# filename → source email subject (logged as session_id so question_answer_log rows are
# attributable to the email they came from)
SUBJECTS = {
    "eval_prijimacky_questions.txt": "RAG EDU-AI - otázky přijímačky",
    "eval_ucitele_questions.txt":    "RAG EDU-AI - provozní učitelé",
    "eval_cjm_questions.txt":        "RAG EDU-AI - otázky ČJ/M",
}
SUBJECT = SUBJECTS.get(os.path.basename(QFILE), os.path.basename(QFILE))


def load_questions(path):
    out = []
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def entry_map():
    r = subprocess.run(
        ["docker", "exec", "-i", "edu_ai_postgres", "psql", "-U", "edu_ai", "-d", "edu_ai",
         "-tA", "-F", "\t", "-c",
         "SELECT entry_id, canonical_q FROM edu_ai.qa_entries WHERE is_active"],
        capture_output=True, text=True)
    m = {}
    for line in r.stdout.splitlines():
        if "\t" in line:
            eid, q = line.split("\t", 1)
            m[eid] = q
    return m


def main():
    qs = load_questions(QFILE)
    canon = entry_map()
    rows = []
    for q in qs:
        try:
            r = requests.post(SEARCH, json={"question": q, "session_id": SUBJECT}, timeout=60)
            d = r.json()
            rows.append((q, d.get("outcome"), d.get("answer_source"),
                         d.get("match_score"), d.get("matched_entry_id"), r.status_code))
        except Exception as e:  # noqa: BLE001
            rows.append((q, "ERROR", str(e)[:30], None, None, None))

    # per-question
    print(f"\n{'#':>3} {'outcome':9} {'source':10} {'score':6}  question → matched entry")
    for i, (q, out, src, sc, mid, code) in enumerate(rows, 1):
        matched = canon.get((mid or "")[:36], "") if mid else ""
        sc_s = f"{sc:.2f}" if isinstance(sc, (int, float)) else "-"
        line = f"{i:>3} {str(out):9} {str(src):10} {sc_s:6}  {q[:44]}"
        if out == "answer" and matched:
            line += f"  → «{matched[:40]}»"
        print(line)

    # distribution
    from collections import Counter
    oc = Counter(r[1] for r in rows)
    sc = Counter(r[2] for r in rows if r[1] == "answer")
    n = len(rows)
    print(f"\n==== {n} otázek — rozložení outcome ====")
    for k in ("answer", "suggest", "no_answer", "blocked", "self_harm", "unusable", "ERROR"):
        if oc.get(k):
            print(f"  {k:10} {oc[k]:3}  ({100*oc[k]//n} %)")
    print("  --- z toho 'answer' podle zdroje ---")
    for k, v in sc.most_common():
        print(f"  {k:12} {v}")

    # committed curated answers — review for false confidence
    print("\n==== 'answer' (kurátorská odpověď — zkontrolovat relevanci) ====")
    for q, out, src, s, mid, code in rows:
        if out == "answer":
            matched = canon.get((mid or "")[:36], "?")
            print(f"  • [{src}] «{q[:46]}»\n      → «{matched[:60]}»")


if __name__ == "__main__":
    main()
