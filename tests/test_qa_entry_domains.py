"""QA CSV domains: real reader/exploder/writer with isolated PostgreSQL tables.

Run in the Langflow dev container (pandas/psycopg2 and DB env already available).
Pass the repository root as argv[1] when the files are copied under /tmp.
"""
import ast
import csv
import io
import json
import os
from pathlib import Path
import sys
import uuid
import types
from contextlib import contextmanager

import pandas as pd
import psycopg2
from psycopg2 import sql

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
flow = json.loads((ROOT / 'langflow_flows/ingestion/EDU_AI_QA.json').read_text())
lemma_template = next(n['data']['node']['template'] for n in flow['data']['nodes']
                      if n['id'] == 'ChunkLemmatizer-qa003-QA')
for field in ('pg_host', 'pg_port', 'pg_user', 'pg_password', 'pg_database', 'pg_schema'):
    assert field in lemma_template, f'QA lemmatizer missing DB input: {field}'
    assert lemma_template[field]['load_from_db'] and lemma_template[field]['value']
    assert lemma_template[field].get('show') or lemma_template[field].get('override_skip')
assert lemma_template['context']['value'] == 'ingest_qa'


def component(node_id, class_name):
    source = next(n['data']['node']['template']['code']['value']
                  for n in flow['data']['nodes'] if n['id'] == node_id)
    tree = ast.parse(source)
    # Only replace Langflow UI wiring; execute the shipped processing methods.
    tree.body = [n for n in tree.body if not (
        isinstance(n, ast.ImportFrom) and (n.module or '').startswith('lfx'))]
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            n.bases = []
            n.body = [x for x in n.body if not (isinstance(x, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id in ('inputs', 'outputs')
                              for t in x.targets))]
    ns = {'DataFrame': pd.DataFrame}
    exec(compile(ast.fix_missing_locations(tree), node_id, 'exec'), ns)
    return ns[class_name]()


