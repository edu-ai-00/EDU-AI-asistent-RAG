"""Real local SQLite/FTS, actual embedded exporter, synthetic read-only fake PG."""
import ast
import copy
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest import mock
from uuid import UUID, uuid4

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT/'langflow_flows/ingestion/EDU_AI_EXPORT_QA_PACK.json'


def exporter_class(pg):
    flow = json.loads(FLOW.read_text(encoding='utf-8'))
    code = next(n for n in flow['data']['nodes'] if n['id']=='ExportQAPack-qa010')['data']['node']['template']['code']['value']
    original = next(n for n in ast.parse(code).body if isinstance(n, ast.ClassDef) and n.name=='ExportQAPack')
    body = []
    for item in original.body:
        if isinstance(item,(ast.FunctionDef,ast.AsyncFunctionDef)):
            body.append(item)
        elif isinstance(item,ast.Assign):
            try:
                ast.literal_eval(item.value)
            except (ValueError,TypeError):
                continue
            body.append(item)
    isolated = ast.Module(body=[ast.ClassDef(name=original.name,bases=[],keywords=[],body=body,decorator_list=[])],type_ignores=[])
    ast.fix_missing_locations(isolated)
    namespace = dict(os=os,json=json,sqlite3=sqlite3,psycopg2=pg,pd=pd,DataFrame=lambda value:value,
                     re=re,tempfile=tempfile,Path=Path,date=date,datetime=datetime,UUID=UUID,Decimal=Decimal)
    exec(compile(isolated,'actual-export-component','exec'),namespace)
    return namespace['ExportQAPack']


def metadata(entity, *, offset=0, null=False, empty=False):
    arrays = None if null else ([] if empty else [offset+1,offset+2,offset+3])
    return dict(school_grade_ids=copy.deepcopy(arrays),school_level=None if null else 'ZS',
        school_subject_ids=copy.deepcopy(arrays),domain_ids=copy.deepcopy(arrays),binding_id=None if null else offset+8,
        legal_entity_code=entity,visibility='entity',owner_user_id=None if null else 'synthetic-owner-'+str(offset),
        audience_roles=None if null else ([] if empty else ['STUDENT','TEACHER','PARENT']),
        valid_from=None if null else date(2026,1,2),valid_to=None if null else date(2027,3,4),is_active=True,
        processing_uuid=uuid4(),file_uuid=uuid4(),created_at=datetime(2026,10,8,9,20,30,tzinfo=timezone.utc))


class FakePG:
    def __init__(self, entities, pack):
        self.calls=[];self.connections=[];self.manifests=[];self.entries=[];self.variants=[];self.force_foreign=False
        for index,entity in enumerate(entities):
            manifest=dict(metadata(entity,offset=index*100),pack_id=pack,lang='cs',version=2,schema_version=1,
                built_at=datetime(2026,10,8,8,0,tzinfo=timezone.utc),entry_count=999,t_answer=0.7,t_suggest=0.3,m_margin=0.08)
            self.manifests.extend([dict(manifest,version=1),manifest,dict(manifest,version=99,is_active=False)])
            for row_index in range(2):
                entry=dict(metadata(entity,offset=index*100+row_index*10,null=row_index==0,empty=row_index==1),
                    entry_id=uuid4(),pack_id=pack,canonical_q='Synthetic question '+str(index)+str(row_index),answer_md='Synthetic answer '+str(index),
                    asset_refs=None if row_index==0 else [],tags=None if row_index==0 else {'category':'Žluťoučký'},
                    entry_key='stable-'+str(row_index),category_id=None if row_index==0 else 42)
                entry['visibility']='private' if row_index==0 else 'organization'
                entry['owner_user_id']='entry-owner-'+str(row_index)
                self.entries.append(entry)
                variant=dict(metadata(entity,offset=index*100+30+row_index),variant_id=index*100+row_index+1,
                    entry_id=entry['entry_id'],phrasing='Kočka skáče',norm_text='kočka skáče',lemma_text='kočka skákat',
                    stem_text='kock skak',setting_id=uuid4())
                self.variants.append(variant)
            inactive=dict(self.entries[-1],entry_id=uuid4(),canonical_q='INACTIVE_SENTINEL',is_active=False)
            self.entries.append(inactive)
            self.variants.append(dict(self.variants[-1],variant_id=index*100+90,entry_id=inactive['entry_id']))
            self.variants.append(dict(self.variants[-3],variant_id=index*100+91,is_active=False,phrasing='INACTIVE_VARIANT'))
        self.variants.append(dict(self.variants[0],variant_id=9999,legal_entity_code=entities[1]))

    def connect(self, **kwargs):
        connection=Connection(self);self.connections.append(connection);return connection


