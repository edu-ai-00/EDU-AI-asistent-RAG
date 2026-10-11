"""N2 regression: republishing a QA CSV must leave qa_variants matching the
file exactly — no growth on an identical re-publish, and a removed variant
must go inactive, not linger. Extracts the real QAJsonReader / QAVariantExploder
/ ChunkLemmatizer / PostgresInsertQA classes straight out of the flow JSON
(same technique as tests/test_qa_entry_domains.py's `component()` helper) and
runs them against an isolated schema in a real Postgres — never against a
re-typed copy of the writer's logic.

Needs a live Postgres reachable at POSTGRES_DB_SERVER/POSTGRES_SERVER_PORT
with the edu_ai schema already migrated (qa_* tables, code_list, settings,
resolved_code_list/resolved_taxonomy_rules/entity_grade_scope). On a bare
checkout, or any host without that stack, the module-level fixture cannot
connect and every test here is skipped — the rest of the suite still runs.

    python3 -m pytest tests/test_qa_writer_variants.py -v
"""
import ast
import csv
import io
import json
import os
import uuid
from pathlib import Path

import pandas as pd
import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="no psycopg2 on this interpreter — writer DB tests skipped")
from psycopg2 import sql  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_FLOW = json.loads((ROOT / "langflow_flows/ingestion/EDU_AI_QA.json").read_text(encoding="utf-8"))


def _component(node_id: str, class_name: str):
    source = next(n["data"]["node"]["template"]["code"]["value"]
                  for n in _FLOW["data"]["nodes"] if n["id"] == node_id)
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


def _db_config():
    return dict(host=os.getenv("POSTGRES_DB_SERVER", "edu_ai_postgres"),
                port=os.getenv("POSTGRES_SERVER_PORT", "5432"),
                user=os.getenv("POSTGRES_DB_USER", "edu_ai"),
                password=os.environ.get("POSTGRES_PASSWORD"),
                dbname=os.getenv("POSTGRES_DB", "edu_ai"))


