"""Fictional-only regression checks for read-only operator applicability."""
import copy
import json
import sqlite3
import tempfile
import sys
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import operator_applicability as a
from operator_applicability_demo import example,examples,article

class ApplicabilityTests(unittest.TestCase):
    def setUp(self):self.x=example();self.before=copy.deepcopy(self.x)
    def state(self):return a.assess(self.x)['state']
    def test_unchanged_reference_only(self):
        r=a.assess(self.x);self.assertEqual(r['state'],'candidate');self.assertFalse(any(r[k] for k in ('can_apply','can_approve','can_publish')));self.assertIn('未記録',r['warnings'][-1])
    def test_addition_recheck(self):
        self.x['current_target']['articles'].append(article(777));r=a.assess(self.x);self.assertEqual(r['state'],'recheck');self.assertEqual(len(r['diff']['added']),1)
    def test_revision_recheck(self):
        self.x['current_target']['articles'][0]['title']='訂正';r=a.assess(self.x);self.assertEqual(r['state'],'recheck');self.assertEqual(len(r['diff']['revised']),1)
    def test_body_revision_recheck(self):
        self.x['current_target']['articles'][0]['body']='新本文';self.assertEqual(self.state(),'recheck')
    def test_deleted_recheck(self):
        self.x['current_target']['articles']=[];self.assertEqual(self.state(),'recheck')
    def test_move_invalid(self):
        url=self.x['current_target']['articles'][0]['url'];self.x['current_target']['articles']=[];self.x['article_event_bindings']={url:['other-event']};self.assertEqual(self.state(),'invalid')
    def test_shared_url_not_move_or_merge(self):
        url=self.x['current_target']['articles'][0]['url'];self.x['article_event_bindings']={url:['other-event',self.x['decision']['event_id']]};self.assertEqual(self.state(),'candidate')
    def test_split_invalid(self):
        self.x['constraints']['split_into']=['e2','e3'];self.assertEqual(self.state(),'invalid')
    def test_merge_invalid(self):
        self.x['current_target']['merged_into']='e2';self.assertEqual(self.state(),'invalid')
    def test_event_id_change_invalid(self):
        self.x['current_target']['event_id']='e2';self.assertEqual(self.state(),'invalid')
    def test_conflict_invalid(self):
        self.x['constraints']['conflict']=True;self.assertEqual(self.state(),'invalid')
    def test_distinct_invalid(self):
        self.x['constraints']['known_distinct']=True;self.assertEqual(self.state(),'invalid')
    def test_held_invalid(self):
        self.x['current_target']['publication_status']='held';self.assertEqual(self.state(),'invalid')
    def test_no_constraint_proof_unknown(self):
        self.x.pop('constraints');self.assertEqual(self.state(),'unknown')
    def test_missing_snapshot_unknown(self):
        self.x.pop('original_target');self.assertEqual(self.state(),'unknown')
    def test_bad_original_snapshot_unknown(self):
        self.x['original_target']['event_name']='different';self.assertEqual(self.state(),'unknown')
    def test_decision_id_tamper_unknown(self):
        self.x['decision']['note']='different';self.assertEqual(self.state(),'unknown')
    def test_source_revision_recheck(self):
        self.x['source_group']['articles'][0]['description']='訂正';self.assertEqual(self.state(),'recheck')
    def test_unhashed_summary_recheck(self):
        self.x['current_target']['summary']='変更された概要';self.assertEqual(a.target_hash(self.x['original_target']),a.target_hash(self.x['current_target']));self.assertEqual(self.state(),'recheck')
    def test_dependency_expiry_recheck(self):
        self.x['dependencies']=[{'state':'expired','quote_id':'q1'}];self.assertEqual(self.state(),'recheck')
    def test_dependency_unknown(self):
        self.x['dependencies']=[{'state':'unknown'}];self.assertEqual(self.state(),'unknown')
    def test_bad_dependency_unknown(self):
        self.x['dependencies']=['bad'];self.assertEqual(self.state(),'unknown')
    def test_current_dependency_preserved(self):
        self.x['dependencies']=[{'state':'current','quote':'架空引用','article_version_id':'fictional-v1'}];r=a.assess(self.x);self.assertEqual(r['state'],'candidate');self.assertEqual(r['dependencies'],self.x['dependencies'])
    def test_same_url_multiple_versions_recheck(self):
        self.x['current_target']['articles'].append({**self.x['current_target']['articles'][0],'title':'新しい版'});self.assertEqual(self.state(),'recheck')
    def test_multiple_judgments_same_event(self):
        r=a.evaluate(examples());self.assertEqual(r['counts'],{'candidate':19,'recheck':20,'invalid':0,'unknown':0});self.assertEqual(len(r['assessments']),39)
    def test_duplicate_id_coalesced(self):
        self.assertEqual(len(a.evaluate([self.x,copy.deepcopy(self.x)])['assessments']),1)
    def test_conflicting_duplicate_id_rejected(self):
        y=copy.deepcopy(self.x);y['current_target']['articles']=[]
        with self.assertRaises(ValueError):a.evaluate([self.x,y])
    def test_inputs_unchanged(self):
        a.evaluate([self.x]);self.assertEqual(self.x,self.before)
    def test_hash_compatible_with_native(self):
        from operator_review import digest,target_digest
        from pending_triage import fingerprint,group_key
        self.assertEqual(a.target_hash(self.x['original_target']),target_digest(self.x['original_target']));self.assertEqual(a.digest(self.x['source_group']),digest(self.x['source_group']));self.assertEqual(a.source_hash(self.x['source_group']),fingerprint(self.x['source_group']));self.assertEqual(a.group_key(self.x['source_group']),group_key(self.x['source_group']))
    def test_representation_and_restore_do_not_rebind(self):
        import pre_ai as pre
        group=copy.deepcopy(self.x['source_group']);original=copy.deepcopy(group)
        duplicate=copy.deepcopy(group);duplicate['articles'][0]['url']='https://example.test/duplicate-url'
        projection=pre.plan([group,duplicate]);self.assertEqual(len(projection['cohorts']),1)
        cohort=projection['cohorts'][0];self.assertTrue(cohort['active'])
        self.assertEqual(a.assess(self.x)['state'],'candidate')
        disabled=pre.plan([group,duplicate],disabled_cohorts=[cohort['cohort_id']])
        self.assertFalse(disabled['cohorts'][0]['active']);self.assertEqual(disabled['original_groups'][0],original)
        self.assertEqual(a.assess(self.x)['original_decision'],self.before['decision'])
        # A projected alias cannot inherit the original group's decision.
        changed=copy.deepcopy(self.x);changed['source_group']=duplicate
        self.assertEqual(a.assess(changed)['state'],'recheck')
    def test_html_readonly_escape_and_sections(self):
        self.x['current_target']['articles'].append(article(888));self.x['current_target']['event_name']='<script>alert(1)</script>'
        h=a.render(a.evaluate([self.x]));self.assertIn('&lt;script&gt;',h);self.assertNotIn('<script>',h)
        for tag in ('<button','<input','<form','<script','<iframe'):self.assertNotIn(tag,h)
        for word in ('追加：1件','判断時点の記事一覧','各判断への影響','日本時間','元URL・記事版'):self.assertIn(word,h)
    def test_timezone_unknown_not_invented(self):
        self.assertIn('時差未確認',a.japanese_date('2026-10-19T10:00:00'));self.assertEqual(a.japanese_date(None),'未確認');self.assertIn('2026年10月19日 10:00',a.japanese_date('2026-10-19T01:00:00Z'))
    def test_html_cannot_write_inside_repo(self):
        with self.assertRaises(ValueError):a.write_html(a.evaluate([self.x]),a.ROOT/'bad.html')

