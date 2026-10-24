"""Offline reference material and scoped input reuse; no news or AI client."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
import operator_app as app
import operator_support as support
import operator_preview as preview
from operator_task_demo import populate
from engine import events
import revision_sync as sync

class ReferenceTests(unittest.TestCase):
    def setUp(self):self.db=app.connect();app.initialize(self.db);populate(self.db)
    def tearDown(self):self.db.close()
    def state(self):return app.snapshot(self.db)
    def task(self,n='02',eid=None):return next(t for t in self.state()['work']['tasks'] if t.get('article',{}).get('url')=='https://example.test/demo-'+n and (eid is None or t['required_event_id']==eid))
    def draft(self,n='02',eid=None):
        t=self.task(n,eid);return t['drafts_by_event'][eid or t['suggested_event_id']]
    def seed(self,n='02',required=None):
        t=self.task(n,required);eid=t['suggested_event_id'];d=t['drafts_by_event'][eid]
        c=dict(action='task_review',op='saved-reference-example',revision=self.state()['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=eid,relation='same_event',progress='new',proposed_stage=d['proposed_stage'],reason='新しい事実を引用で確認／以前の記録との差を確認／自由な補足',reason_codes=['新しい事実を引用で確認','以前の記録との差を確認'],draft_review={'draft_id':d['draft_id'],'applied_fields':[]},progress_details=dict(new_fact=d['new_fact'],difference='保存済みの人の入力',evidence_ids=[],duplicate_checked=False))
        app.operate(self.db,c);return c
    def command(self,n='02',required=None):
        t=self.task(n,required);eid=t['suggested_event_id'];d=t['drafts_by_event'][eid];p=d['previous_input']
        return dict(action='task_review',op='reused-reference-example',revision=self.state()['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=eid,relation='same_event',progress='new',proposed_stage=p['proposed_stage'],reason=p['original_reason'],reason_codes=p['reason_codes'],copied_review_op=p['op'],draft_review={'draft_id':d['draft_id'],'applied_fields':[]},progress_details=dict(new_fact=p['new_fact'],difference=p['difference'],evidence_ids=[],duplicate_checked=False))
    def test_missing_fields_never_count_as_differences(self):
        m=self.draft('04')['materials'];self.assertEqual(m['differences'],[]);self.assertTrue(m['unknown']);self.assertEqual(m['label'],'ルールによる参考情報・未確定')
    def test_matches_have_both_source_quotes(self):
        m=self.draft()['materials'];self.assertTrue(m['matches']);self.assertTrue(all(p['existing'] and p['incoming'] for p in m['matches']))
    def test_differences_have_evidence_not_just_absence(self):
        m=self.draft('03')['materials'];self.assertTrue(m['differences']);self.assertTrue(all(p['existing'] and p['incoming'] for p in m['differences']))
    def test_next_steps_include_missing_dates_without_publication_inference(self):
        m=self.draft()['materials'];self.assertTrue(any('発生日時' in p for p in m['next_checks']));self.assertIsNone(self.draft()['event_occurred_at'])
    def test_new_quotation_is_not_classified_as_contradiction(self):
        self.assertEqual(self.draft()['materials']['fact_status'],'未確認')
    def test_exact_quote_equality_is_literal_only(self):
        m=support.materials({'rows':[],'existing_facts':[['old','原文']],'incoming_facts':[['new','原文']]},{})
        self.assertEqual(m['fact_status'],'一致');self.assertIn('証明ではありません',m['fact_note'])
    def test_previous_input_is_not_reused_without_saved_record(self):self.assertIsNone(self.draft()['previous_input'])
    def test_valid_previous_input_keeps_text_but_no_confirmations(self):
        self.seed();p=self.draft()['previous_input'];self.assertEqual(p['reason_text'],'自由な補足');self.assertEqual(p['difference'],'保存済みの人の入力');self.assertFalse(p['confirmation_reused']);self.assertNotIn('evidence_ids',p);self.assertNotIn('duplicate_checked',p)
    def test_reuse_remains_hold_until_quotes_and_duplicates_checked(self):
        self.seed();before=events(self.db);out=app.operate(self.db,self.command());self.assertEqual(out['progress_assessment'],'needs_evidence');self.assertFalse(out['adopted']);self.assertFalse(out['published']);self.assertEqual(events(self.db),before)
    def test_reuse_origin_is_saved_separately(self):
        self.seed();c=self.command();app.operate(self.db,c);d=self.task()['record']['review_details'];self.assertEqual(d['review_reuse']['source']['op'],'saved-reference-example');self.assertFalse(d['review_reuse']['confirmation_reused'])
    def test_changed_article_invalidates_previous_input(self):
        self.seed();t=self.task();sync.change_source(self.db,'article-revised',t['article'],t['source']['state_id'],app.now(),body_text='訂正本文');self.assertIsNone(self.draft()['previous_input'])
    def test_shared_article_reviews_are_not_copied_across_incidents(self):
        self.seed('10','demo-e2');self.assertIsNotNone(self.draft('10','demo-e2')['previous_input']);self.assertIsNone(self.draft('10','demo-e3')['previous_input'])
    def test_unknown_origin_rejected_without_new_review(self):
        self.seed();c=self.command();c['copied_review_op']='other-event-record'
        with self.assertRaises(ValueError):app.operate(self.db,c)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0],1)
    def test_reuse_requires_current_proposal_id(self):
        self.seed();c=self.command();c.pop('draft_review')
        with self.assertRaises(ValueError):app.operate(self.db,c)
    def test_projection_does_not_bill_or_write(self):
        self.seed();t=self.task();e=next(e for e in events(self.db) if e['event_id']==t['suggested_event_id']);before=self.db.total_changes;a=support.build(self.db,t,e);b=support.build(self.db,t,e);self.assertEqual(a,b);self.assertEqual(before,self.db.total_changes);self.assertFalse(a['ai_called'])
    def test_example_preview_is_fictional_readonly_with_saved_input(self):
        import re
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'preview.html';preview.build(p,demo=True,review_reuse_example=True);html=p.read_text();s=json.loads(re.search(r'<script id="offlineSnapshot" type="application/json">(.*?)</script>',html,re.S).group(1));self.assertEqual(s['data_scope'],'synthetic_demo');self.assertFalse(s['can_publish']);self.assertFalse(s['legacy_operator_records']);self.assertTrue(any(d['previous_input'] for t in s['work']['tasks'] for d in t.get('drafts_by_event',{}).values()));self.assertIn("connect-src 'none'",html)
    def test_review_example_cannot_be_added_to_real_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):preview.build(Path(tmp)/'preview.html',snapshot_root=tmp,review_reuse_example=True)
    def test_cancelled_operator_review_not_offered_for_reuse(self):
        self.seed();r=self.task()['record']
        with self.db:self.db.execute("UPDATE sync_decision_state SET status='cancelled' WHERE decision_id=?",(r['decision_id'],))
        self.assertIsNone(self.draft()['previous_input'])
    def test_reuse_rollback_and_operation_receipt_prevent_duplicates(self):
        self.seed();c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0],1)
        first=app.operate(self.db,c);self.assertEqual(app.operate(self.db,c),first);self.assertEqual(self.db.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0],2)

if __name__=='__main__':unittest.main()
