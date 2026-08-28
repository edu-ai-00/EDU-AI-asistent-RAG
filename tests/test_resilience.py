# last change in code by Martin Safranek (2026-08-02).
"""Resilience / fault-injection tests for the edu_ai stack.

Runs against the LIVE dev stack on loopback. Each test injects a fault, asserts
graceful behaviour, and RESTORES the stack in a finally block. Run:

    python3 tests/test_resilience.py            # all
    python3 tests/test_resilience.py r4 r8      # selected

No pytest needed (built-in runner below); the test_* functions are also
pytest-compatible. requests must be importable on the host.

Covered (first set):
  R1  corrupted / unparseable PDF upload
  R4  LLM (OpenAI) unavailable → search must degrade, not crash
  R8  database unavailable → handled error + recovery, no hang
  ATOM  QA versioning is atomic (failed insert rolls back the deactivation)
"""

import os
import sys
import subprocess
import tempfile

import requests

sys.path.insert(0, os.path.dirname(__file__))
from _util import (  # noqa: E402
    PG, search, ingest_file, db_count, dcompose, docker,
    wait_search_healthy, wait_db_ready,
)

LLM_OVERRIDE = "tests/llmfault.override.yml"


# ── R1 — corrupted PDF ────────────────────────────────────────
def test_r1_corrupted_pdf():
    """A garbage .pdf must not crash the API nor leave partial chunk rows."""
    before = db_count("edu_ai.document_chunks")
    p = tempfile.mktemp(suffix=".pdf")
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 this is not a valid pdf body " + os.urandom(2048))
    try:
        r = ingest_file(p, "corrupted.pdf", timeout=300,
                        document_type="pdf", document_language="cs")
    finally:
        os.remove(p)

    assert r.status_code == 200, f"unhandled HTTP {r.status_code} on corrupted PDF"
    data = r.json()                                   # must be well-formed JSON
    assert "files" in data, f"malformed response: {list(data)[:6]}"
    after = db_count("edu_ai.document_chunks")
    assert after == before, f"corrupted PDF created {after - before} chunk rows (want 0)"
    print(f"    R1: HTTP 200, status={data.get('status')}, chunk delta={after - before}")


# ── R4 — LLM / OpenAI down ────────────────────────────────────
def test_r4_llm_down_search_degrades():
    """With a broken OpenAI key: deterministic QA tiers still answer; the
    vector/RAG (LLM) path degrades to a handled response — never a 500/hang."""
    try:
        dcompose("up", "-d", "--force-recreate", "edu_ai_search", extra_files=(LLM_OVERRIDE,))
        assert wait_search_healthy(60), "search unhealthy after LLM-fault inject"

        r1 = search("Kdy jsou přijímačky?", timeout=30)
        assert r1.status_code == 200, f"QA query 5xx under LLM fault: {r1.status_code}"
        src = r1.json().get("answer_source")
        assert src in ("qa_exact", "qa_lexical"), \
            f"QA tier should answer without OpenAI, got source={src}"

        r2 = search("What is the capital of France?", timeout=60)
        assert r2.status_code == 200, f"RAG query crashed under LLM fault: {r2.status_code}"
        out = r2.json().get("outcome")
        assert out in ("no_answer", "suggest", "blocked", "unusable"), \
            f"RAG path should degrade gracefully, got outcome={out}"
        print(f"    R4: QA tier still answers (src={src}); RAG-only degraded to '{out}'")
    finally:
        dcompose("up", "-d", "--force-recreate", "edu_ai_search")   # restore real key
        wait_search_healthy(60)


# ── R8 — database down ────────────────────────────────────────
def test_r8_db_down_then_recover():
    """DB down: search returns a bounded, handled response (no hang); recovers."""
    hung = False
    try:
        docker("stop", PG)
        try:
            r = search("Kdy jsou přijímačky?", timeout=20)
            code, body = r.status_code, r.text[:120]
        except requests.Timeout:
            hung = True
            code, body = None, ""
        assert not hung, "search HUNG with DB down (no bounded failure)"
        assert code in (200, 500, 502, 503), f"unexpected {code} with DB down"
        print(f"    R8: DB down → HTTP {code} (handled, no hang) :: {body!r}")
    finally:
        docker("start", PG)
        assert wait_db_ready(60), "postgres did not come back up"
        wait_search_healthy(40)

    r = search("Kdy jsou přijímačky?", timeout=30)
    assert r.status_code == 200, f"search did not recover after DB restart: {r.status_code}"
    print("    R8: recovered after DB restart (HTTP 200)")


# ── ATOM — QA versioning atomicity ────────────────────────────
def test_atom_qa_versioning_rollback():
    """PostgresInsertQA deactivates all active QA then inserts the new pack in ONE
    transaction. If the insert fails, the deactivation MUST roll back — otherwise
    a failed upload leaves the corpus 'all-inactive, nothing-active'. Verify the
    DB guarantee it relies on."""
    before = db_count("edu_ai.qa_entries", "WHERE is_active")
    assert before > 0, "precondition: expected some active QA entries"

    sql = ("BEGIN;"
           "UPDATE edu_ai.qa_entries SET is_active=false WHERE is_active;"
           # canonical_q is NOT NULL → this INSERT aborts the transaction
           "INSERT INTO edu_ai.qa_entries(entry_id,pack_id,canonical_q,answer_md)"
           " VALUES (gen_random_uuid(),'atomtest',NULL,'a');"
           "COMMIT;")
    subprocess.run(["docker", "exec", "-i", PG, "psql", "-U", "edu_ai", "-d", "edu_ai", "-c", sql],
                   capture_output=True, text=True)

    after = db_count("edu_ai.qa_entries", "WHERE is_active")
    assert after == before, \
        f"ATOMICITY BROKEN: active {before}→{after} (deactivation not rolled back)"
    print(f"    ATOM: forced insert failure rolled back; active stayed {before}")


# ── runner (no pytest needed) ─────────────────────────────────
TESTS = {
    "r1": test_r1_corrupted_pdf,
    "r4": test_r4_llm_down_search_degrades,
    "r8": test_r8_db_down_then_recover,
    "atom": test_atom_qa_versioning_rollback,
}


def main(argv):
    picked = [a.lower() for a in argv] or list(TESTS)
    results = []
    for key in picked:
        fn = TESTS.get(key)
        if not fn:
            print(f"?? unknown test '{key}' (have: {', '.join(TESTS)})")
            continue
        print(f"\n▶ {key}: {fn.__doc__.splitlines()[0]}")
        try:
            fn()
            print(f"✅ {key} PASS")
            results.append((key, True))
        except AssertionError as e:
            print(f"❌ {key} FAIL: {e}")
            results.append((key, False))
        except Exception as e:  # noqa: BLE001
            print(f"💥 {key} ERROR: {type(e).__name__}: {e}")
            results.append((key, False))
    ok = sum(1 for _, p in results if p)
    print(f"\n==== {ok}/{len(results)} passed ====")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
