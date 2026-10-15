import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_engine as fixtures
from engine import connect,import_snapshot,events,dumps
from event_tracker import TRACKING_RESPONSE
from test_tracking import NOW,report
from revision_sync import (initialize,seed,head,add_evidence,record_reference,change_source,
    verify_quote,make_job,call_mock,adopt,cancel,job_current,refresh_links)

class RevisionSyncTests(unittest.TestCase):
    def setUp(self):
        fixtures.PrototypeTests.setUp(self);initialize(self.db)
        self.article=self.g['articles'][0];self.url=self.article['url']
        self.body='SYNTHETIC 原文。金額は100万円。別件B。'
        seed(self.db,self.e['articles'][0]);seed(self.db,self.article,body_text=self.body)
    tearDown=fixtures.PrototypeTests.tearDown
    write=fixtures.PrototypeTests.write
    def evidence(self,event_id='e1',field='body',url=None,quote='100万円'):
        return add_evidence(self.db,event_id,url or self.url,field,quote if field=='body' else None)
    def reference(self,name,evidence=None,kind='ai',event_id='e1',outcome='rejected',supersedes=None):
        ids=evidence or [self.evidence(event_id)]
        return record_reference(self.db,name,event_id,kind,outcome,{'basis':'synthetic'},ids,NOW,supersedes)['decision_id']
    def status(self,did):return self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(did,)).fetchone()[0]
    def answer(self,article=None,**values):
        result=copy.deepcopy(TRACKING_RESPONSE);result.update(report(article or self.article,**values));return result
    def job(self,group=None,evidence=None):return make_job(self.db,'e1',group or self.g,evidence or [self.evidence()],NOW)
    def budget(self):
        row=self.db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone();return row[0] if row else None
    def other_event(self):
        other=copy.deepcopy(self.e);other['event_id']='e2';other['articles'][0]['url']='https://example.test/other'
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',('e2',dumps(other)))
        seed(self.db,other['articles'][0],body_text='SYNTHETIC unrelated original')
        return other
    def test_body_change_invalidates_only_dependent_decisions(self):
        other=self.other_event();a=self.reference('a');b=self.reference('b',[self.evidence('e2',url=other['articles'][0]['url'])],event_id='e2')
        original=self.db.execute('SELECT payload FROM sync_decisions WHERE decision_id=?',(b,)).fetchone()[0]
        changed=change_source(self.db,'body',self.article,head(self.db,self.url)['state_id'],NOW,body_text=self.body.replace('100','120'))
        self.assertEqual(changed['affected_decisions'],[a]);self.assertEqual(changed['affected_events'],['e1'])
        self.assertEqual(self.status(a),'needs_recheck');self.assertEqual(self.status(b),'current')
        self.assertEqual(self.db.execute('SELECT payload FROM sync_decisions WHERE decision_id=?',(b,)).fetchone()[0],original)
    def test_title_only_does_not_invalidate_body_only_reference(self):
        body=self.reference('body');title=self.reference('title',[self.evidence(field='title')]);article=copy.deepcopy(self.article);article['title']+=' SYNTHETIC見出し訂正'
        changed=change_source(self.db,'change-title',article,head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(changed['affected_decisions'],[title]);self.assertEqual(self.status(body),'current')
    def test_publication_time_only_affects_date_reference(self):
        body=self.reference('body');date=self.reference('date',[self.evidence(field='published')]);article=copy.deepcopy(self.article);article['published']='2026-10-09T00:00:00Z'
        changed=change_source(self.db,'change-date',article,head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(changed['affected_decisions'],[date]);self.assertEqual(self.status(body),'current')
    def test_shared_article_marks_both_event_decisions_without_merging(self):
        self.other_event();a=self.reference('a');b=self.reference('b',[self.evidence('e2')],event_id='e2');before=events(self.db)
        result=change_source(self.db,'shared',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC corrected two-event text')
        self.assertEqual(set(result['affected_events']),{'e1','e2'});self.assertEqual(set(result['affected_decisions']),{a,b})
        self.assertEqual(events(self.db),before)
    def test_operator_history_requires_explicit_reconfirmation(self):
        old=self.reference('operator',kind='operator',outcome='same_event')
        change_source(self.db,'revise',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC corrected')
        self.assertEqual(self.status(old),'needs_recheck')
        new=self.reference('operator-new',[self.evidence(quote='corrected')],kind='operator',outcome='same_event',supersedes=old)
        self.assertEqual(self.status(old),'superseded');self.assertEqual(self.status(new),'current')
        self.assertEqual(self.db.execute("SELECT count(*) FROM sync_decisions WHERE kind='operator'").fetchone()[0],2)
    def test_pending_operator_cannot_be_overridden_by_ai(self):
        old=self.reference('operator',kind='operator',outcome='same_event')
        change_source(self.db,'revise',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC corrected')
        ev=self.evidence(quote='corrected');job=self.job(evidence=[ev]);call_mock(self.db,job,lambda p:self.answer(),NOW)
        self.assertEqual(adopt(self.db,'blocked',job,NOW)['status'],'operator_reconfirmation_required')
        self.reference('confirmed',[ev],kind='operator',outcome='same_event',supersedes=old)
        self.assertEqual(adopt(self.db,'allowed',job,NOW)['status'],'accepted')
    def test_transport_failure_preserves_head_cache_and_decisions(self):
        did=self.reference('a');before=head(self.db,self.url)
        result=change_source(self.db,'timeout',self.article,before['state_id'],NOW,outcome='request_failed',error='SYNTHETIC timeout')
        self.assertEqual(head(self.db,self.url),before);self.assertEqual(self.status(did),'current');self.assertTrue(result['transport_retry'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_fetch_waits').fetchone()[0],1)
    def test_deleted_source_preserves_historical_quote_and_clears_no_facts(self):
        ev=self.evidence();did=self.reference('a',[ev]);before=events(self.db)
        change_source(self.db,'deleted',self.article,head(self.db,self.url)['state_id'],NOW,outcome='deleted')
        self.assertEqual(self.status(did),'needs_recheck');self.assertEqual(verify_quote(self.db,ev)['status'],'exact_literal_match')
        self.assertEqual(verify_quote(self.db,ev,current=True)['status'],'stale_source');self.assertEqual(events(self.db),before)
    def test_empty_body_is_not_a_failed_fetch_or_deleted_article(self):
        did=self.reference('a');change_source(self.db,'empty',self.article,head(self.db,self.url)['state_id'],NOW,outcome='empty_body')
        self.assertEqual(head(self.db,self.url)['availability'],'empty_body');self.assertEqual(self.status(did),'needs_recheck')
        self.assertEqual(self.db.execute("SELECT count(*) FROM sep_fetch_observations WHERE result='empty_body'").fetchone()[0],1)
    def test_metadata_update_without_body_does_not_assume_reobserved_body(self):
        ev=self.evidence();did=self.reference('a',[ev]);article=copy.deepcopy(self.article);article['title']+='訂正'
        change_source(self.db,'metadata-only',article,head(self.db,self.url)['state_id'],NOW)
        self.assertIsNone(head(self.db,self.url)['components']['body']);self.assertEqual(self.status(did),'needs_recheck')
        self.assertEqual(verify_quote(self.db,ev)['status'],'exact_literal_match')
        self.assertEqual(verify_quote(self.db,self.evidence())['status'],'unverified_body_missing')
    def test_literal_verification_is_version_bound_and_does_not_normalize(self):
        exact=self.evidence();mismatch=self.evidence(quote='１００万円')
        self.assertEqual(verify_quote(self.db,exact)['status'],'exact_literal_match');self.assertEqual(verify_quote(self.db,mismatch)['status'],'literal_mismatch')
        self.assertFalse(verify_quote(self.db,exact)['gold_promoted']);self.assertFalse(verify_quote(self.db,exact)['source_verified'])
        change_source(self.db,'body',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC 120万円')
        fresh=self.evidence(quote='100万円');self.assertEqual(verify_quote(self.db,fresh)['status'],'literal_mismatch')
        self.assertEqual(verify_quote(self.db,exact)['status'],'exact_literal_match')
    def test_missing_original_body_stays_unverified(self):
        url=self.e['articles'][0]['url'];ev=self.evidence(url=url)
        self.assertEqual(verify_quote(self.db,ev)['status'],'unverified_body_missing')
    def test_source_update_failures_roll_back_all_projections_and_invalidation(self):
        for phase in ['after_version','after_head','after_invalidation','before_receipt']:
            did=self.reference('ref-'+phase);before=head(self.db,self.url);counts=self.db.execute('SELECT count(*) FROM sync_source_states').fetchone()[0]
            with self.assertRaises(RuntimeError):change_source(self.db,'fail-'+phase,self.article,before['state_id'],NOW,body_text='SYNTHETIC update',failpoint=phase)
            self.assertEqual(head(self.db,self.url),before);self.assertEqual(self.status(did),'current')
            self.assertEqual(self.db.execute('SELECT count(*) FROM sync_source_states').fetchone()[0],counts)
    def test_source_commit_receipt_recovers_lost_ack_without_duplicate_history(self):
        old=head(self.db,self.url)['state_id']
        with self.assertRaises(RuntimeError):change_source(self.db,'source-op',self.article,old,NOW,body_text='SYNTHETIC new',failpoint='after_commit')
        counts=self.db.execute('SELECT count(*) FROM sync_source_states').fetchone()[0]
        result=change_source(self.db,'source-op',self.article,old,NOW,body_text='SYNTHETIC new')
        self.assertEqual(result['state_id'],head(self.db,self.url)['state_id']);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_source_states').fetchone()[0],counts)
        with self.assertRaises(ValueError):change_source(self.db,'source-op',self.article,old,NOW,body_text='different intent')
        with self.assertRaises(ValueError):change_source(self.db,'old-head-op',self.article,old,NOW,body_text='SYNTHETIC stale write')
    def test_guarded_adoption_cancellation_and_cached_readoption(self):
        before=events(self.db);job=self.job();calls=[]
        call_mock(self.db,job,lambda p:(calls.append(1) or self.answer()),NOW);accepted=adopt(self.db,'adopt',job,NOW);budget=self.budget()
        self.assertEqual(accepted['status'],'accepted');self.assertEqual(events(self.db)[0]['progress_count'],1)
        self.assertEqual(adopt(self.db,'adopt',job,NOW),accepted);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
        cancel(self.db,'cancel','adopt',NOW);self.assertEqual(events(self.db),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_event_holds').fetchone()[0],1)
        new_job=self.job();call_mock(self.db,new_job,lambda p:self.fail('response must be reused'),NOW)
        readopted=adopt(self.db,'readopt',new_job,NOW);self.assertEqual(readopted['status'],'accepted')
        self.assertEqual(events(self.db)[0]['progress_count'],1);self.assertEqual(self.budget(),budget)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_event_holds').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],2)
    def test_article_correction_then_cancel_then_readopt_keeps_revision_history(self):
        first_job=self.job();call_mock(self.db,first_job,lambda p:self.answer(),NOW);first=adopt(self.db,'first',first_job,NOW)
        corrected=copy.deepcopy(self.article);corrected['description']='SYNTHETIC 訂正記事の追加情報'
        change_source(self.db,'correct',corrected,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC 金額は120万円。')
        group=copy.deepcopy(self.g);group['articles']=[corrected];ev=self.evidence(quote='120万円')
        second_job=self.job(group,[ev]);call_mock(self.db,second_job,lambda p:self.answer(corrected,meaningful_change=False),NOW)
        second=adopt(self.db,'second',second_job,NOW,supersedes=first['decision_id']);self.assertEqual(second['status'],'accepted')
        self.assertTrue(events(self.db)[0].get('source_revisions'));budget=self.budget()
        cancel(self.db,'cancel-second','second',NOW);self.assertEqual(self.status(second['decision_id']),'cancelled')
        third_job=self.job(group,[ev]);call_mock(self.db,third_job,lambda p:self.fail('cache expected'),NOW)
        self.assertEqual(adopt(self.db,'third',third_job,NOW)['status'],'accepted');self.assertEqual(self.budget(),budget)
        self.assertTrue(events(self.db)[0].get('source_revisions'));self.assertEqual(events(self.db)[0]['event_id'],'e1')
    def test_adoption_save_failure_reuses_cached_answer_and_budget(self):
        job=self.job();call_mock(self.db,job,lambda p:self.answer(),NOW);budget=self.budget();before=events(self.db)
        for phase in ['after_guard','after_links','before_receipt']:
            with self.assertRaises(RuntimeError):adopt(self.db,'fail-'+phase,job,NOW,failpoint=phase)
            self.assertEqual(events(self.db),before);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
        self.assertEqual(adopt(self.db,'success',job,NOW)['status'],'accepted');self.assertEqual(self.budget(),budget)
    def test_adoption_committed_before_ack_is_not_applied_twice(self):
        job=self.job();call_mock(self.db,job,lambda p:self.answer(),NOW)
        with self.assertRaises(RuntimeError):adopt(self.db,'ack',job,NOW,failpoint='after_commit')
        accepted=adopt(self.db,'ack',job,NOW);self.assertEqual(accepted['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM changes').fetchone()[0],1);self.assertEqual(events(self.db)[0]['progress_count'],1)
    def test_cancellation_failure_rolls_back_event_and_history(self):
        job=self.job();call_mock(self.db,job,lambda p:self.answer(),NOW);adopt(self.db,'adopt',job,NOW);before=events(self.db)
        for phase in ['after_undo','after_links','before_receipt']:
            with self.assertRaises(RuntimeError):cancel(self.db,'cancel-'+phase,'adopt',NOW,failpoint=phase)
            self.assertEqual(events(self.db),before);self.assertEqual(self.db.execute('SELECT cancelled FROM sync_adoptions').fetchone()[0],0)
        with self.assertRaises(RuntimeError):cancel(self.db,'cancel-ok','adopt',NOW,failpoint='after_commit')
        self.assertEqual(cancel(self.db,'cancel-ok','adopt',NOW)['status'],'cancelled')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_transitions').fetchone()[0],1)
    def test_rejected_same_answer_does_not_repeat_validation(self):
        job=self.job();call_mock(self.db,job,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW)
        first=adopt(self.db,'bad1',job,NOW);self.assertNotEqual(first['status'],'accepted');budget=self.budget()
        count=self.db.execute('SELECT count(*) FROM decisions').fetchone()[0]
        with patch('revision_sync.judge',side_effect=AssertionError('must not revalidate')):
            second=adopt(self.db,'bad2',job,NOW)
        self.assertTrue(second['memo_reused']);self.assertEqual(self.budget(),budget)
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0],count)
    def test_body_revision_reopens_rejected_job_without_erasing_old_response(self):
        ev=self.evidence();job=self.job(evidence=[ev]);call_mock(self.db,job,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW);bad=adopt(self.db,'bad',job,NOW)
        change_source(self.db,'body',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC 120万円')
        self.assertEqual(self.status(bad['decision_id']),'needs_recheck');self.assertFalse(job_current(self.db,job))
        new=self.job(evidence=[self.evidence(quote='120万円')]);self.assertNotEqual(job,new)
        call_mock(self.db,new,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0],2)
    def test_unrelated_cached_job_remains_identical_and_current(self):
        other=self.other_event();group=copy.deepcopy(self.g);group['articles'][0]['url']='https://example.test/new-other'
        seed(self.db,group['articles'][0]);ev=add_evidence(self.db,'e2',other['articles'][0]['url'],'body','unrelated')
        job=make_job(self.db,'e2',group,[ev],NOW);call_mock(self.db,job,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW)
        cached=self.db.execute('SELECT * FROM sync_answer_cache').fetchall();change_source(self.db,'e1-body',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC other content')
        self.assertTrue(job_current(self.db,job));self.assertEqual(self.db.execute('SELECT * FROM sync_answer_cache').fetchall(),cached)
        self.assertEqual(call_mock(self.db,job,lambda p:self.fail('cached'),NOW)['status'],'cached')
    def test_budget_reservation_and_response_commit_failures_do_not_double_charge(self):
        job=self.job()
        with self.assertRaises(RuntimeError):call_mock(self.db,job,lambda p:self.answer(),NOW,failpoint='after_reserve')
        budget=self.budget();calls=[]
        with self.assertRaises(RuntimeError):call_mock(self.db,job,lambda p:(calls.append(1) or self.answer()),NOW,failpoint='after_cache')
        self.assertEqual(call_mock(self.db,job,lambda p:self.fail('must not call'),NOW)['status'],'cached')
        self.assertEqual(len(calls),1);self.assertEqual(self.budget(),budget)
    def test_uncertain_response_gap_never_automatically_calls_again(self):
        job=self.job();calls=[]
        with self.assertRaises(RuntimeError):call_mock(self.db,job,lambda p:(calls.append(1) or self.answer()),NOW,failpoint='after_response_before_cache')
        budget=self.budget();result=call_mock(self.db,job,lambda p:self.fail('uncertain must stop'),NOW)
        self.assertEqual(result['status'],'uncertain_response_requires_reconciliation');self.assertEqual(len(calls),1);self.assertEqual(self.budget(),budget)
    def test_budget_exhaustion_keeps_job_evidence_and_source_heads(self):
        job=self.job();before=head(self.db,self.url)
        with patch('revision_sync.reserve_mock',return_value={'status':'budget_deferred'}):
            self.assertEqual(call_mock(self.db,job,lambda p:self.fail('no budget'),NOW)['status'],'budget_deferred')
        self.assertEqual(head(self.db,self.url),before);self.assertTrue(job_current(self.db,job));self.assertIsNone(self.budget())
    def test_revision_during_mock_keeps_reply_but_blocks_stale_adoption(self):
        job=self.job()
        def response(prompt):
            change_source(self.db,'during-call',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC changed during response')
            return self.answer()
        call_mock(self.db,job,response,NOW);self.assertEqual(adopt(self.db,'blocked',job,NOW)['status'],'needs_recheck')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0],1)
    def test_guard_failure_does_not_adopt_body_quote_as_incident_identity(self):
        job=self.job();bad=self.answer();bad['identity_matches']=[]
        call_mock(self.db,job,lambda p:bad,NOW);before=events(self.db)
        self.assertEqual(adopt(self.db,'bad',job,NOW)['status'],'needs_evidence');self.assertEqual(events(self.db),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
    def test_superseding_different_event_operator_is_rejected_atomically(self):
        self.other_event();old=self.reference('old',kind='operator',outcome='hold');count=self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0]
        with self.assertRaises(ValueError):self.reference('wrong',[self.evidence('e2')],kind='operator',event_id='e2',supersedes=old)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],count)
    def test_disk_reopen_after_cached_response_and_after_commit(self):
        self.db.close();path=self.root/'sync.db';self.db=connect(str(path));import_snapshot(self.db,self.root);initialize(self.db)
        seed(self.db,self.e['articles'][0]);seed(self.db,self.article,body_text=self.body);job=self.job()
        with self.assertRaises(RuntimeError):call_mock(self.db,job,lambda p:self.answer(),NOW,failpoint='after_cache')
        budget=self.budget();self.db.close();self.db=connect(str(path))
        self.assertEqual(call_mock(self.db,job,lambda p:self.fail('cached after reopen'),NOW)['status'],'cached')
        with self.assertRaises(RuntimeError):adopt(self.db,'disk',job,NOW,failpoint='after_commit')
        self.db.close();self.db=connect(str(path));self.assertEqual(adopt(self.db,'disk',job,NOW)['status'],'accepted')
        self.assertEqual(self.budget(),budget);self.assertEqual(self.db.execute('SELECT count(*) FROM changes').fetchone()[0],1)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
    def test_unchanged_content_reuses_failed_memo_even_with_new_observation(self):
        job=self.job();call_mock(self.db,job,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW);adopt(self.db,'bad-before',job,NOW)
        change_source(self.db,'unchanged',self.article,head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        again=self.job();self.assertEqual(again,job)
        with patch('revision_sync.judge',side_effect=AssertionError('unchanged inputs must not be rejudged')):
            self.assertTrue(adopt(self.db,'bad-after',again,NOW)['memo_reused'])
    def test_actual_ai_job_watches_metadata_it_receives(self):
        job=self.job();changed=copy.deepcopy(self.article);changed['title']+=' SYNTHETIC changed heading'
        change_source(self.db,'heading',changed,head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertFalse(job_current(self.db,job))
        self.assertEqual(call_mock(self.db,job,lambda p:self.fail('stale prompt'),NOW)['status'],'needs_recheck')
    def test_reverted_body_gets_new_validation_generation_and_version_bound_response(self):
        job=self.job();call_mock(self.db,job,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW);old=adopt(self.db,'old-result',job,NOW)
        change_source(self.db,'change',self.article,head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC corrected')
        change_source(self.db,'restore',self.article,head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(self.status(old['decision_id']),'needs_recheck');self.assertFalse(job_current(self.db,job))
        fresh=self.job();self.assertNotEqual(fresh,job);budget=self.budget()
        self.assertEqual(call_mock(self.db,fresh,lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW)['status'],'cached')
        # The restored edition has a different generation-bound prompt, so it
        # gets one new reservation. Repeating that same edition gets none.
        after=self.budget()
        self.assertEqual(json.loads(after)['run']['calls'],json.loads(budget)['run']['calls']+1)
        self.assertEqual(call_mock(self.db,fresh,lambda p:self.fail('duplicate restored-edition call'),NOW)['status'],'cached')
        self.assertEqual(self.budget(),after)
    def test_reservation_from_prior_day_cannot_bypass_today_budget(self):
        from datetime import timedelta
        job=self.job()
        with self.assertRaises(RuntimeError):call_mock(self.db,job,lambda p:self.answer(),NOW,failpoint='after_reserve')
        before=self.budget();result=call_mock(self.db,job,lambda p:self.fail('old-day reservation'),NOW+timedelta(days=1))
        self.assertEqual(result['status'],'reservation_period_requires_reconciliation');self.assertEqual(self.budget(),before)
    def test_unrelated_triage_does_not_change_job_identity(self):
        job=self.job();same=make_job(self.db,'e1',self.g,[self.evidence()],NOW,triage={'unrelated-group':{'status':'manual_review'}})
        self.assertEqual(job,same)
    def test_unmatched_or_unavailable_quote_cannot_be_adopted(self):
        for name,evidence in [('mismatch',self.evidence(quote='存在しない引用')),('unknown',self.evidence(url=self.e['articles'][0]['url']))]:
            job=self.job(evidence=[evidence]);call_mock(self.db,job,lambda p:self.answer(),NOW)
            before=events(self.db);result=adopt(self.db,'quote-'+name,job,NOW)
            self.assertEqual(result['status'],'quote_validation_failed');self.assertEqual(events(self.db),before)
    def test_legacy_fact_projection_does_not_destroy_original_fact_history(self):
        with self.db:
            cursor=self.db.execute('INSERT INTO facts(event_id,kind,quote,payload,verification) VALUES(?,?,?,?,?)',
                ('e1','synthetic_legacy_extra','SYNTHETIC original quote','{"unknown_field":"keep"}','imported_not_revalidated'))
            old_id=cursor.lastrowid
        job=self.job();call_mock(self.db,job,lambda p:self.answer(),NOW);adopt(self.db,'adopt-archive',job,NOW)
        rows=[json.loads(row[0]) for row in self.db.execute('SELECT payload FROM sync_fact_archive WHERE original_fact_id=?',(old_id,))]
        self.assertTrue(any(r['quote']=='SYNTHETIC original quote' and r['payload']=='{"unknown_field":"keep"}' for r in rows))
        cancel(self.db,'cancel-archive','adopt-archive',NOW)
        self.assertTrue(self.db.execute('SELECT 1 FROM sync_fact_archive WHERE op_id=?',('adopt-archive',)).fetchone())
    def test_old_cancellation_cannot_discard_a_later_valid_adoption(self):
        job=self.job();call_mock(self.db,job,lambda p:self.answer(),NOW);adopt(self.db,'first',job,NOW)
        article=copy.deepcopy(self.article);article['url']='https://example.test/later-followup';article['title']+=' SYNTHETIC判決'
        seed(self.db,article,body_text=self.body);group=copy.deepcopy(self.g);group['articles']=[article]
        proof=self.evidence(url=article['url']);second=self.job(group,[proof])
        call_mock(self.db,second,lambda p:self.answer(article,current_stage='判決'),NOW)
        self.assertEqual(adopt(self.db,'later',second,NOW)['status'],'accepted');before=events(self.db)
        with self.assertRaisesRegex(ValueError,'later_changes_require_review'):cancel(self.db,'unsafe-cancel','first',NOW)
        self.assertEqual(events(self.db),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions WHERE cancelled=1').fetchone()[0],0)
    def test_corrupted_stored_body_hash_blocks_quote_and_job_creation(self):
        ev=self.evidence();bid=head(self.db,self.url)['body_id']
        with self.db:self.db.execute('UPDATE sep_contents SET payload=? WHERE content_id=?',(dumps({'body_text':'SYNTHETIC corruption'}),bid))
        self.assertEqual(verify_quote(self.db,ev)['status'],'content_hash_mismatch')
        with self.assertRaisesRegex(ValueError,'content hash mismatch'):self.job(evidence=[ev])
