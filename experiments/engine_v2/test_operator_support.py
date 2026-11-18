"""Synthetic draft/provenance and reference-only save regressions."""
import copy
import json
import unittest
import operator_app as app
import operator_support as support
from operator_task_demo import populate
from engine import events
import revision_sync as sync

class SupportTests(unittest.TestCase):
    def setUp(self):self.db=app.connect();app.initialize(self.db);populate(self.db)
    def tearDown(self):self.db.close()
    def state(self):return app.snapshot(self.db)
    def task(self,n='02',eid=None):return next(t for t in self.state()['work']['tasks'] if t.get('article',{}).get('url')=='https://example.test/demo-'+n and (eid is None or t['required_event_id']==eid))
    def draft(self,n='02',eid=None):
        t=self.task(n,eid);return t['drafts_by_event'][eid or t['suggested_event_id']]
    def command(self,n='02',**kw):
        t=self.task(n,kw.pop('required',None));eid=t['suggested_event_id'];d=t['drafts_by_event'][eid]
        c=dict(action='task_review',op='draft-review-operation',revision=self.state()['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=eid,relation='same_event',progress='new',proposed_stage='起訴',reason='提案を確認した参照記録',reason_codes=['新しい事実を引用で確認'],draft_review={'draft_id':d['draft_id'],'applied_fields':['new_fact','difference','proposed_stage']},progress_details=dict(new_fact=d['new_fact'],difference=d['difference'],evidence_ids=d['evidence_ids'],duplicate_checked=True))
        c.update(kw);return c
    def test_pure_snapshot_is_stable_and_makes_no_writes(self):
        t=self.task();event=next(e for e in events(self.db) if e['event_id']==t['suggested_event_id']);before=self.db.total_changes
        first=support.build(self.db,t,event);again=support.build(self.db,t,event);self.assertEqual(first,again);self.assertEqual(before,self.db.total_changes)
    def test_saved_simulated_ai_is_reused_as_proposal_only(self):
        d=self.draft();self.assertEqual(d['method'],'saved_ai_and_literal_rules');self.assertEqual(len(d['reused_ai']),1);self.assertFalse(d['ai_called']);self.assertEqual(d['status'],'提案・未確認')
    def test_no_cached_ai_uses_literal_rules_not_fake_ai(self):
        d=self.draft('01');self.assertEqual(d['method'],'literal_rules_only');self.assertEqual(d['reused_ai'],[])
    def test_no_evidence_produces_no_fact_or_stage(self):
        d=self.draft('05');self.assertFalse(d['new_fact']);self.assertIsNone(d['proposed_stage']);self.assertEqual(d['evidence_ids'],[])
    def test_publication_time_not_used_for_occurrence(self):
        d=self.draft();self.assertIsNone(d['event_occurred_at']);self.assertTrue(all(p['event_occurred_at'] is None for p in d['evidence_cards']))
    def test_source_and_literal_verification_are_separate(self):
        d=self.draft();p=d['evidence_cards'][0];self.assertEqual(p['verification']['status'],'exact_literal_match');self.assertEqual(p['source_verification'],'未検証');self.assertFalse(p['verification']['source_verified'])
    def test_body_missing_remains_unconfirmed(self):
        d=self.draft('08');self.assertTrue(d['evidence_cards']);self.assertTrue(all(not c['body_available'] for c in d['evidence_cards']));self.assertTrue(all(c['field']=='title' for c in d['evidence_cards']))
    def test_shared_article_has_separate_draft_ids_and_quotes(self):
        a=self.draft('10','demo-e2');b=self.draft('10','demo-e3');self.assertNotEqual(a['draft_id'],b['draft_id'])
        self.assertTrue(all('被害者佐藤' not in p['quote'] for p in a['evidence_cards']));self.assertTrue(all('被害者山田' not in p['quote'] for p in b['evidence_cards']))
    def test_duplicate_candidate_does_not_increment_progress(self):
        d=self.draft('06');self.assertTrue(d['duplicate_candidates']);before=events(self.db);out=app.operate(self.db,self.command('06'));self.assertFalse(out['new_progress_counted']);self.assertEqual(events(self.db),before)
    def test_stage_word_is_not_treated_as_proved_progress(self):
        before=events(self.db);d=self.draft('01');self.assertEqual(d['proposed_stage'],'逮捕');self.assertEqual(events(self.db),before)
    def test_operator_edits_and_original_proposal_are_separate(self):
        c=self.command();c['progress_details']['difference']='運営者が修正した違い';app.operate(self.db,c)
        r=self.state()['work']['completed'][0]['record']['review_details']['draft_review'];self.assertNotEqual(r['original']['difference'],r['operator_values']['difference']);self.assertEqual(r['operator_values']['difference'],'運営者が修正した違い')
    def test_review_save_never_adopts_merges_or_approves(self):
        before=events(self.db);approvals=self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0];adoptions=self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0]
        out=app.operate(self.db,self.command());self.assertFalse(out['published']);self.assertFalse(out['adopted']);self.assertEqual(events(self.db),before);self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],approvals);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],adoptions)
    def test_wrong_draft_id_rejected(self):
        c=self.command();c['draft_review']['draft_id']='old'
        with self.assertRaisesRegex(ValueError,'下書き'):app.operate(self.db,c)
    def test_foreign_shared_event_draft_rejected(self):
        c=self.command('10',required='demo-e2');c['draft_review']['draft_id']=self.draft('10','demo-e3')['draft_id']
        with self.assertRaises(ValueError):app.operate(self.db,c)
    def test_unknown_applied_fields_rejected(self):
        c=self.command();c['draft_review']['applied_fields']=['relation','duplicate_checked']
        with self.assertRaises(ValueError):app.operate(self.db,c)
    def test_new_article_edition_invalidates_saved_ai_proposal(self):
        t=self.task();old=self.draft();sync.change_source(self.db,'draft-body-edit',t['article'],t['source']['state_id'],app.now(),body_text='改訂本文')
        new=self.draft();self.assertNotEqual(old['draft_id'],new['draft_id']);self.assertEqual(new['reused_ai'],[])
    def test_old_command_after_revision_rejected(self):
        c=self.command();t=self.task();sync.change_source(self.db,'draft-title-edit',dict(t['article'],title='訂正後の架空見出し'),t['source']['state_id'],app.now())
        with self.assertRaises(ValueError):app.operate(self.db,c)
    def test_cancelled_ai_not_reused(self):
        did=self.draft()['reused_ai'][0]['decision_id']
        with self.db:self.db.execute("UPDATE sync_decision_state SET status='cancelled' WHERE decision_id=?",(did,))
        self.assertEqual(self.draft()['reused_ai'],[])
    def test_failed_ai_answer_not_reused(self):
        did=self.draft()['reused_ai'][0]['decision_id']
        with self.db:self.db.execute("UPDATE sync_decisions SET outcome='invalid_answer_schema' WHERE decision_id=?",(did,))
        self.assertEqual(self.draft()['reused_ai'],[])
    def test_missing_duplicate_confirmation_keeps_hold(self):
        c=self.command();c['progress_details']['duplicate_checked']=False;out=app.operate(self.db,c);self.assertEqual(out['progress_assessment'],'needs_evidence');self.assertEqual(self.task()['category'],'hold')
    def test_missing_selected_quote_keeps_hold(self):
        c=self.command();c['progress_details']['evidence_ids']=[];self.assertEqual(app.operate(self.db,c)['progress_assessment'],'needs_evidence')
    def test_no_progress_subtypes_are_distinct(self):
        from operator_comparison import review_details
        for label,code in support.NO_PROGRESS.items():
            t=self.task('01');c=self.command('01',progress='none',reason_codes=[label],progress_details=None);d=review_details(c);support.audit(t,c,d);self.assertEqual(d['no_progress_types'],[code])
    def test_failure_rolls_back_audit_and_retry_is_idempotent(self):
        c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_review_details').fetchone()[0],0)
        out=app.operate(self.db,c);self.assertEqual(app.operate(self.db,c),out);self.assertEqual(self.db.execute('SELECT count(*) FROM ui_review_details').fetchone()[0],1)
    def test_correction_remains_separate_not_new_progress(self):
        out=app.operate(self.db,self.command('07',progress='possible_correction',reason_codes=['訂正・撤回を示す記述'],progress_details=None));self.assertEqual(out['correction'],'possible');self.assertFalse(out['new_progress_counted']);self.assertEqual(self.task('07')['category'],'hold')
    def test_explicit_occurrence_annotation_is_not_publication_time(self):
        import operator_comparison as comparison
        url='https://example.test/time-source';article=dict(url=url,title='架空の記事',published='2026-10-08T11:00:00+00:00');quote='発生日時は2026-10-01T10:00:00+09:00と記載'
        sync.seed(self.db,article,body_text=quote);proof=sync.add_evidence(self.db,'demo-e2',url,'body',quote)
        with self.db:comparison.add_note(self.db,'demo-e2',url,'occurred_at','2026-10-01T10:00:00+09:00',proof)
        card=support.cards(self.db,'demo-e2',url)[0];self.assertEqual(card['event_occurrence_text'],'2026-10-01T10:00:00+09:00');self.assertNotEqual(card['event_occurrence_text'],card['article']['published']);self.assertEqual(card['source_verification'],'未検証')
    def test_same_event_ai_citing_other_url_not_reused(self):
        did=self.draft()['reused_ai'][0]['decision_id'];payload={'current_stage':'起訴','progress_evidence':[{'url':'https://example.test/different','quote':self.draft()['new_fact']}]}
        with self.db:self.db.execute('UPDATE sync_decisions SET payload=? WHERE decision_id=?',(json.dumps(payload),did))
        self.assertEqual(self.draft()['reused_ai'],[])
    def test_title_only_revision_does_not_reuse_old_ai_edition(self):
        t=self.task();old=self.draft();sync.change_source(self.db,'only-heading-changed',dict(t['article'],title=t['article']['title']+'【改訂】'),t['source']['state_id'],app.now())
        self.assertNotEqual(self.draft()['draft_id'],old['draft_id']);self.assertEqual(self.draft()['reused_ai'],[])

if __name__=='__main__':unittest.main()
