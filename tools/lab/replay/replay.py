"""Replay logged questions through the search cascade, without generation.

Runs INSIDE a search image (any version) against a database that holds the
data the questions were asked on, and writes one JSON line per question: what
the cascade decided BEFORE the answer step — the curated entry it answered
with, or the document chunks it gathered. Two runs (two code versions, same
data) are then compared by compare.py.

Nothing is written to the database: the endpoint's own preparation
(server._prepare_search) and executor.run_search(stop_before="answer") are
called directly, so neither the query log nor the idempotency table is touched.
The language model and the embedder raise at once (no key, no network): the
steps that need them fall through exactly as they do without a key, instead of
waiting on retries.

Follow-up questions are replayed WITHOUT their conversation (no
conversation_id): rewriting them needs the model.

  python replay.py --out /out/new.jsonl [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from fastapi import HTTPException

from app import llm
from app.db import get_cursor


def _no_model(*a, **k):
    raise RuntimeError("replay: no language model")


def _patch_models():
    import app.nodes.qa_vector as qv
    import app.nodes.rag_vector as rv
    import app.nodes.resolve_context as rc
    import app.nodes.answer as an
    import app.server as srv
    for mod in (llm, qv, rv, rc, an, srv):
        for name in ("get_embedder", "get_chat"):
            if hasattr(mod, name):
                setattr(mod, name, _no_model)


def _rows(limit: int | None):
    with get_cursor() as cur:
        cur.execute(
            "SELECT id, raw_query, legal_entity_code, user_id, user_role, user_school_grade,"
            "       domains, answer_source, matched_entry_id"
            "  FROM edu_ai.question_answer_log"
            " WHERE coalesce(raw_query, '') <> '' AND legal_entity_code IS NOT NULL"
            " ORDER BY id" + (f" LIMIT {int(limit)}" if limit else ""))
        return cur.fetchall()


def _domains(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = [x for x in v.strip("{}").split(",") if x]
    return [str(x) for x in v] or None


def _ask(server, row, user_id):
    rid, q, entity, _uid, role, grade, domains, _src, _eid = row
    req = server.SearchRequest(question=q[:2000], legal_entity=entity, user_id=user_id,
                               user_role=role, user_school_grade=grade,
                               domains=_domains(domains))
    prep = server._prepare_search(req)
    if prep["_role_err"] is not None:
        return {"stage": "role_required"}
    from app.executor import run_search
    state, _setting = run_search(prep["initial"], stop_before="answer")
    chunks = state.get("rag_chunks") or []
    return {
        "stage": "answered" if state.get("resolved") else "before_answer",
        "answer_source": state.get("answer_source"),
        "outcome": state.get("outcome"),
        "entry": state.get("matched_entry_id"),
        "score": state.get("match_score"),
        "suggestions": [s.get("entry_id") for s in (state.get("suggestions") or [])
                        if s.get("entry_id")],
        "chunks": [c.get("chunk_id") for c in chunks][:10],
        "lemma": state.get("query_lemma_tokens"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)
    import app.server as server
    _patch_models()
    rows = _rows(a.limit or None)
    t0 = time.time()
    with open(a.out, "w", encoding="utf-8") as fh:
        for n, row in enumerate(rows, 1):
            rec = {"id": row[0], "prod_source": row[7], "prod_entry": str(row[8]) if row[8] else None}
            try:
                try:
                    rec.update(_ask(server, row, row[3]))
                except HTTPException as e:
                    if e.status_code == 403 and row[3]:   # the user id is unknown here
                        rec.update(_ask(server, row, None))
                        rec["anonymous"] = True
                    else:
                        raise
            except HTTPException as e:
                rec.update({"stage": f"http_{e.status_code}"})
            except Exception as e:                        # one bad row must not stop the run
                rec.update({"stage": "error", "error": type(e).__name__})
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            if n % 250 == 0:
                print(f"[replay] {n}/{len(rows)} ({time.time() - t0:.0f} s)", file=sys.stderr, flush=True)
    print(f"[replay] {len(rows)} questions in {time.time() - t0:.0f} s -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