def main():
    cfg = dict(host=os.getenv('POSTGRES_DB_SERVER', 'edu_ai_postgres'),
               port=os.getenv('POSTGRES_SERVER_PORT', '5432'),
               user=os.getenv('POSTGRES_DB_USER', 'edu_ai'),
               password=os.environ['POSTGRES_PASSWORD'],
               dbname=os.getenv('POSTGRES_DB', 'edu_ai'))
    db = psycopg2.connect(**cfg)
    db.autocommit = True
    cur = db.cursor()
    schema = 'test_qa_domains_' + uuid.uuid4().hex
    cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        for table in ('qa_entries', 'qa_variants', 'qa_embedding', 'qa_pack_manifest', 'ingestion_log'):
            cur.execute(sql.SQL('CREATE TABLE {}.{} (LIKE edu_ai.{} INCLUDING ALL)').format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
        for table in ('settings', 'code_list', 'legal_entity', 'taxonomy_binding', 'taxonomy_rules'):
            cur.execute(sql.SQL('CREATE VIEW {}.{} AS SELECT * FROM edu_ai.{}').format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(table)))
        for fn in ('entity_grade_scope(text)', 'resolved_code_list(text,text)',
                   'resolved_taxonomy_rules(text,text)'):
            cur.execute('SELECT pg_get_functiondef(%s::regprocedure)', ('edu_ai.' + fn,))
            cur.execute(cur.fetchone()[0].replace('FUNCTION edu_ai.', f'FUNCTION {schema}.', 1))
        cur.execute("SELECT legal_entity_code FROM edu_ai.legal_entity WHERE is_active ORDER BY legal_entity_code LIMIT 1")
        entity = cur.fetchone()[0]
        cur.execute("SELECT type_code,id FROM edu_ai.resolved_taxonomy_rules(%s,'DOMAIN') ORDER BY type_code", (entity,))
        catalog = dict(cur.fetchall())
        a, b = list(catalog)[:2]
        cur.execute("SELECT code FROM edu_ai.code_list WHERE list_code='language' AND is_active LIMIT 1")
        lang = cur.fetchone()[0]
        reader = component('QAJsonReader-qa001-QA', 'QAJsonReader')
        reader.file_path_override = ''
        reader._language_registry = lambda: {'active': [lang], 'fallback': lang}
        exploder = component('QAVariantExploder-qa002-QA', 'QAVariantExploder')
        exploder.variants_column, exploder.question_column, exploder.max_variants = 'variants_json', 'canonical_q', 3
        writer = component('PostgresInsertQA-qa004-QA', 'PostgresInsertQA')
        lemmatizer = component('ChunkLemmatizer-qa003-QA', 'ChunkLemmatizer')
        for attr, key in [('pg_host','host'),('pg_port','port'),('pg_user','user'),
                          ('pg_password','password'),('pg_database','dbname')]:
            setattr(writer, attr, cfg[key])
            setattr(lemmatizer, attr, cfg[key])
        writer.pg_schema = schema
        lemmatizer.pg_schema = 'edu_ai'
        lemmatizer.context = 'ingest_qa'
        lemmatizer.text_column = 'phrasing'
        lemmatizer.norm_column, lemmatizer.lemma_column, lemmatizer.stem_column = 'norm_text', 'lemma_text', 'stem_text'

        def ingest(items):
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=';')
            w.writerow(['entry_key','canonical_q','answer_md','variants','domains'])
            for key, domains in items:
                w.writerow([key, 'Question '+key, 'Answer '+key, 'Variant '+key, domains])
            reader.input_df = pd.DataFrame([dict(file_content_utf8=buf.getvalue(),
                user_defined_pack_id='test-domains', legal_entity_code=entity,
                document_language=lang, document_domain_orig=json.dumps([a]),
                visibility='entity', audience_roles='["PARENT"]')])
            exploder.dataframe = reader.process()
            lemmatizer.dataframe = exploder.process()
            writer.dataframe = lemmatizer.process()
            return writer.process().iloc[0]['result']

        def labels():
            cur.execute(f'SELECT entry_key,domain_ids FROM {schema}.qa_entries WHERE is_active')
            return {k: set(ids or []) for k, ids in cur.fetchall()}

        result = ingest([('a', a.lower()+','+a), ('b', b), ('both', json.dumps([a,b])),
                         ('inherit',''), ('none','[]')])
        assert 'ERROR' not in result, result
        assert labels() == {'a':{catalog[a]}, 'b':{catalog[b]}, 'both':{catalog[a],catalog[b]},
                            'inherit':{catalog[a]}, 'none':set()}, labels()
        cur.execute(f'SELECT entry_key,entry_id FROM {schema}.qa_entries')
        ids_before = dict(cur.fetchall())
        result = ingest([('a', b)])
        assert 'ERROR' not in result, result
        assert labels()['a'] == {catalog[b]} and len(labels()) == 5
        cur.execute(f'SELECT entry_key,entry_id FROM {schema}.qa_entries')
        assert dict(cur.fetchall()) == ids_before
        cur.execute(f'SELECT domain_ids FROM {schema}.qa_pack_manifest WHERE is_active')
        assert set(cur.fetchone()[0]) == {catalog[a],catalog[b]}
        cur.execute(f'''SELECT count(*) FROM {schema}.qa_variants v JOIN {schema}.qa_entries e
                        USING(entry_id) WHERE v.is_active AND v.domain_ids IS DISTINCT FROM e.domain_ids''')
        assert cur.fetchone()[0] == 0
        before = labels()
        assert 'ERROR' in ingest([('a', a), ('bad', 'NOT_A_REAL_DOMAIN')])
        assert labels() == before, 'Invalid domain partially changed the pack'
        # Execute the shipped export query against the same isolated tables.
        @contextmanager
        def get_cursor():
            with db.cursor() as cursor:
                yield cursor
        fake_db = types.ModuleType('qa_export_test.db')
        fake_db.get_cursor = get_cursor
        sys.modules['qa_export_test.db'] = fake_db
        # qa_export delegates the byte format to app/qa_csv.py, so the same
        # relative import has to resolve here too — loaded from the real file,
        # never reimplemented, or this stops testing the shipped bytes.
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location('qa_export_test.qa_csv',
                                             ROOT / 'search/app/qa_csv.py')
        _qa_csv = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_qa_csv)
        sys.modules['qa_export_test.qa_csv'] = _qa_csv
        source = ast.parse((ROOT / 'search/app/server.py').read_text())
        functions = [n for n in source.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('qa_export', '_sources_cell')]
        for fn in functions:
            fn.decorator_list = []
        ns = {'__package__': 'qa_export_test', 'json': json,
              'config': types.SimpleNamespace(PG_SCHEMA=schema),
              'Response': lambda **kwargs: kwargs['content']}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'qa_export', 'exec'), ns)
        # ',' since CONTRACT.md 5.1 v1.4.0; the reader still accepts ';'.
        exported = list(csv.DictReader(io.StringIO(ns['qa_export'](entity, 'test-domains')
                            .decode('utf-8-sig')), delimiter=','))
        assert len(exported) == 5
        for row in exported:
            assert {catalog[c] for c in json.loads(row['domains'])} == before[row['entry_key']]
        for invalid in ('[12]', 'A,,B', '[" "]'):
            try:
                ingest([('bad', invalid)])
            except ValueError:
                pass
            else:
                raise AssertionError('Malformed domains accepted: '+invalid)
        print('PASS: row domains, inheritance, clearing, normalization, variants, merge, stable IDs, atomic rejection, CSV export')
    finally:
        cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        db.close()


if __name__ == '__main__':
    main()
