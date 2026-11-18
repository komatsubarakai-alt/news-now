"""Fictional-only offline fairness, budget and cache integration regressions."""
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
from pending_triage import group_key, fingerprint
from test_status_cache_replay import group, payload, NOW
import shadow_priority as p
import status_cache_replay as c


def fixtures(n=8):
    gs=[group(i) for i in range(n)]
    for i,g in enumerate(gs):
        g['articles'][0]['title']='架空'+('起訴の続報' if i%2 else '制度の報道')+str(i)
    triage={group_key(g):{'first_seen_at':(NOW-timedelta(hours=100-i)).isoformat()} for i,g in enumerate(gs)}
    return gs,triage


def tracking_job(n,sizes=None,**kw): return {'id':str(n),'prompt_bytes':sizes or [100],**kw}


class PriorityTests(unittest.TestCase):
    def setUp(self):
        self.db=connect();self.gs,self.triage=fixtures()
        self.net=patch('socket.socket.connect',side_effect=AssertionError('network forbidden'));self.net.start()
        self.http=patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden'));self.http.start()
    def tearDown(self): self.net.stop();self.http.stop();self.db.close()
    def run_policy(self,**kw):
        opts=dict(operation_id='op1',ledger={},triage=self.triage);opts.update(kw)
        return p.run_status(self.db,self.gs,NOW,**opts)
    def test_pattern(self):
        r=self.run_policy();self.assertEqual([g['group_key'] for g in r['selected']],[group_key(self.gs[i]) for i in [0,1,2,4]])
    def test_missing_signal_redistributes(self):
        for g in self.gs:g['articles'][0]['title']='架空の制度'
        r=self.run_policy();self.assertEqual(len(r['selected']),4);self.assertEqual(sum(x['signal'] for x in r['selected']),0)
    def test_missing_normal_redistributes(self):
        for g in self.gs:g['articles'][0]['title']='架空の起訴'
        self.assertEqual(len(self.run_policy()['selected']),4)
    def test_empty(self):
        self.gs=[];self.assertEqual(self.run_policy()['selected'],[])
    def test_less_than_four(self):
        self.gs=self.gs[:2];self.assertEqual(len(self.run_policy()['selected']),2)
    def test_identical_group_deduplicated(self):
        self.gs=[self.gs[0],copy.deepcopy(self.gs[0])];self.assertEqual(len(self.run_policy()['selected']),1)
    def test_limit_enforced(self):
        with self.assertRaises(ValueError):self.run_policy(limit=5)
    def test_zero_processing_slots(self):
        r=self.run_policy(limit=0);self.assertFalse(r['selected']);self.assertTrue(all(v==['processing_slots'] for v in r['wait_reasons'].values()))
    def test_budget_stops_and_preserves_oldest_next_run(self):
        day,month=p.periods(NOW);ledger={'days':{day:{'reserved_micro_usd':200000}}}
        self.assertEqual(self.run_policy(ledger=ledger)['selected'],[])
        r=self.run_policy(operation_id='op2');self.assertEqual(r['selected'][0]['group_key'],group_key(self.gs[0]))
    def test_monthly_limit(self):
        _,month=p.periods(NOW);r=self.run_policy(ledger={'months':{month:{'reserved_micro_usd':5000000}}})
        self.assertTrue(all('amount_monthly' in v for v in r['wait_reasons'].values()))
    def test_daily_call_limit(self):
        day,_=p.periods(NOW);r=self.run_policy(ledger={'days':{day:{'calls':40}}})
        self.assertTrue(all('calls_daily' in v for v in r['wait_reasons'].values()))
    def test_run_call_limit(self):
        r=self.run_policy(ledger={'run':{'calls':24}});self.assertTrue(all('calls_run' in v for v in r['wait_reasons'].values()))
    def test_budget_is_accumulated(self):
        day,_=p.periods(NOW);ledger={'days':{day:{'reserved_micro_usd':180000,'tracking_micro_usd':100000,'tracking_calls':16}},'run':{'tracking_calls':4}}
        r=self.run_policy(ledger=ledger);self.assertEqual(len(r['selected']),2);self.assertLessEqual(r['reservation_micro_usd'],20000)
    def test_tracking_protection(self):
        day,_=p.periods(NOW);r=self.run_policy(ledger={'days':{day:{'reserved_micro_usd':100000}}})
        self.assertTrue(all('tracking_reserved_amount' in v for v in r['wait_reasons'].values()))
    def test_evidence_is_not_retried(self):
        self.triage[group_key(self.gs[0])].update(status='needs_evidence')
        r=self.run_policy();self.assertNotIn(group_key(self.gs[0]),[g['group_key'] for g in r['selected']]);self.assertIn(['evidence'],r['wait_reasons'].values())
    def test_changed_revision_can_reopen_evidence_wait(self):
        g=self.gs[0];self.triage[group_key(g)].update(status='needs_evidence',source_fingerprint=fingerprint(g))
        g['articles'][0]['description']='変更された架空根拠'
        self.assertEqual(self.run_policy()['selected'][0]['group_key'],group_key(g))
    def test_suppressed_not_retried(self):
        self.triage[group_key(self.gs[0])]['cached_rejection_waiting']=True
        self.assertIn(['rejected_unchanged'],self.run_policy()['wait_reasons'].values())
    def test_cache_separate_from_four_slots(self):
        g=self.gs[-1];c.capture(self.db,g,payload(),triage=self.triage)
        mocks=c.MockResponses({p.digest(c.request(g)):payload() for g in self.gs[:-1]})
        r=self.run_policy(mock=mocks);self.assertEqual(r['cache_validated'],1);self.assertEqual(r['fresh_mock_requests'],4);self.assertEqual(r['fresh_results'],4)
    def test_legacy_cache_not_reused(self):
        raw={p.digest(c.request(self.gs[0])):payload()};r=self.run_policy(raw_legacy_cache=raw)
        self.assertEqual(r['cache_validated'],0);self.assertEqual(r['fresh_results'],0)
    def test_cache_revision_change_invalidates(self):
        c.capture(self.db,self.gs[0],payload(),triage=self.triage);self.gs[0]['articles'][0]['description']='改訂'
        self.assertEqual(self.run_policy()['cache_validated'],0)
    def test_idempotent_operation_and_conflict(self):
        a=self.run_policy();b=self.run_policy();self.assertTrue(b['operation_replayed']);self.assertEqual(a['selected'],b['selected'])
        self.gs[0]['articles'][0]['description']='改訂'
        with self.assertRaises(ValueError):self.run_policy()
    def test_failed_save_rollback_and_retry(self):
        mocks=c.MockResponses({p.digest(c.request(g)):payload() for g in self.gs})
        with self.assertRaises(RuntimeError):self.run_policy(mock=mocks,fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM priority_runs').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0],0)
        self.assertEqual(self.run_policy(mock=mocks)['fresh_results'],4)
    def test_no_source_mutation_or_adoption(self):
        before=copy.deepcopy((self.gs,self.triage));self.run_policy()
        self.assertEqual(before,(self.gs,self.triage))
        for table in ['events','progress','decisions','budget']:self.assertEqual(self.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
    def test_restart_receipt_and_debt(self):
        with tempfile.TemporaryDirectory() as td:
            db=connect(Path(td)/'shadow.sqlite');r=p.run_status(db,self.gs,NOW,operation_id='restart',ledger={},triage=self.triage);db.close()
            db=connect(Path(td)/'shadow.sqlite');again=p.run_status(db,self.gs,NOW,operation_id='restart',ledger={},triage=self.triage)
            self.assertTrue(again['operation_replayed']);self.assertEqual(r['selected'],again['selected']);db.close()
    def test_unchanged_invalid_answer_suppressed(self):
        bad=payload(category='invalid');mocks=c.MockResponses({p.digest(c.request(g)):bad for g in self.gs})
        self.run_policy(mock=mocks)
        r=self.run_policy(mock=mocks,operation_id='op2')
        self.assertIn(['rejected_unchanged'],r['wait_reasons'].values())
        self.assertTrue(all(x['group_key'] not in [group_key(g) for g in self.gs[:3]] for x in r['selected']))

    def test_success_advances_oldest_opportunity(self):
        m=c.MockResponses({p.digest(c.request(g)):payload() for g in self.gs})
        first=self.run_policy(mock=m)
        second=self.run_policy(mock=m,operation_id='op2')
        self.assertTrue(set(x['group_key'] for x in first['selected']).isdisjoint(x['group_key'] for x in second['selected']))
        self.assertEqual(second['remaining_status_groups'],0)
    def test_invalid_policy(self):
        with self.assertRaises(ValueError):self.run_policy(policy='unknown')
    def test_unchanged_sources_wait_not_complete(self):
        r=self.run_policy();self.assertEqual(r['remaining_status_groups'],len(self.gs));self.assertEqual(r['fresh_results'],0)

    def test_shared_round_cannot_overspend_across_stages(self):
        day,_=p.periods(NOW)
        ledger={'days':{day:{'reserved_micro_usd':170000,'tracking_micro_usd':100000,'tracking_calls':16}}}
        r=p.run_round(self.db,self.gs,[],[tracking_job('normal')],NOW,operation_id='round1',ledger=ledger,triage=self.triage)
        self.assertLessEqual(r['reservation_micro_usd'],30000)
        self.assertEqual(r['tracking']['reserved_calls'],1)
        self.assertLess(len(r['status']['selected']),4)
    def test_round_operation_replay_and_failure(self):
        kw=dict(operation_id='round1',ledger={},triage=self.triage)
        with self.assertRaises(RuntimeError):p.run_round(self.db,self.gs,[],[],NOW,fail_save=True,**kw)
        self.assertEqual(self.db.execute('SELECT count(*) FROM priority_runs').fetchone()[0],0)
        r=p.run_round(self.db,self.gs,[],[],NOW,**kw)
        again=p.run_round(self.db,self.gs,[],[],NOW,**kw)
        self.assertTrue(again['operation_replayed']);self.assertEqual(r['reservation_micro_usd'],again['reservation_micro_usd'])


class TrackingTests(unittest.TestCase):
    def plan(self,due=None,normal=None,**kw):
        return p.plan_tracking(due if due is not None else [tracking_job('due',[100,200])],
                               normal if normal is not None else [tracking_job('normal')],kw.pop('ledger',{}),NOW,**kw)
    def test_due_and_normal_minimum(self):
        r=self.plan();self.assertEqual(set(r['selected']),{'normal','due'});self.assertEqual(r['reserved_calls'],3)
    def test_no_due_keeps_normal(self): self.assertEqual(self.plan(due=[])['selected'],['normal'])
    def test_no_normal_allows_due(self): self.assertEqual(self.plan(normal=[])['selected'],['due'])
    def test_only_two_calls_preserves_normal(self):
        r=self.plan(call_limit=2);self.assertEqual(r['selected'],['normal']);self.assertEqual(r['wait_reasons']['due'],['processing_slots'])
    def test_missing_prompt_waits_unknown(self):
        r=self.plan(due=[{'id':'due'}]);self.assertEqual(r['wait_reasons']['due'],['unknown'])
    def test_one_due_per_run(self):
        r=self.plan(due=[tracking_job('d1',[10,20]),tracking_job('d2',[10,20])]);self.assertEqual(r['due_selected'],1);self.assertIn('d2',r['wait_reasons'])
    def test_money_wait_is_not_slots(self):
        day,_=p.periods(NOW);r=self.plan(ledger={'days':{day:{'reserved_micro_usd':200000}}})
        self.assertTrue(all('amount_daily' in v for v in r['wait_reasons'].values()))
    def test_blocked_evidence_and_rejection(self):
        r=self.plan(due=[tracking_job('d',blocked='evidence')],normal=[tracking_job('n',blocked='rejected_unchanged')])
        self.assertEqual(r['wait_reasons'],{'d':['evidence'],'n':['rejected_unchanged']})
    def test_duplicate_job_rejected(self):
        with self.assertRaises(ValueError):self.plan(due=[tracking_job('normal')])
    def test_limit_cannot_increase(self):
        with self.assertRaises(ValueError):self.plan(call_limit=9)
    def test_does_not_mutate_ledger_or_jobs(self):
        jobs=[tracking_job('d')];ledger={};before=copy.deepcopy((jobs,ledger));self.plan(due=jobs,ledger=ledger);self.assertEqual(before,(jobs,ledger))

    def test_prior_tracking_calls_reduce_available_stage_slots(self):
        r=self.plan(ledger={'run':{'tracking_calls':6,'calls':6}})
        self.assertEqual(r['selected'],['normal']);self.assertIn('processing_slots',r['wait_reasons']['due'])
    def test_budget_bundle_is_atomic(self):
        day,_=p.periods(NOW)
        r=self.plan(ledger={'days':{day:{'reserved_micro_usd':185000}}})
        self.assertEqual(r['selected'],['normal']);self.assertEqual(r['reserved_calls'],1)
        self.assertIn('amount_daily',r['wait_reasons']['due'])
    def test_due_adapter_keeps_unknown_inputs(self):
        event={'event_id':'fictional','schedules':[{'status':'scheduled','due_start':'2026-01-01','due_end':'2026-01-01'}]}
        jobs=p.scheduled_jobs([event],NOW)
        self.assertEqual(len(jobs),1);self.assertEqual(self.plan(due=jobs)['wait_reasons']['schedule:fictional'],['unknown'])
    def test_tracking_monthly_and_daily_calls_are_separate(self):
        day,month=p.periods(NOW)
        r=self.plan(ledger={'days':{day:{'calls':40}},'months':{month:{'reserved_micro_usd':5000000}}})
        for v in r['wait_reasons'].values():self.assertIn('amount_monthly',v);self.assertIn('calls_daily',v)

if __name__=='__main__':unittest.main()
