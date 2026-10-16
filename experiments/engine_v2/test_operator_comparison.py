"""Synthetic regression tests; no network or production writes."""
import copy
import json
import unittest
import operator_app as app
import operator_comparison as comparison
from operator_task_demo import populate
from engine import events
import revision_sync as sync

class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.db=app.connect();app.initialize(self.db);populate(self.db)
    def tearDown(self):self.db.close()
    def state(self):return app.snapshot(self.db)
    def task(self,n='02',eid=None):
        return next(t for t in self.state()['work']['tasks'] if t.get('article',{}).get('url')=='https://example.test/demo-'+n and (eid is None or t['required_event_id']==eid))
    def command(self,n='02',**kw):
        t=self.task(n,kw.pop('required',None));eid=t['suggested_event_id']
        proofs=[p['evidence_id'] for p in t['evidence_by_event'][eid] if p['quote'] and p['verification']['status']=='exact_literal_match']
        c=dict(action='task_review',op='comparison-regression',revision=self.state()['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=eid,relation='same_event',progress='new',proposed_stage='起訴',reason='架空の引用を確認',reason_codes=['新しい事実を引用で確認'],progress_details=dict(new_fact='起訴',difference='逮捕から起訴という記述へ変化',evidence_ids=proofs,duplicate_checked=True))
        c.update(kw);return c
    def test_explicit_comparison_matches_without_datetime_inference(self):
        t=self.task();c=t['comparison_by_event'][t['suggested_event_id']];rows={r['field']:r for r in c['rows']}
        self.assertEqual(rows['place']['status'],'一致');self.assertEqual(rows['occurred_at']['status'],'未確認');self.assertEqual(rows['occurred_at']['incoming'],[])
        self.assertEqual(c['scope'],'reference_only_not_identity_proof')
    def test_different_place_is_visible_without_automatic_verdict(self):
        t=self.task('03');c=t['comparison_by_event']['demo-e1'];self.assertEqual(next(r for r in c['rows'] if r['field']=='place')['status'],'相違')
        before=events(self.db);out=app.operate(self.db,self.command('03',relation='different_event',progress=None,reason_codes=['発生場所が異なる','関係人物が異なる'],progress_details=None));self.assertFalse(out['adopted']);self.assertEqual(events(self.db),before)
    def test_unknown_has_no_fabricated_anchors(self):
        t=self.task('04');c=t['comparison_by_event'][t['suggested_event_id']];self.assertTrue(all(r['status']=='未確認' for r in c['rows']))
    def test_notes_require_literal_event_scoped_current_evidence(self):
        t=self.task();proof=next(p for p in t['evidence'] if p['quote'])
        for args in [('demo-e3',t['article']['url'],'place','江別豊幌',proof['evidence_id']),('demo-e2',t['article']['url'],'place','東京',proof['evidence_id'])]:
            with self.assertRaises(ValueError):comparison.add_note(self.db,*args)
    def test_stale_comparison_note_removed_but_history_preserved(self):
        t=self.task();url=t['article']['url'];before=self.db.execute('SELECT count(*) FROM ui_comparison_notes').fetchone()[0]
        a=copy.deepcopy(t['article']);a['title']='訂正された見出し'
        sync.change_source(self.db,'comparison-title-change',a,sync.head(self.db,url)['state_id'],app.now(),body_text='本文を訂正')
        t2=self.task();self.assertNotEqual(t['context_hash'],t2['context_hash']);self.assertTrue(all(not r['incoming'] for r in t2['comparison_by_event']['demo-e2']['rows']));self.assertEqual(self.db.execute('SELECT count(*) FROM ui_comparison_notes').fetchone()[0],before)
    def test_shared_url_has_separate_event_quotes(self):
        a=self.task('10','demo-e2');b=self.task('10','demo-e3');self.assertNotEqual(a['task_id'],b['task_id'])
        for t,eid,person,other in [(a,'demo-e2','被害者山田','被害者佐藤'),(b,'demo-e3','被害者佐藤','被害者山田')]:
            values=[v['value'] for r in t['comparison_by_event'][eid]['rows'] for v in r['incoming']]
            self.assertIn(person,values);self.assertNotIn(other,values)
    def test_shared_url_review_does_not_merge_or_complete_other_task(self):
        before=events(self.db);other=self.task('10','demo-e3')['context_hash']
        app.operate(self.db,self.command('10',required='demo-e2',progress='none',reason_codes=['既報と同じ内容'],progress_details=None))
        self.assertEqual(events(self.db),before);self.assertEqual(self.task('10','demo-e3')['context_hash'],other)
    def test_new_progress_details_are_only_reference_not_adoption(self):
        before=events(self.db);out=app.operate(self.db,self.command());self.assertEqual(out['progress_assessment'],'operator_reported_new');self.assertFalse(out['published']);self.assertEqual(events(self.db),before)
        row=self.state()['work']['completed'][0]['record'];self.assertEqual(row['review_details']['progress_details']['new_fact'],'起訴')
    def test_missing_details_content_keeps_hold(self):
        c=self.command();c['progress_details']['new_fact']=''
        out=app.operate(self.db,c);self.assertEqual(out['progress_assessment'],'needs_evidence');self.assertEqual(self.task()['category'],'hold')
    def test_unchecked_duplicate_keeps_hold(self):
        c=self.command();c['progress_details']['duplicate_checked']=False
        self.assertEqual(app.operate(self.db,c)['progress_assessment'],'needs_evidence')
    def test_unquoted_new_fact_keeps_hold(self):
        c=self.command();c['progress_details']['new_fact']='判決が確定した'
        self.assertEqual(app.operate(self.db,c)['progress_assessment'],'needs_evidence')
    def test_foreign_event_evidence_rejected(self):
        c=self.command();c['progress_details']['evidence_ids']=[self.task('03')['evidence'][0]['evidence_id']]
        with self.assertRaisesRegex(ValueError,'別事件'):app.operate(self.db,c)
    def test_body_unconfirmed_remains_unknown_and_not_gold(self):
        t=self.task('08');self.assertEqual(t['source']['availability'],'unknown');self.assertTrue(all(not p['verification']['gold_promoted'] for p in t['evidence']))
    def test_missing_quote_hold_saves_selected_reasons_without_event(self):
        c=self.command('04',event_id=None,relation='hold',progress=None,reason_codes=['本文未確認','引用根拠が不足'],progress_details=None)
        app.operate(self.db,c);r=self.task('04')['record'];self.assertEqual(r['review_details']['reason_codes'],c['reason_codes']);self.assertIsNone(r['decision_id'])
    def test_wrong_reason_group_rejected(self):
        with self.assertRaisesRegex(ValueError,'理由'):app.operate(self.db,self.command(reason_codes=['発生場所が異なる']))
    def test_duplicate_reasons_and_malformed_details_rejected(self):
        for kw in [dict(reason_codes=['その他','その他']),dict(progress_details={'evidence_ids':'not a list'}),dict(progress_details={'duplicate_checked':'yes'}),dict(progress_details={'unexpected':True})]:
            with self.assertRaises(ValueError):app.operate(self.db,self.command(**kw))
    def test_correction_separate_and_not_counted(self):
        before=events(self.db);out=app.operate(self.db,self.command('07',progress='possible_correction',reason_codes=['訂正・撤回を示す記述'],progress_details=None));self.assertEqual(out['correction'],'possible');self.assertFalse(out['new_progress_counted']);self.assertEqual(events(self.db),before)
    def test_duplicate_report_does_not_count(self):
        out=app.operate(self.db,self.command('06'));self.assertEqual(out['progress_assessment'],'already_recorded_literal_candidate');self.assertFalse(out['new_progress_counted'])
    def test_no_progress_multiple_reasons_persist(self):
        c=self.command('01',progress='none',reason_codes=['既報と同じ内容','新しい事実がない'],progress_details=None);app.operate(self.db,c)
        self.assertEqual(self.state()['work']['completed'][0]['record']['review_details']['reason_codes'],c['reason_codes'])
    def test_rollback_and_retry_have_one_details_record(self):
        c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_review_details').fetchone()[0],0)
        first=app.operate(self.db,c);self.assertEqual(app.operate(self.db,c),first);self.assertEqual(self.db.execute('SELECT count(*) FROM ui_review_details').fetchone()[0],1)
    def test_past_edition_evidence_rejected(self):
        c=self.command();t=self.task();sync.change_source(self.db,'edited-before-save',t['article'],t['source']['state_id'],app.now(),body_text='本文が訂正された')
        with self.assertRaises(ValueError):app.operate(self.db,c)
    def test_occurrence_time_not_inferred_from_publication(self):
        app.operate(self.db,self.command());row=self.state()['work']['completed'][0]['record'];self.assertIsNone(row['event_occurred_at'])
    def test_same_stage_without_duplicate_quote_requires_review(self):
        from engine import dumps
        e=next(e for e in events(self.db) if e['event_id']=='demo-e2')
        e['progress_history']=[{'stage':'起訴','progress_evidence':[{'quote':'別の人物についての起訴'}]}]
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'demo-e2'))
        out=app.operate(self.db,self.command());self.assertEqual(out['progress_assessment'],'needs_evidence');self.assertFalse(out['new_progress_counted']);self.assertEqual(self.task()['category'],'hold')
    def test_mixed_comparison_values_stay_unknown(self):
        t=self.task('10','demo-e2');p=next(p for p in t['evidence_by_event']['demo-e2'] if p['quote'] and p['verification']['status']=='exact_literal_match')
        with self.db:comparison.add_note(self.db,'demo-e2',t['article']['url'],'place','江別',p['evidence_id'])
        t=self.task('10','demo-e2');self.assertEqual(next(r for r in t['comparison_by_event']['demo-e2']['rows'] if r['field']=='place')['status'],'未確認')

if __name__=='__main__':unittest.main()
