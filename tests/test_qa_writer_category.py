"""N3 regression: a CSV's category_code must resolve to qa_entries.category_id
against the global qa_category catalog (migration 215), honouring the three
CSV states — column absent (leave category_id untouched on UPDATE), cell blank
(explicit NULL), cell has a code (resolve, fail loudly if unknown).

Root cause this guards: QAVariantExploder rebuilt its per-entry `base` dict
without carrying `entry_category_code` through from QAJsonReader, so
PostgresInsertQA's `r.get("entry_category_code")` always saw None and
category_id stayed NULL even though both the reader and the writer already
implemented the three-state contract correctly.

Same extraction technique as tests/test_qa_entry_domains.py's `component()`
helper — the real reader/exploder/lemmatizer/writer classes, run against an
isolated schema in a live Postgres. Skips cleanly (module-level fixture) when
no such Postgres is reachable, or when the shared code_list carries no
qa_category rows (migration 215 not applied there).

    python3 -m pytest tests/test_qa_writer_category.py -v
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
    cfg = _db_config()
    try:
        db = psycopg2.connect(connect_timeout=3, **cfg)
    except Exception as e:
        pytest.skip(f"no live Postgres reachable for the writer test rig ({e})")
        return
    db.autocommit = True
    cur = db.cursor()

    cur.execute("SELECT code, id FROM edu_ai.code_list WHERE list_code = 'qa_category' "
                "AND is_active ORDER BY code LIMIT 2")
    cats = cur.fetchall()
    if len(cats) < 2:
        db.close()
        pytest.skip("shared code_list has fewer than 2 active qa_category rows "
                    "(migration 215 not applied against this database)")
        return
    (code_a, id_a), (code_b, id_b) = cats

    schema = "test_qa_category_" + uuid.uuid4().hex
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

        def ingest(rows_with_cat, has_category_col=True):
            """rows_with_cat: list of (entry_key, category_code_cell_or_None_for_absent_col)."""
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            header = ["entry_key", "canonical_q", "answer_md", "variants"]
            if has_category_col:
                header.append("category_code")
            w.writerow(header)
            for key, cat in rows_with_cat:
                row = [key, "Question " + key, "Answer " + key, ""]
                if has_category_col:
                    row.append(cat if cat is not None else "")
                w.writerow(row)
            reader.input_df = pd.DataFrame([dict(
                file_content_utf8=buf.getvalue(), user_defined_pack_id="test-category",
                legal_entity_code=entity, document_language=lang,
                visibility="entity", audience_roles='["PARENT"]')])
            exploder.dataframe = reader.process()
            lemmatizer.dataframe = exploder.process()
            writer.dataframe = lemmatizer.process()
            return writer.process().iloc[0]["result"]

        def category_id(entry_key):
            cur.execute(f"SELECT category_id FROM {schema}.qa_entries WHERE entry_key = %s", (entry_key,))
            row = cur.fetchone()
            return row[0] if row else None

        yield ingest, category_id, (code_a, id_a), (code_b, id_b)
    finally:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        db.close()


def test_category_code_resolves_to_category_id(rig):
    """N3 / test C. A CSV carrying category_code results in a non-NULL
    qa_entries.category_id resolving to that exact code."""
    ingest, category_id, (code_a, id_a), _ = rig
    result = ingest([("e_cat", code_a)])
    assert "ERROR" not in result, result
    assert category_id("e_cat") == id_a


def test_blank_cell_clears_absent_column_preserves(rig):
    """N3 / test D. A blank category_code cell means explicit NULL; an
    entirely absent category_code column leaves an existing category_id
    untouched on the next (merge) update."""
    ingest, category_id, (code_a, id_a), (code_b, id_b) = rig

    # give the entry a category first
    result = ingest([("e_cat2", code_a)])
    assert "ERROR" not in result, result
    assert category_id("e_cat2") == id_a

    # blank cell -> explicit NULL
    result = ingest([("e_cat2", "")])
    assert "ERROR" not in result, result
    assert category_id("e_cat2") is None

    # put a category back, then republish with the column absent entirely —
    # must survive unchanged
    ingest([("e_cat2", code_b)])
    assert category_id("e_cat2") == id_b
    result = ingest([("e_cat2", None)], has_category_col=False)
    assert "ERROR" not in result, result
    assert category_id("e_cat2") == id_b


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