class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'private.sqlite');self.db=a.open_store(self.path);self.x=example();self.at=datetime(2026,10,19,tzinfo=timezone.utc)
    def tearDown(self):self.db.close();self.temp.cleanup()
    def save(self,operation='op1',**kwargs):return a.record(self.db,operation,[self.x],self.at,**kwargs)
    def test_separate_and_restore(self):
        r=self.save();self.assertEqual(a.restore_originals(self.db),[self.x['decision']]);self.assertEqual(self.db.execute('select count(*) from applicability_assessments').fetchone()[0],1);self.assertNotIn('state',a.restore_originals(self.db)[0]);self.assertFalse(r['can_apply'])
    def test_failure_atomic_recovery(self):
        with self.assertRaises(RuntimeError):self.save(fail_save=True)
        for table in ('original_decisions','applicability_assessments','applicability_runs'):self.assertEqual(self.db.execute('select count(*) from '+table).fetchone()[0],0)
        self.save();self.assertEqual(len(a.restore_originals(self.db)),1)
    def test_restart_replay_no_duplicates(self):
        r=self.save();self.db.close();self.db=a.open_store(self.path);self.assertEqual(self.save(),r);self.assertEqual(self.db.execute('select count(*) from applicability_runs').fetchone()[0],1)
    def test_same_operation_changed_input_rejected(self):
        self.save();self.x['current_target']['articles'].append(article(888))
        with self.assertRaises(ValueError):self.save()
    def test_assessment_changes_original_kept(self):
        self.save();self.x['current_target']['articles'].append(article(888));r=self.save('op2');self.assertEqual(r['counts']['recheck'],1);self.assertEqual(len(a.restore_originals(self.db)),1);self.assertEqual(self.db.execute('select count(*) from applicability_assessments').fetchone()[0],2)
    def test_sql_original_immutable(self):
        self.save()
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute("update original_decisions set payload='changed'")
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute('delete from original_decisions')
    def test_sql_assessment_immutable(self):
        self.save()
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute('delete from applicability_assessments')
    def test_changed_original_same_id_not_overwritten(self):
        self.save();self.x['decision']['submitted_at']='2026-10-18T02:00:00Z'
        with self.assertRaises(ValueError):self.save('op2')
        self.assertNotEqual(a.restore_originals(self.db)[0]['submitted_at'],self.x['decision']['submitted_at'])
    def test_foreign_db_untouched(self):
        p=Path(self.temp.name)/'foreign.db';d=sqlite3.connect(p);d.execute('create table production(x)');d.commit();d.close();before=p.read_bytes()
        with self.assertRaises(ValueError):a.open_store(p)
        self.assertEqual(before,p.read_bytes())
    def test_source_json_rejected_untouched(self):
        p=Path(self.temp.name)/'production.json';p.write_text('{}')
        with self.assertRaises(ValueError):a.open_store(p)
        self.assertEqual(p.read_text(),'{}')
    def test_invalid_timestamp_rejected(self):
        with self.assertRaises(ValueError):a.record(self.db,'x',[self.x],datetime(2026,10,19))

if __name__=='__main__':unittest.main()
