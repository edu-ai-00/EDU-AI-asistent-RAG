"""QA ingest status aggregation — the QAFinalResult component (Langflow flow
JSON) and the gateway's ok/error mapping (api/app/main.py) both agree on when
a publish is genuinely a partial failure.

Root cause this guards: on 2026-09-12 a real publish of pack 'faq-provoz' was
reported FAILED by the gateway while the answer text was written correctly.
Investigation (DB rows in edu_ai.qa_embedding + the dev bifrost proxy having
zero configured providers, per /api/providers) showed the vectors stage
genuinely never completed — PARTIAL_FAILURE was the CORRECT report. The one
real code defect found along the way: QAFinalResult defaulted a genuinely
MISSING stage row (dict key absent, not merely empty) to "ok" instead of
treating it as a failure signal — see _stage_status's `by_stage.get(stage)`
(no default) vs the old `by_stage.get(stage, "")` this replaced.

This file does NOT spin up Docker or Langflow — it extracts and executes the
real QAFinalResult class straight out of langflow_flows/ingestion/EDU_AI_QA.json
(same technique as tests/test_qa_entry_domains.py's `component()` helper) and
imports the real api/app/main.py functions. Standard library + pandas only.

    python3 -m pytest tests/test_qa_ingest_status.py -v
"""
import ast
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

# ── api/app/main.py — import the real gateway functions ──────────────────
sys.path.insert(0, str(ROOT / "api"))
from app.main import _extract_pipeline_status, _gateway_status_for_pipeline  # noqa: E402

# ── QAFinalResult — extracted from the real flow JSON, not re-typed ──────
_FLOW = json.loads((ROOT / "langflow_flows/ingestion/EDU_AI_QA.json").read_text(encoding="utf-8"))


def _component(node_id: str, class_name: str):
    """Exec the component's shipped code (minus Langflow's own UI wiring) and
    return an instance — the same extraction tests/test_qa_entry_domains.py
    uses, so this never re-implements the aggregation logic under test."""
    source = next(
        n["data"]["node"]["template"]["code"]["value"]
        for n in _FLOW["data"]["nodes"] if n["id"] == node_id
    )
    tree = ast.parse(source)
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and (n.module or "").startswith("lfx"))]
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            n.bases = []
            n.body = [x for x in n.body if not (isinstance(x, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id in ("inputs", "outputs")
                              for t in x.targets))]
    ns = {"DataFrame": pd.DataFrame}
    exec(compile(ast.fix_missing_locations(tree), node_id, "exec"), ns)
    return ns[class_name]()


def _qa_final_result(rows: list[dict]) -> dict:
    """Run the real QAFinalResult.process() over accumulated status rows and
    return the parsed pipeline_status dict (what ChatOutput would surface)."""
    comp = _component("QAFinalResult-qa030-QA", "QAFinalResult")
    comp.dataframe = pd.DataFrame(rows) if rows else pd.DataFrame()
    out_df = comp.process()
    record = out_df.data.iloc[0] if hasattr(out_df, "data") else out_df.iloc[0]
    return json.loads(record["pipeline_status"])


def _chat_output_response(pipeline_status: dict) -> dict:
    """A minimal Langflow /run response shaped the way _extract_pipeline_status
    parses it: walk() looks for a "text" field holding the ChatOutput message,
    which is the json.dumps'd summary containing the literal 'json_parse' key
    (real QAFinalResult always emits it) so the bracket-matcher picks it up."""
    text = json.dumps(pipeline_status, ensure_ascii=False)
    return {
        "session_id": "file-uuid-under-test",
        "outputs": [{"outputs": [{"results": {"message": {"text": text}}}]}],
    }


PROC = "e96195c1-3d2c-4c04-ae60-42f0348f571f"


# ── A: every stage genuinely succeeded → OK, gateway counts it ok ────────

def test_all_stages_ok_reports_ok_end_to_end():
    rows = [
        {"stage": "qa_tables", "processing_uuid": PROC,
         "result": "pack 'faq-provoz' v3 [merge]: added 0, updated 1, variants 3"},
        {"stage": "vectors", "processing_uuid": PROC,
         "result": "SUCCESS — embedded 3 variants with text-embedding-3-small into qa_embedding"},
    ]
    pipeline_status = _qa_final_result(rows)
    assert pipeline_status["status"] == "OK"
    assert pipeline_status["stages"] == {
        "json_parse": "ok", "llm": "ok", "qa_tables": "ok", "vectors": "ok",
    }
    assert pipeline_status["errors"] is None

    # same shape api/app/main.py:875 (now _gateway_status_for_pipeline) consumes
    extracted = _extract_pipeline_status(_chat_output_response(pipeline_status))
    assert extracted == pipeline_status
    assert _gateway_status_for_pipeline(extracted) == "ok"