class Connection:
    def __init__(self, pg):self.pg=pg;self.closed=False;self.session=None;self._cursor=Cursor(pg)
    def set_session(self, **kwargs):self.session=kwargs
    def cursor(self):return self._cursor
    def close(self):self.closed=True


class Cursor:
    def __init__(self, pg):self.pg=pg;self.result=[]
    def execute(self, query, params):
        self.pg.calls.append((query,params))
        if not query.lstrip().upper().startswith('SELECT '):raise AssertionError('PG_WRITE_FORBIDDEN')
        select=query.split('FROM',1)[0][len('SELECT '):]
        columns=[name.strip().split('.')[-1].split('::')[0] for name in select.split(',')]
        if 'subject' in columns or 'grade' in columns:raise RuntimeError('column subject does not exist')
        pack=params[0];entity=params[1] if len(params)>1 else None
        if 'qa_pack_manifest' in query:
            source=[r for r in self.pg.manifests if r['pack_id']==pack and
                    (entity is None or r['legal_entity_code']==entity) and ('m.is_active' not in query or r['is_active'])]
            source=sorted(source,key=lambda r:r['version'],reverse='ORDER BY m.version DESC' in query)[:1]
        elif 'qa_variants' in query:
            by_id={e['entry_id']:e for e in self.pg.entries}
            source=[]
            for row in self.pg.variants:
                entry=by_id[row['entry_id']]
                if entry['pack_id']!=pack or not entry['is_active']:continue
                if entity is not None and entry['legal_entity_code']!=entity:continue
                if 'v.legal_entity_code' in query and entity is not None and row['legal_entity_code']!=entity:continue
                if 'e.legal_entity_code = v.legal_entity_code' in query and entry['legal_entity_code']!=row['legal_entity_code']:continue
                if 'v.is_active' in query and not row['is_active']:continue
                source.append(row)
            source.sort(key=lambda r:r['variant_id'])
        else:
            source=[e for e in self.pg.entries if e['pack_id']==pack and e['is_active'] and
                    (entity is None or e['legal_entity_code']==entity)]
            source.sort(key=lambda r:r['canonical_q'])
        if self.pg.force_foreign and source:
            source=[dict(source[0],legal_entity_code='FOREIGN_SOURCE_ROW')]
        self.result=[tuple(row.get(name) for name in columns) for row in source]
    def fetchone(self):return self.result[0] if self.result else None
    def fetchall(self):return self.result


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'exports'
        self.entities=['SYNTH_'+uuid4().hex,'SYNTH_'+uuid4().hex]
        self.pack='pack-'+uuid4().hex
        self.pg=FakePG(self.entities,self.pack)
        self.cls=exporter_class(self.pg)
        self.obj=self.cls()
        for key,value in dict(pg_host='synthetic',pg_port='5432',pg_user='synthetic',pg_password='synthetic-secret',
                              pg_database='synthetic',pg_schema='edu_ai',pack_id=self.pack,legal_entity=self.entities[0],output_dir=str(self.root)).items():
            setattr(self.obj,key,value)

    def export(self):return self.obj.process().iloc[0].to_dict()

    def success(self):
        result=self.export();self.assertIn('pack_path',result,result.get('result'))
        return result,Path(result['pack_path'])

    def test_approved_schema_exact_column_order_and_unchanged_fts_definitions(self):
        result,path=self.success()
        ddl=(ROOT/'contracts/sqlite-qa-export.sql').read_text(encoding='utf-8')
        self.assertEqual(self.obj._DDL.strip(),ddl.strip())
        with closing(sqlite3.connect(':memory:')) as golden,closing(sqlite3.connect(path)) as actual:
            golden.executescript(ddl)
            for table in ('pack_manifest','entries','variants'):
                self.assertEqual(actual.execute('PRAGMA table_info('+table+')').fetchall(),golden.execute('PRAGMA table_info('+table+')').fetchall())
            for table in ('variants_fts','variants_tri'):
                get=lambda db:re.sub(r'\s+',' ',db.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0]).strip()
                self.assertEqual(get(actual),get(golden))

    def test_entities_same_pack_have_independent_files_and_active_scoped_rows(self):
        first,path1=self.success();old=path1.read_bytes()
        self.obj.legal_entity=self.entities[1]
        second,path2=self.success()
        self.assertNotEqual(path1,path2);self.assertEqual(path1.read_bytes(),old)
        self.assertEqual(path1,self.root/self.entities[0]/(self.pack+'.sqlite'))
        for entity,path in zip(self.entities,(path1,path2)):
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT DISTINCT legal_entity_code FROM entries').fetchall(),[(entity,)])
                self.assertEqual(db.execute('SELECT DISTINCT legal_entity_code FROM variants').fetchall(),[(entity,)])
                self.assertEqual(db.execute('SELECT entry_count,version,subject,grade FROM pack_manifest').fetchone(),(2,2,None,None))
                self.assertEqual(db.execute('SELECT count(*) FROM entries').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT count(*) FROM variants').fetchone()[0],2)

    def test_arrays_null_empty_roles_and_per_row_metadata_are_preserved(self):
        _,path=self.success()
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            manifest=dict(db.execute('SELECT * FROM pack_manifest').fetchone())
            self.assertEqual(json.loads(manifest['school_grade_ids']),[1,2,3])
            entries=[dict(r) for r in db.execute('SELECT * FROM entries ORDER BY canonical_q')]
            self.assertIsNone(entries[0]['school_grade_ids']);self.assertEqual(entries[1]['school_grade_ids'],'[]')
            self.assertIsNone(entries[0]['asset_refs']);self.assertEqual(entries[1]['asset_refs'],'[]')
            self.assertIsNone(entries[0]['audience_roles']);self.assertEqual(entries[1]['audience_roles'],'[]')
            self.assertEqual(entries[0]['visibility'],'private');self.assertEqual(entries[0]['owner_user_id'],'entry-owner-0')
            variants=[dict(r) for r in db.execute('SELECT * FROM variants ORDER BY variant_id')]
            self.assertEqual(json.loads(variants[0]['school_grade_ids']),[31,32,33])
            self.assertEqual(json.loads(variants[0]['audience_roles']),['STUDENT','TEACHER','PARENT'])
            self.assertEqual(variants[0]['valid_from'],'2026-01-02');self.assertEqual(variants[0]['created_at'],'2026-10-08T09:20:30+00:00')
            self.assertEqual(variants[0]['stem_text'],'kock skak')
            self.assertEqual(set(r['is_active'] for r in entries+variants),{1})

    def test_full_asset_json_and_all_provenance_metadata_are_preserved(self):
        entry=self.pg.entries[0]
        entry['asset_refs']=[{'title':'Žluťoučký','url':'https://synthetic.invalid/a'},
                             {'title':'Druhý','url':'https://synthetic.invalid/b'}]
        entry['tags']={'category':'K','nested':{'all':[1,2,3]}}
        _,path=self.success()
        with closing(sqlite3.connect(path)) as db:
            db.row_factory=sqlite3.Row
            actual=dict(db.execute('SELECT * FROM entries WHERE entry_id=?',(str(entry['entry_id']),)).fetchone())
            self.assertEqual(json.loads(actual['asset_refs']),entry['asset_refs'])
            self.assertEqual(json.loads(actual['tags']),entry['tags'])
            self.assertEqual(actual['processing_uuid'],str(entry['processing_uuid']))
            self.assertEqual(actual['file_uuid'],str(entry['file_uuid']))
            self.assertEqual(actual['entry_key'],entry['entry_key'])
            variant=self.pg.variants[0]
            actual=dict(db.execute('SELECT * FROM variants WHERE variant_id=?',(variant['variant_id'],)).fetchone())
            for name in ('setting_id','processing_uuid','file_uuid'):
                self.assertEqual(actual[name],str(variant[name]))
            for name in ('school_grade_ids','school_subject_ids','domain_ids','audience_roles'):
                self.assertEqual(json.loads(actual[name]),variant[name])
            for name in ('school_level','binding_id','owner_user_id','visibility'):
                self.assertEqual(actual[name],variant[name])

    def test_pack_schema_version_is_copied_and_not_used_as_a_new_format_version(self):
        for manifest in self.pg.manifests:manifest['schema_version']=3
        _,path=self.success()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM pack_manifest').fetchone()[0],3)

    def test_empty_active_pack_has_zero_count_and_valid_empty_fts(self):
        self.pg.entries=[];self.pg.variants=[]
        result,path=self.success()
        self.assertEqual(result['entry_count'],0);self.assertEqual(result['variant_count'],0)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT entry_count FROM pack_manifest').fetchone()[0],0)
            for table in ('variants_fts','variants_tri'):
                db.execute('INSERT INTO '+table+'('+table+',rank) VALUES (?,1)',('integrity-check',))

    def test_actual_fts_rebuild_and_rank_one_integrity_against_content(self):
        _,path=self.success()
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(len(db.execute('SELECT rowid FROM variants_fts WHERE variants_fts MATCH ?',('kocka',)).fetchall()),2)
            self.assertEqual(len(db.execute('SELECT rowid FROM variants_fts WHERE variants_fts MATCH ?',('skakat',)).fetchall()),2)
            self.assertEqual(len(db.execute('SELECT rowid FROM variants_tri WHERE variants_tri MATCH ?',('očk',)).fetchall()),2)
            for table in ('variants_fts','variants_tri'):
                db.execute('INSERT INTO '+table+'('+table+',rank) VALUES (?,1)',('integrity-check',))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_rank_one_integrity_detects_empty_index_despite_external_content_count(self):
        _,path=self.success()
        db=sqlite3.connect(path)
        try:
            db.execute("INSERT INTO variants_fts(variants_fts) VALUES ('delete-all')")
            self.assertEqual(db.execute('SELECT count(*) FROM variants_fts').fetchone()[0],2)
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("INSERT INTO variants_fts(variants_fts,rank) VALUES ('integrity-check',1)")
        finally:db.close()

    def test_every_pg_query_is_bound_scoped_active_and_consistent_readonly(self):
        self.success()
        self.assertEqual(len(self.pg.calls),3)
        for query,params in self.pg.calls:
            self.assertNotIn(self.pack,query);self.assertNotIn(self.entities[0],query)
            self.assertEqual(params[:2],(self.pack,self.entities[0]))
            self.assertIn('legal_entity_code',query);self.assertIn('is_active',query)
        self.assertEqual(self.pg.calls[2][1],(self.pack,self.entities[0],self.entities[0]))
        self.assertIn('e.legal_entity_code = v.legal_entity_code',self.pg.calls[2][0])
        self.assertEqual(self.pg.connections[0].session,{'readonly':True,'isolation_level':'REPEATABLE READ'})
        self.assertTrue(self.pg.connections[0].closed)

    def test_required_entity_and_safe_segments_fail_before_pg_or_files(self):
        cases=[('legal_entity',x) for x in (None,'','   ','../entity','entity/other','entity\\other','C:entity','x\x00y')]
        cases += [('pack_id',x) for x in ('../pack','a/b','a\\b','..','a:ads','x\ny')]
        cases += [('pg_schema','edu_ai;DROP TABLE qa_entries')]
        for field,value in cases:
            with self.subTest(field=field,value=value):
                original=getattr(self.obj,field);setattr(self.obj,field,value)
                result=self.export();setattr(self.obj,field,original)
                self.assertTrue(result['result'].startswith('ERROR'))
                self.assertEqual(self.pg.calls,[]);self.assertEqual(self.pg.connections,[]);self.assertFalse(self.root.exists())

    def test_no_manifest_is_scoped_and_creates_no_directory_or_file(self):
        self.obj.legal_entity='SMOKE_'+uuid4().hex
        result=self.export()
        self.assertIn('no manifest for pack_id=',result['result'])
        self.assertEqual(len(self.pg.calls),1);self.assertFalse(self.root.exists())
        self.assertTrue(self.pg.connections[0].closed)

    def test_foreign_source_row_is_refused_defensively(self):
        self.pg.force_foreign=True
        result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'));self.assertFalse(self.root.exists())

    def test_pg_failure_closes_readonly_connection_and_preserves_existing_artifact(self):
        _,path=self.success();original=path.read_bytes()
        with mock.patch.object(Cursor,'fetchall',side_effect=RuntimeError('synthetic source fetch failure')):
            result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(path.read_bytes(),original)
        self.assertTrue(self.pg.connections[-1].closed)
        self.assertEqual(list(path.parent.iterdir()),[path])

    def test_sqlite_failure_keeps_previous_complete_file_and_cleans_temp(self):
        _,path=self.success();original=path.read_bytes()
        self.obj._DDL='THIS IS INVALID SQLITE DDL'
        result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(path.parent.iterdir()),[path])

    def test_publication_failure_keeps_previous_file_and_cleans_temp(self):
        _,path=self.success();original=path.read_bytes()
        with mock.patch.object(os,'replace',side_effect=PermissionError('synthetic publication failure')):
            result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(path.parent.iterdir()),[path])

    def test_database_close_failure_does_not_prevent_owned_temp_cleanup(self):
        temporary=Path(self.temp.name)/'own-temp.sqlite';temporary.write_bytes(b'synthetic')
        db=mock.Mock();db.close.side_effect=OSError('synthetic close failure')
        self.assertFalse(self.obj._cleanup(db,str(temporary)))
        self.assertFalse(temporary.exists())

    def test_directory_descriptor_close_failure_is_best_effort(self):
        with mock.patch.object(os,'open',return_value=321),mock.patch.object(os,'fsync'), \
                mock.patch.object(os,'close',side_effect=OSError('synthetic directory close failure')):
            self.assertFalse(self.obj._directory_sync('synthetic-directory'))

    def test_cleanup_does_not_delete_a_replaced_foreign_inode(self):
        path=Path(self.temp.name)/'different-inode.sqlite';path.write_bytes(b'preserve unrelated file')
        info=path.stat()
        self.assertFalse(self.obj._cleanup(None,str(path),(info.st_dev,info.st_ino+1)))
        self.assertEqual(path.read_bytes(),b'preserve unrelated file')

    def test_directory_identity_failure_preserves_previous_file(self):
        _,path=self.success();original=path.read_bytes();info=path.parent.stat()
        with mock.patch.object(self.obj,'_check_directory',side_effect=[(info.st_dev,info.st_ino),ValueError('changed directory')]):
            result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(path.parent.iterdir()),[path])

    def test_entity_link_guard_is_exercised_even_without_os_symlink_privilege(self):
        entity_dir=self.root/self.entities[0]
        islink=os.path.islink
        with mock.patch.object(os.path,'islink',side_effect=lambda path: str(path)==str(entity_dir) or islink(path)):
            result=self.export()
        self.assertTrue(result['result'].startswith('ERROR'))
        self.assertEqual(list(entity_dir.iterdir()),[])

    def test_directory_sync_limitation_after_publication_reports_valid_success(self):
        with mock.patch.object(self.obj,'_directory_sync',return_value=False):
            result,path=self.success()
        self.assertIn('directory fsync unverified',result['result'])
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM entries').fetchone()[0],2)

    def test_atomic_commit_point_sees_old_complete_file_and_new_complete_database(self):
        _,path=self.success();old=path.read_bytes()
        self.pg.entries[0]['answer_md']='changed synthetic answer'
        replace=os.replace
        observed=[]
        def publication(source,destination):
            self.assertEqual(Path(destination).read_bytes(),old)
            with closing(sqlite3.connect(source)) as db:
                self.assertEqual(db.execute('SELECT answer_md FROM entries ORDER BY canonical_q').fetchone()[0],'changed synthetic answer')
                db.execute("INSERT INTO variants_fts(variants_fts,rank) VALUES ('integrity-check',1)")
            observed.append(True)
            replace(source,destination)
        with mock.patch.object(os,'replace',side_effect=publication):self.success()
        self.assertEqual(observed,[True])

    def test_entity_symlink_and_destination_symlink_never_escape_root(self):
        self.root.mkdir();outside=Path(self.temp.name)/'outside';outside.mkdir()
        try:(self.root/self.entities[0]).symlink_to(outside,target_is_directory=True)
        except OSError as error:self.skipTest('symlinks unavailable: '+type(error).__name__)
        result=self.export();self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(list(outside.iterdir()),[])
        (self.root/self.entities[0]).unlink();(self.root/self.entities[0]).mkdir()
        victim=outside/'victim.sqlite';victim.write_bytes(b'preserve external file')
        (self.root/self.entities[0]/(self.pack+'.sqlite')).symlink_to(victim)
        result=self.export();self.assertTrue(result['result'].startswith('ERROR'));self.assertEqual(victim.read_bytes(),b'preserve external file')