@pytest.fixture(scope="module")
def rig():
    """One isolated schema (LIKE ... INCLUDING ALL of the real qa_* tables,
    views onto the shared code_list/settings/legal_entity/taxonomy), the
    extracted components wired to it, and a small ingest() helper. Skips
    the whole module if Postgres is unreachable."""
    cfg = _db_config()
    try:
        db = psycopg2.connect(connect_timeout=3, **cfg)
    except Exception as e:
        pytest.skip(f"no live Postgres reachable for the writer test rig ({e})")
        return
    db.autocommit = True
    cur = db.cursor()
    schema = "test_qa_variants_" + uuid.uuid4().hex
    cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        for table in ("qa_entries", "qa_variants", "qa_embedding", "qa_pack_manifest", "ingestion_log"):
            cur.execute(sql.SQL("CREATE TABLE {}.{} (LIKE edu_ai.{} INCLUDING ALL)").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
        for table in ("settings", "code_list", "legal_entity", "taxonomy_binding", "taxonomy_rules"):
            cur.execute(sql.SQL("CREATE VIEW {}.{} AS SELECT * FROM edu_ai.{}").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
        for fn in ("entity_grade_scope(text)", "resolved_code_list(text,text)",
                   "resolved_taxonomy_rules(text,text)"):
            cur.execute("SELECT pg_get_functiondef(%s::regprocedure)", ("edu_ai." + fn,))
            cur.execute(cur.fetchone()[0].replace("FUNCTION edu_ai.", f"FUNCTION {schema}.", 1))
        cur.execute("SELECT legal_entity_code FROM edu_ai.legal_entity WHERE is_active "
                    "ORDER BY legal_entity_code LIMIT 1")
        entity = cur.fetchone()[0]
        cur.execute("SELECT code FROM edu_ai.code_list WHERE list_code='language' AND is_active LIMIT 1")
        lang = cur.fetchone()[0]

        reader = _component("QAJsonReader-qa001-QA", "QAJsonReader")
        reader.file_path_override = ""
        reader._language_registry = lambda: {"active": [lang], "fallback": lang}
        exploder = _component("QAVariantExploder-qa002-QA", "QAVariantExploder")
        exploder.variants_column = "variants_json"
        exploder.question_column = "canonical_q"
        exploder.max_variants = 3
        lemmatizer = _component("ChunkLemmatizer-qa003-QA", "ChunkLemmatizer")
        writer = _component("PostgresInsertQA-qa004-QA", "PostgresInsertQA")
        for attr, key in [("pg_host", "host"), ("pg_port", "port"), ("pg_user", "user"),
                          ("pg_password", "password"), ("pg_database", "dbname")]:
            setattr(writer, attr, cfg[key])
            setattr(lemmatizer, attr, cfg[key])
        writer.pg_schema = schema
        lemmatizer.pg_schema = "edu_ai"
        lemmatizer.context = "ingest_qa"
        lemmatizer.text_column = "phrasing"
        lemmatizer.norm_column, lemmatizer.lemma_column, lemmatizer.stem_column = (
            "norm_text", "lemma_text", "stem_text")

        def ingest(rows, qa_mode="merge"):
            """rows: list of (entry_key, curated_variants_csv_cell, want_active)."""
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["entry_key", "canonical_q", "answer_md", "variants", "is_active"])
            for key, variants, active in rows:
                w.writerow([key, "Question " + key, "Answer " + key, variants, str(active)])
            reader.input_df = pd.DataFrame([dict(
                file_content_utf8=buf.getvalue(), user_defined_pack_id="test-variants",
                legal_entity_code=entity, document_language=lang, qa_mode=qa_mode,
                visibility="entity", audience_roles='["PARENT"]')])
            exploder.dataframe = reader.process()
            lemmatizer.dataframe = exploder.process()
            writer.dataframe = lemmatizer.process()
            return writer.process().iloc[0]["result"]

        def active_variants(entry_key):
            cur.execute(f"SELECT v.phrasing FROM {schema}.qa_variants v "
                        f"JOIN {schema}.qa_entries e USING (entry_id) "
                        f"WHERE e.entry_key = %s AND v.is_active ORDER BY v.phrasing", (entry_key,))
            return [r[0] for r in cur.fetchall()]

        def total_variants(entry_key):
            cur.execute(f"SELECT count(*) FROM {schema}.qa_variants v "
                        f"JOIN {schema}.qa_entries e USING (entry_id) "
                        f"WHERE e.entry_key = %s", (entry_key,))
            return cur.fetchone()[0]

        yield ingest, active_variants, total_variants
    finally:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db.close()


def test_republish_same_csv_does_not_multiply_variants(rig):
    """N2 / test A. The same file ingested twice must leave the entry with
    exactly the declared variants both times — the active count must not grow
    between the two runs (it must never exceed what the file carries)."""
    ingest, active_variants, total_variants = rig
    rows = [("e_dup", "Alt one\nAlt two", True)]

    result1 = ingest(rows)
    assert "ERROR" not in result1, result1
    first = active_variants("e_dup")
    assert len(first) == 3, first  # canonical + 2 declared alternatives

    result2 = ingest(rows)
    assert "ERROR" not in result2, result2
    second = active_variants("e_dup")
    assert len(second) == 3, second
    assert set(second) == set(first), "republishing the identical file changed the active set"

    # a third run, to catch a slower-growing multiplier that a single repeat
    # would not expose
    ingest(rows)
    assert len(active_variants("e_dup")) == 3


def test_removed_variant_is_deactivated_not_left_active(rig):
    """N2 / test B. Dropping a phrasing from the file and republishing must
    leave that phrasing inactive; the active set must match the new file."""
    ingest, active_variants, total_variants = rig
    ingest([("e_shrink", "Keep me\nDrop me", True)])
    assert set(active_variants("e_shrink")) == {
        "Question e_shrink", "Keep me", "Drop me"}

    ingest([("e_shrink", "Keep me", True)])
    active = set(active_variants("e_shrink"))
    assert active == {"Question e_shrink", "Keep me"}, active
    assert "Drop me" not in active

    # the dropped phrasing must still exist, only inactive (history kept,
    # never hard-deleted) — total count is at least the sum of both uploads
    assert total_variants("e_shrink") >= 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