def test_nothing_to_embed_is_still_ok():
    """A vectors run that found no pending variants is success, not silence."""
    rows = [
        {"stage": "qa_tables", "processing_uuid": PROC, "result": "pack v1: added 2"},
        {"stage": "vectors", "processing_uuid": PROC,
         "result": "nothing to embed (all active variants already embedded)"},
    ]
    pipeline_status = _qa_final_result(rows)
    assert pipeline_status["status"] == "OK"
    extracted = _extract_pipeline_status(_chat_output_response(pipeline_status))
    assert _gateway_status_for_pipeline(extracted) == "ok"


# ── B: a genuinely failed/skipped/missing vectors stage stays PARTIAL_FAILURE
#      — the guard on "don't report success because the answer was written" ─

def test_vectors_error_is_partial_failure_end_to_end():
    """This is the exact shape of the 2026-09-12 faq-provoz run: qa_tables
    succeeded (the merge line logged fine) but vectors genuinely errored."""
    rows = [
        {"stage": "qa_tables", "processing_uuid": PROC,
         "result": "pack 'faq-provoz' v3 [merge]: added 0, updated 1, variants 3"},
        {"stage": "vectors", "processing_uuid": PROC,
         "result": "ERROR: BadRequestError: could not auto resolve a provider for the request"},
    ]
    pipeline_status = _qa_final_result(rows)
    assert pipeline_status["status"] == "PARTIAL_FAILURE"
    assert pipeline_status["stages"]["vectors"] == "error"
    assert pipeline_status["stages"]["qa_tables"] == "ok"
    assert pipeline_status["errors"] == ["vectors: ERROR: BadRequestError: could not auto resolve a provider for the request"]

    extracted = _extract_pipeline_status(_chat_output_response(pipeline_status))
    assert _gateway_status_for_pipeline(extracted) == "error"


def test_vectors_skipped_upstream_error_is_partial_failure():
    rows = [
        {"stage": "qa_tables", "processing_uuid": PROC, "result": "ERROR: connection refused"},
        {"stage": "vectors", "processing_uuid": PROC,
         "result": "SKIPPED — upstream qa_tables error, embedding not run"},
    ]
    pipeline_status = _qa_final_result(rows)
    assert pipeline_status["status"] == "PARTIAL_FAILURE"
    assert pipeline_status["stages"]["qa_tables"] == "error"
    assert pipeline_status["stages"]["vectors"] == "skipped"
    extracted = _extract_pipeline_status(_chat_output_response(pipeline_status))
    assert _gateway_status_for_pipeline(extracted) == "error"


def test_missing_vectors_row_is_not_silently_ok():
    """The regression this file exists for: if the vectors stage row never
    reaches QAFinalResult at all (dropped/misrouted upstream — a real
    candidate raised in the original incident), that must NOT default to
    "ok". Before the fix, `by_stage.get("vectors", "")` fed `_stage_status`
    an empty string indistinguishable from "no failure text", so an entirely
    missing row silently reported OK. The fix reports a distinct "missing"
    status and still forces PARTIAL_FAILURE."""
    rows = [
        {"stage": "qa_tables", "processing_uuid": PROC,
         "result": "pack 'faq-provoz' v3 [merge]: added 0, updated 1, variants 3"},
        # no "vectors" row at all
    ]
    pipeline_status = _qa_final_result(rows)
    assert pipeline_status["stages"]["vectors"] == "missing"
    assert pipeline_status["status"] == "PARTIAL_FAILURE"
    assert pipeline_status["detail"]["vectors"] == "(no status row)"

    extracted = _extract_pipeline_status(_chat_output_response(pipeline_status))
    assert _gateway_status_for_pipeline(extracted) == "error"


def test_document_flow_has_no_pipeline_status_and_reports_ok():
    """Document flows never run QAFinalResult — _extract_pipeline_status
    correctly returns None for their /run response, and the gateway must
    still read that as ok (None is not PARTIAL_FAILURE)."""
    response = {"outputs": [{"outputs": [{"results": {"message": {"text": "some markdown chunk text"}}}]}]}
    extracted = _extract_pipeline_status(response)
    assert extracted is None
    assert _gateway_status_for_pipeline(extracted) == "ok"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
