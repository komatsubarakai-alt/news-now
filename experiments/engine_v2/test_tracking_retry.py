"""Fictional-only retry contexts, restart, fault and B-plan integration tests."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent)]
from engine import connect
from test_status_cache_replay import NOW,payload
from test_shadow_priority import fixtures,tracking_job
import tracking_retry as r
import shadow_priority as p

EVENT={'event_id':'fictional-event','event_name':'架空施設','articles':[{'url':'https://example.test/old','title':'架空施設の既報','published':'2025-12-01T00:00:00Z'}],'schedules':[
    {'status':'scheduled','due_start':'2026-01-03','due_end':'2026-01-03'}]}
ARTICLE={'url':'https://example.test/a','title':'架空施設の進展','body':'架空の原文。',
         'published':'2026-01-01T00:00:00Z','article_version_id':'fictional-v1'}
SEARCH={'query':'架空施設','window_start':'2025-12-01','window_end':'2026-01-02'}


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.db=connect();self.event=copy.deepcopy(EVENT);self.articles=[copy.deepcopy(ARTICLE)]
        self.net=patch('socket.socket.connect',side_effect=AssertionError('network forbidden'));self.net.start()
        self.http=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden'));self.http.start()
    def tearDown(self):self.net.stop();self.http.stop();self.db.close()
    def context(self,stage='schedule_analysis',**kw):
        opts=dict(search=SEARCH,validator_id='fictional-validator-v1');opts.update(kw)
        return r.make_context(stage,self.event,self.articles,NOW,**opts)
    def remember(self,cause='evidence',context=None,**kw):
        opts=dict(operation_id='failure1',context=context or self.context(),cause=cause,now=NOW);opts.update(kw)
        return r.record(self.db,**opts)
    def test_classify_known_categories(self):
        for reason,cause in [('daily_budget_deferred','amount_daily'),('stage_budget_deferred','calls_stage'),
                             ('article_identity_unproved:https://example.test','evidence'),('no_results','no_results'),
                             ('JSONDecodeError','format'),('HTTPError','transport'),('same_rejected_answer','rejected_unchanged')]:
            self.assertEqual(r.classify(reason),cause)
    def test_ambiguous_legacy_not_guessed(self):
        for reason in ['ValueError','BudgetExceeded','budget_deferred','call_budget_deferred','unselected_search_candidates']:
            self.assertEqual(r.classify(reason),'unknown')
    def test_legacy_never_suppresses(self):
        r.import_legacy(self.db,[{'stage':'schedule_search','reason':'ValueError','payload':{'secret':'not logged'}}])
        self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
        self.assertEqual(self.db.execute('SELECT cause FROM tracking_retry_legacy').fetchone()[0],'unknown')
        self.assertEqual(r.gate(self.db,None,NOW)['basis'],'legacy_context_missing')
    def test_same_evidence_suppresses(self):
        self.remember();self.assertFalse(r.gate(self.db,self.context(),NOW+timedelta(hours=12))['allowed'])
    def test_same_format_suppresses(self):
        self.remember('format');self.assertFalse(r.gate(self.db,self.context(),NOW+timedelta(hours=6))['allowed'])
    def test_different_target_reopens(self):
        self.remember();self.event['event_id']='another-fictional-event';self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_body_revision_reopens(self):
        self.remember();self.articles[0]['body']='訂正された架空原文';self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_version_id_change_reopens(self):
        self.remember();self.articles[0]['article_version_id']='v2';self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_new_article_reopens(self):
        self.remember();self.articles.append({**ARTICLE,'url':'https://example.test/b'});self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_search_query_change_reopens(self):
        self.remember();self.assertTrue(r.gate(self.db,self.context(search={**SEARCH,'query':'新しい架空検索語'}),NOW)['allowed'])
    def test_search_window_change_reopens(self):
        self.remember();self.assertTrue(r.gate(self.db,self.context(search={**SEARCH,'window_end':'2026-01-03'}),NOW)['allowed'])
    def test_missing_search_window_is_not_invented(self):
        with self.assertRaises(ValueError):self.context(search={'query':'架空施設'})
    def test_schedule_date_change_reopens(self):
        self.remember();self.event['schedules'][0]['due_start']='2026-01-04';self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_schedule_due_phase_reopens_without_event_change(self):
        ctx=self.context();self.remember(context=ctx)
        later=NOW+timedelta(days=1)
        ctx2=r.make_context('schedule_analysis',self.event,self.articles,later,search=SEARCH,validator_id='fictional-validator-v1')
        self.assertTrue(r.gate(self.db,ctx2,later)['allowed'])
    def test_old_manifest_cannot_hide_due_boundary(self):
        ctx=self.context();self.remember(context=ctx);later=NOW+timedelta(days=1)
        self.assertEqual(r.gate(self.db,ctx,later)['basis'],'schedule_boundary_changed')
        with self.assertRaises(ValueError):r.record(self.db,'stale',ctx,'format',later)
    def test_validator_change_reopens(self):
        self.remember();self.assertTrue(r.gate(self.db,self.context(validator_id='v2'),NOW)['allowed'])
    def test_operator_change_reopens(self):
        self.remember();self.assertTrue(r.gate(self.db,self.context(operator={'decision':'recheck'}),NOW)['allowed'])
    def test_bookkeeping_changes_do_not_reopen(self):
        self.remember();self.event.update(backfill={'attempts':3},last_schedule_search_at=NOW.isoformat(),search_query='cached query')
        self.assertFalse(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_transport_recovers_after_cooldown(self):
        self.remember('transport');self.assertFalse(r.gate(self.db,self.context(),NOW+timedelta(minutes=14))['allowed'])
        self.assertTrue(r.gate(self.db,self.context(),NOW+timedelta(minutes=15))['allowed'])
    def test_transport_backoff_is_bounded(self):
        self.event['schedules']=[]
        ctx=self.context();at=NOW
        for i in range(10):
            r.record(self.db,'transport'+str(i),ctx,'transport',at)
            after=self.db.execute('SELECT retry_after FROM tracking_retry_attempts ORDER BY id DESC').fetchone()[0]
            self.assertLessEqual((r.date_time(after)-at).total_seconds(),6*3600);at=r.date_time(after)
        self.assertTrue(r.gate(self.db,ctx,at)['allowed'])
    def test_unknown_has_finite_cooldown(self):
        self.remember('unknown');self.assertTrue(r.gate(self.db,self.context(),NOW+timedelta(hours=1))['allowed'])
    def test_no_results_is_not_completion_and_can_search_again(self):
        self.remember('no_results');self.assertFalse(r.gate(self.db,self.context(),NOW+timedelta(hours=5))['allowed'])
        self.assertTrue(r.gate(self.db,self.context(),NOW+timedelta(hours=6))['allowed']);self.assertNotIn('lifecycle',self.event)
    def test_budget_recovers_with_resources(self):
        self.remember('amount_daily');self.assertTrue(r.gate(self.db,self.context(),NOW,resources_available=True)['allowed'])
    def test_month_budget_recovers_next_month(self):
        self.event['schedules']=[]
        self.remember('amount_monthly');self.assertTrue(r.gate(self.db,self.context(),NOW.replace(month=2))['allowed'])
    def test_daily_calls_reset_on_jst_day(self):
        self.event['schedules']=[]
        self.remember('calls_daily');self.assertTrue(r.gate(self.db,self.context(),NOW+timedelta(days=1))['allowed'])
    def test_run_call_limit_not_permanent(self):
        self.remember('calls_run',run_id='run1');self.assertTrue(r.gate(self.db,self.context(),NOW,run_id='run2')['allowed'])
    def test_processing_slots_do_not_block_retry(self):
        self.remember('processing_slots');self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_duplicate_failure_does_not_increment(self):
        self.remember();self.assertTrue(self.remember()['operation_replayed'])
        result=self.remember(operation_id='another');self.assertTrue(result['suppressed'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM tracking_retry_attempts').fetchone()[0],1)
    def test_operation_conflict_rejected(self):
        self.remember()
        with self.assertRaises(ValueError):self.remember(cause='format')
    def test_failure_rollback(self):
        with self.assertRaises(RuntimeError):self.remember(fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM tracking_retry_attempts').fetchone()[0],0)
        self.assertTrue(self.remember()['recorded'])
    def test_restart_retains_suppression(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'retry.sqlite';db=connect(path);ctx=self.context();r.record(db,'restart',ctx,'evidence',NOW);db.close()
            db=connect(path);self.assertFalse(r.gate(db,ctx,NOW)['allowed']);db.close()
    def test_record_has_no_raw_answer_or_prompt(self):
        secret={'status':'failed','text':'SECRET-ANSWER'};self.remember(payload=secret)
        stored=self.db.execute('SELECT * FROM tracking_retry_attempts').fetchone();self.assertNotIn('SECRET-ANSWER',str(stored))
        self.assertNotIn('架空の原文',str(self.db.execute('SELECT manifest FROM retry_manifests').fetchone()))
    def test_tampered_context_rejected(self):
        ctx=self.context();ctx['event_id']='tampered'
        with self.assertRaises(ValueError):r.gate(self.db,ctx,NOW)
    def test_different_stages_are_separate(self):
        self.remember();self.assertTrue(r.gate(self.db,self.context(stage='backfill_analysis'),NOW)['allowed'])
    def test_amount_observations_not_charged(self):
        self.remember(reservation_micro=10000,usage_micro=1000)
        self.assertEqual(self.db.execute('SELECT count(*) FROM budget').fetchone()[0],0)
    def test_no_adoption_or_source_mutation(self):
        before=copy.deepcopy((self.event,self.articles));self.remember();self.assertEqual(before,(self.event,self.articles))
        for table in ('events','progress','decisions','budget'):self.assertEqual(self.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
    def test_tracking_plan_applies_suppression_without_using_slots(self):
        ctx=self.context();self.remember(context=ctx)
        due=tracking_job('due',[100,200],event_id=self.event['event_id'],retry_context=ctx)
        normal=tracking_job('normal')
        result=p.plan_tracking([due],[normal],{},NOW,retry_db=self.db)
        self.assertEqual(result['selected'],['normal']);self.assertEqual(result['reserved_calls'],1)
        self.assertEqual(result['wait_reasons']['due'],['retry_suppressed','evidence'])
    def test_wrong_target_cannot_inherit_suppression(self):
        job=tracking_job('wrong',[100],event_id='wrong',retry_context=self.context())
        with self.assertRaises(ValueError):p.plan_tracking([job],[],{},NOW,retry_db=self.db)
    def test_legacy_job_keeps_minimum_schedule_slot(self):
        result=p.plan_tracking([tracking_job('due',[100,200])],[tracking_job('normal')],{},NOW,retry_db=self.db)
        self.assertEqual(result['due_selected'],1);self.assertEqual(result['retry_decisions']['due']['basis'],'legacy_context_missing')
    def test_changed_schedule_restores_minimum_slot(self):
        self.remember();self.event['schedules'][0]['due_start']='2026-01-04'
        job=tracking_job('due',[100,200],event_id=self.event['event_id'],retry_context=self.context())
        self.assertEqual(p.plan_tracking([job],[tracking_job('normal')],{},NOW,retry_db=self.db)['due_selected'],1)
    def test_schedule_adapter_missing_answer_unknown(self):
        result=r.replay_schedule(self.db,self.context(),self.event,self.articles,None,None,NOW,operation_id='replay')
        self.assertEqual(result['cause'],'unknown');self.assertFalse(result['can_publish'])
    def test_schedule_adapter_empty_results_no_completion(self):
        self.articles=[];ctx=self.context()
        result=r.replay_schedule(self.db,ctx,self.event,[],None,None,NOW,operation_id='replay')
        self.assertEqual(result['cause'],'no_results');self.assertNotIn('completed_at',self.event)
    def test_schedule_adapter_uses_existing_incomplete_guard(self):
        result=r.replay_schedule(self.db,self.context(),self.event,self.articles,{'confidence':'high','matches':[{'url':ARTICLE['url'],'confidence':'high'}]},
                                 {'status':'incomplete'},NOW,operation_id='replay')
        self.assertEqual(result['cause'],'format')
    def test_adapter_failure_rolls_back_diagnostics(self):
        with self.assertRaises(RuntimeError):r.replay_schedule(self.db,self.context(),self.event,self.articles,None,None,NOW,operation_id='x',fail_save=True)
        self.assertTrue(r.gate(self.db,self.context(),NOW)['allowed'])
    def test_operator_hold_requires_changed_context(self):
        self.remember('operator_hold');self.assertFalse(r.gate(self.db,self.context(),NOW)['allowed'])
        changed=self.context(operator={'recheck':'explicit-fictional-operation'})
        self.assertTrue(r.gate(self.db,changed,NOW)['allowed'])
    def test_recovered_budget_can_record_new_outcome(self):
        self.remember('amount_daily')
        result=self.remember('evidence',operation_id='recovered',resources_available=True)
        self.assertTrue(result['recorded']);self.assertEqual(r.gate(self.db,self.context(),NOW)['cause'],'evidence')
    def test_unrelated_event_receipt_survives(self):
        ctx=self.context();self.remember(context=ctx)
        other=copy.deepcopy(self.event);other['event_id']='different';other['summary']='別件の訂正'
        self.assertTrue(r.gate(self.db,r.make_context('schedule_analysis',other,self.articles,NOW,search=SEARCH,validator_id='fictional-validator-v1'),NOW)['allowed'])
        self.assertFalse(r.gate(self.db,ctx,NOW)['allowed'])
    def test_same_operation_after_restart_clock_is_idempotent(self):
        self.remember()
        result=self.remember(now=NOW+timedelta(hours=12))
        self.assertTrue(result['operation_replayed'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM tracking_retry_attempts').fetchone()[0],1)
    def test_adapter_operation_replay_does_not_revalidate(self):
        ctx=self.context()
        r.replay_schedule(self.db,ctx,self.event,self.articles,None,None,NOW,operation_id='repeat')
        with patch.object(r.diagnostics,'schedule_replay',side_effect=AssertionError('must not revalidate')):
            result=r.replay_schedule(self.db,ctx,self.event,self.articles,None,None,NOW+timedelta(hours=2),operation_id='repeat')
        self.assertTrue(result['operation_replayed'])
    def test_existing_schedule_diagnostics_rollback(self):
        ctx=self.context()
        with self.assertRaises(RuntimeError):r.replay_schedule(self.db,ctx,self.event,self.articles,
             {'matches':[{'url':ARTICLE['url'],'confidence':'high'}]},{'status':'incomplete'},NOW,operation_id='fail',fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM tracking_retry_attempts').fetchone()[0],0)
        tables={x[0] for x in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'queue_diagnostics' in tables:self.assertEqual(self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0],0)
    def test_unknown_search_boundary_requires_explicit_unbounded(self):
        with self.assertRaises(ValueError):self.context(search={**SEARCH,'window_start':None})
        ctx=self.context(search={**SEARCH,'window_start':None,'unbounded_start':True})
        self.assertTrue(r.gate(self.db,ctx,NOW)['allowed'])
    def test_failure_of_other_stage_cannot_suppress_schedule(self):
        ctx=self.context(stage='backfill_selection');self.remember(context=ctx)
        job=tracking_job('due',[100,200],event_id=self.event['event_id'],retry_context=ctx)
        with self.assertRaises(ValueError):p.plan_tracking([job],[],{},NOW,retry_db=self.db)
    def test_b_classification_and_cache_unchanged(self):
        gs,triage=fixtures();c=p.cache;c.capture(self.db,gs[-1],payload(),triage=triage)
        result=p.run_round(self.db,gs,[],[],NOW,operation_id='b-round',ledger={},triage=triage)
        self.assertEqual(len(result['status']['selected']),4);self.assertEqual(result['status']['cache_validated'],1)
        self.assertFalse(result['can_publish'])

if __name__=='__main__':unittest.main()