class CallerAndDeclarationTests(unittest.TestCase):
    def probe_helper(self):
        source=ast.parse((ROOT/'scripts/smoke.py').read_text(encoding='utf-8'))
        values={}
        for node in source.body:
            if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                try:values[node.targets[0].id]=ast.literal_eval(node.value)
                except (ValueError,TypeError):pass
        helper=next(n for n in source.body if isinstance(n,ast.FunctionDef) and n.name=='probe_request_body')
        module=ast.Module(body=[helper],type_ignores=[]);ast.fix_missing_locations(module)
        namespace=dict(values,uuid=__import__('uuid'))
        exec(compile(module,'pure-smoke-body','exec'),namespace)
        return namespace['probe_request_body'],values

    def test_required_input_has_no_default_and_embedded_code_hash_matches(self):
        flow=json.loads(FLOW.read_text(encoding='utf-8'))
        node=next(n for n in flow['data']['nodes'] if n['id']=='ExportQAPack-qa010')['data']['node']
        source=node['template']['code']['value']
        self.assertEqual(node['metadata']['code_hash'],hashlib.sha256(source.encode()).hexdigest()[:12])
        cls=next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='ExportQAPack')
        inputs=next(n.value for n in cls.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='inputs' for t in n.targets))
        declaration=next(n for n in inputs.elts if any(k.arg=='name' and isinstance(k.value,ast.Constant) and k.value.value=='legal_entity' for k in n.keywords))
        kwargs={k.arg:ast.literal_eval(k.value) for k in declaration.keywords}
        self.assertTrue(kwargs['required']);self.assertNotIn('value',kwargs)
        self.assertTrue(node['template']['legal_entity']['required'])
        self.assertEqual(node['template']['legal_entity']['value'],'')
        self.assertFalse(node['template']['legal_entity']['load_from_db'])

    def test_smoke_body_uses_fresh_synthetic_entity_and_existing_output_leaf(self):
        helper,values=self.probe_helper();first,second=helper(),helper()
        a=first['tweaks'][values['PROBE_NODE']]['legal_entity'];b=second['tweaks'][values['PROBE_NODE']]['legal_entity']
        self.assertNotEqual(a,b);self.assertRegex(a,r'^SMOKE_[0-9a-f]{32}$')
        self.assertEqual(first['tweaks'][values['PROBE_NODE']]['pack_id'],values['PROBE_PACK_ID'])
        self.assertNotIn('input_value',first)
        flow=json.loads(FLOW.read_text(encoding='utf-8'))
        self.assertIn(first['output_component'],[n['id'] for n in flow['data']['nodes']])


if __name__=='__main__':unittest.main(verbosity=2)
