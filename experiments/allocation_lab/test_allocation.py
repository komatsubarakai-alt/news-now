import copy,socket,unittest,tempfile,json
from unittest.mock import patch
from .allocation import *
from .snapshot import status_prompt_factory,summarize_wait
from .run import fictional

class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.jobs,self.other,self.seed,self.asof,self.last,self.profiles=fictional();self.windows=opportunities(self.asof,self.last)
        self.net=patch.object(socket.socket,'connect',side_effect=AssertionError('network forbidden'));self.net.start();self.addCleanup(self.net.stop)
        self.api=patch.object(native,'response',side_effect=AssertionError('AI forbidden'));self.api.start();self.addCleanup(self.api.stop)
    def simulate(self,plan='A',**kw):return simulate(plan,self.jobs,self.seed,self.windows,self.profiles,asof=self.asof,**kw)
    def test_plans_keep_native_daily_and_tracking_guards(self):
        for mode in ('estimated_settlement','reservation_only'):
            for plan in PLANS:
                result=self.simulate(plan,cost_mode=mode)
                for t in result['trace']:
                    self.assertLessEqual(t['daily_calls'],24);self.assertLessEqual(t['daily_accounted_micro_usd'],100000);self.assertLessEqual(t['monthly_accounted_micro_usd'],5000000)
                self.assertEqual(result['approval_count'],0);self.assertEqual(result['event_merges'],0)
    def test_inputs_and_history_immutable(self):
        self.jobs['status'][0]['history']=[{'decision_id':'fictional-old','state':'withdrawn'}]
        before=copy.deepcopy((self.jobs,self.seed));result=self.simulate('B')
        self.assertEqual(before,(self.jobs,self.seed));self.assertTrue(result['source_records_preserved'])
    def test_unknown_answers_never_complete(self):
        r=self.simulate('B',outcome='unknown');self.assertEqual(r['completed_actual_groups'],0);self.assertEqual(r['completed_mock_groups'],0);self.assertEqual(r['initial_waiting_article_versions'],r['final_waiting_article_versions'])
    def test_downstream_transfer_is_not_total_queue_reduction(self):
        r=self.simulate('B',outcome='mock_downstream');self.assertGreater(r['completed_mock_groups'],0);self.assertEqual(r['closed_mock_groups'],0);self.assertEqual(r['final_waiting_article_versions'],120);self.assertGreater(r['transferred_mock_article_versions'],0)
    def test_invalid_answers_keep_versions(self):
        r=self.simulate(outcome='mock_invalid');self.assertEqual(r['final_waiting_article_versions'],120);self.assertTrue(r['holds']['mock_invalid'])
    def test_unchanged_work_never_selected_twice(self):
        r=self.simulate('B');ids=[x['id'] for x in r['receipts'] if 'reservation_micro_usd' in x];self.assertEqual(len(ids),len(set(ids)))
    def test_rejection_signature_suppresses_only_same_context(self):
        j=self.jobs['status'][0];j.update(blocked_reason='invalid_answer',blocked_signature=j['context_signature']);r=self.simulate();self.assertEqual(r['remaining_blocked_groups'],1)
        j['context_signature']='changed-article-or-validator';self.assertEqual(self.simulate()['remaining_blocked_groups'],0)
    def test_missing_rejection_proof_is_not_fabricated(self):
        self.jobs['status'][0].update(blocked_reason='old_unknown',blocked_signature=None,context_signature=None)
        self.assertEqual(self.simulate()['remaining_blocked_groups'],0)
    def test_month_stop(self):
        self.seed={'days':{},'months':{'2026-02':{'reserved_micro_usd':4999999,'calls':0}}}
        r=self.simulate();self.assertEqual(sum(r['simulated_calls'].values()),0);self.assertEqual(r['final_waiting_article_versions'],120)
    def test_daily_call_exhaustion(self):
        self.seed={'days':{'2026-02-01':{'calls':40,'tracking_calls':16,'reserved_micro_usd':1}},'months':{}}
        self.windows=self.windows[:2];r=self.simulate();self.assertEqual(sum(r['simulated_calls'].values()),0)
    def test_run_limit_reason(self):self.assertEqual(rejection_reason({'run':{'calls':24}},dt(self.asof),1),'run_call_limit')
    def test_daily_money_reason(self):self.assertEqual(rejection_reason({'days':{'2026-02-01':{'reserved_micro_usd':200000}}},dt(self.asof),1),'money_limit:day')
    def test_tracking_can_still_reserve(self):
        r=self.simulate('B');at=self.windows[-1]
        with shadow_ledger(r['ledger']),patch.object(native,'now',return_value=at),patch.dict(os.environ,{'GITHUB_RUN_ID':'tracking-proof','AI_PROCESSING_STAGE':'tracking'}):self.assertIsNotNone(native.reserve('fictional search',native.MODEL,1200))
    def test_day_reset_and_interval(self):
        self.assertEqual(len(self.windows),4);self.assertTrue(all(b-a>=timedelta(hours=6) for a,b in zip(self.windows,self.windows[1:])))
        self.assertGreaterEqual(self.windows[0],dt(self.last)+timedelta(hours=6));self.assertEqual(len({native.periods(x)[0] for x in self.windows}),2)
    def test_restored_ledger_restarts_without_source_change(self):
        first=self.simulate();self.seed=copy.deepcopy(first['ledger']);r=self.simulate();self.assertEqual(r['source_records_preserved'],True)
    def test_failure_restores_working_directory(self):
        previous=os.getcwd()
        with self.assertRaises(OSError):
            with shadow_ledger({}):raise OSError('simulated save failure')
        self.assertEqual(os.getcwd(),previous)
    def test_native_save_failure_aborts_without_mutating_inputs(self):
        previous=os.getcwd();before=copy.deepcopy((self.jobs,self.seed))
        with patch.object(native,'atomic',side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):self.simulate('B')
        self.assertEqual(os.getcwd(),previous);self.assertEqual(before,(self.jobs,self.seed))
        self.assertGreater(self.simulate('B')['completed_mock_groups'],0)
    def test_malformed_ledger_fails_closed(self):
        with shadow_ledger({'days':{},'months':{}}):
            Path('ai_budget.json').write_text('broken ledger')
            with self.assertRaises(ValueError):native.reserve('fictional',native.MODEL,1200)
    def test_reason_summary_deduplicates_stages(self):
        from pending_triage import group_key
        article={'title':'架空事件','url':'https://example.test/fictional','description':'','published':''};group={'articles':[article]};key=group_key(group)
        files={'news_inbox':[article],'collection_triage':{'items':{article['url']:{'status':'queued'}}},'status_triage':{'items':{key:{'reason':'budget_deferred'}}},'tracking_triage':{'items':{key:{'reason':'article_identity_unproved','cached_rejection_waiting':True}}},'status_pending':[group],'tracking_pending':[group]}
        with tempfile.TemporaryDirectory() as directory:
            for name,data in files.items():Path(directory,name+'.json').write_text(json.dumps(data))
            r=summarize_wait(directory)
        self.assertEqual(r['unique_waiting_versions'],1);self.assertEqual(r['primary_article_versions']['evidence_missing'],1);self.assertEqual(r['tracking_cached_rejection_overlay_not_additive'],1)
    def test_duplicate_id_rejected(self):
        self.jobs['status'].append(copy.deepcopy(self.jobs['status'][0]))
        with self.assertRaises(ValueError):self.simulate()
    def test_unknown_budget_label_stays_unknown(self):self.assertEqual(explain_wait({'reason':'budget_deferred'},'status')[0],'other_unknown')
    def test_ambiguous_call_limit_stays_unknown(self):self.assertEqual(explain_wait({'reason':'call_budget_deferred'},'status')[0],'other_unknown')
    def test_daily_and_run_causes_separate(self):
        for kind,label in [('day','daily_call_limit'),('run','run_call_limit')]:self.assertEqual(explain_wait({'reason':'call_budget_deferred'},'status',{'limit_kind':kind})[0],label)
    def test_logged_slot_requires_matching_time(self):
        e={'stage':'status','source':'fictional complete log','complete_log':True,'status_successes':4,'status_limit':4,'status_errors':0,'from':'2026-02-01T00:00:00Z','to':'2026-02-01T00:02:00Z'}
        self.assertEqual(explain_wait({'reason':'budget_deferred','last_seen_at':'2026-02-01T00:01:00Z'},'status',e)[0],'status_slot')
        self.assertEqual(explain_wait({'reason':'budget_deferred','last_seen_at':'2026-01-01T00:01:00Z'},'status',e)[0],'other_unknown')
    def test_interval_requires_gate_evidence(self):
        e={'source':'fictional log','gate_deferred':True,'billing_blocked':False,'at':self.asof,'last_ai_run_at':self.last}
        self.assertEqual(explain_wait({},'collection',e)[0],'interval');e['gate_deferred']=False;self.assertEqual(explain_wait({},'collection',e)[0],'other_unknown')
    def test_evidence_failures_not_approved(self):
        self.assertEqual(explain_wait({'reason':'article_identity_unproved'},'tracking')[0],'evidence_missing')
        self.assertEqual(explain_wait({'reason':'uncertain_or_invalid_evidence'},'tracking')[0],'validation_unestablished')
    def test_no_missing_fields_filled_by_prompt_adapter(self):
        factory=status_prompt_factory("article_text=''\nfor article in articles:\n article_text += f\"{article.get('title','')}|{article.get('published','')}\"\nprompt=f'{event_name}:{article_text}'")
        self.assertEqual(factory({'event_name':'架空','articles':[{'title':'架空見出し'}]}),'架空:架空見出し|')
    def test_prompt_syntax_change_stops(self):
        with self.assertRaises(ValueError):status_prompt_factory('prompt="changed"')
    def test_protected_calls_not_reallocated(self):self.assertEqual(rejection_reason({'days':{'2026-02-01':{'calls':24}}},dt(self.asof),1),'daily_call_limit:tracking_protection')
    def test_model_profile_validation(self):
        with self.assertRaises(ValueError):cost_profile([])
    def test_shared_versions_count_once_without_merging(self):
        self.jobs['status'][1]['versions']=self.jobs['status'][0]['versions'][:]
        self.assertEqual(self.simulate(outcome='unknown')['initial_waiting_article_versions'],119)
    def test_no_evidence_based_auto_exclusion(self):
        self.jobs['status'][0]['reason']='missing_evidence';r=self.simulate(outcome='unknown');self.assertEqual(r['final_waiting_article_versions'],120)
    def test_identical_seed_is_reproducible(self):self.assertEqual(self.simulate('C'),self.simulate('C'))
    def test_no_window_is_safe_with_asof(self):self.windows=[];self.assertEqual(self.simulate()['final_waiting_article_versions'],120)

if __name__=='__main__':unittest.main()
