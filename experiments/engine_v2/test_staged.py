import copy
import json
from unittest.mock import patch

import test_engine as fixtures
from engine import atomic, events, dumps
from staged import begin,step,inspect
from event_tracker import TRACKING_RESPONSE
from pending_triage import fingerprint,group_key
from operator_review import digest
from test_tracking import NOW,event,report

# Inherit only fixture setup; do not duplicate the earlier test methods.
import unittest
class StagedTests(unittest.TestCase):
    setUp=fixtures.PrototypeTests.setUp
    tearDown=fixtures.PrototypeTests.tearDown
    write=fixtures.PrototypeTests.write
    def add_candidates(self):
        with self.db:
            for i in range(6):
                e=event();e['event_id']='other'+str(i);e['articles'][0]['url']='https://example.test/other'+str(i)
                self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
    def session(self,triage=None):
        from engine import rank
        def controlled(db,g,t,persist=False):
            rows=rank(db,g,t,persist=False);rows.sort(key=lambda r:r['event_id']=='e1')
            for i,r in enumerate(rows,1):r['rank']=i
            return rows
        with patch('staged.rank',side_effect=controlled):return begin(self.db,self.g,triage)
    def answer(self):
        a=copy.deepcopy(TRACKING_RESPONSE);a.update(report(self.g['articles'][0]));return a
    def unknown(self,prompt,batch):return copy.deepcopy(TRACKING_RESPONSE)
    def test_batch_policies_preserve_every_candidate_and_resume(self):
        self.add_candidates()
        for policy,widths in [(1,[1]*7),(2,[2,2,2,1]),(4,[4,3]),(8,[7]),((1,2,4,8),[1,2,4]),((4,64),[4,3])]:
            sid=begin(self.db,self.g,batch_size=policy)
            state=inspect(self.db,sid)
            self.assertEqual(step(self.db,sid,self.unknown,NOW,budget_available=False)['cursor'],0)
            for width in widths:
                before=state['cursor'];state=step(self.db,sid,self.unknown,NOW)
                self.assertEqual(state['cursor'],before+width)
                self.assertEqual(begin(self.db,self.g,batch_size=policy),sid)
            self.assertEqual(state['status'],'needs_evidence')
            self.assertEqual(len(state['candidate_states']),7)
            self.assertTrue(all(s=='examined_unresolved' for s in state['candidate_states'].values()))
    def test_changed_policy_does_not_reuse_old_session(self):
        self.add_candidates();sid=begin(self.db,self.g,batch_size=(1,2,4,8))
        step(self.db,sid,self.unknown,NOW)
        new=begin(self.db,self.g,batch_size=8)
        self.assertNotEqual(sid,new);self.assertEqual(inspect(self.db,sid)['cursor'],1)
        self.assertEqual(inspect(self.db,new)['cursor'],0)
    def test_widening_save_failure_reuses_answer_without_advancing_width(self):
        self.add_candidates();sid=begin(self.db,self.g,batch_size=(1,2,4,8));calls=[]
        def mock(p,b):calls.append(b);return self.unknown(p,b)
        with self.assertRaises(RuntimeError):step(self.db,sid,mock,NOW,fail_save=True)
        self.assertEqual(inspect(self.db,sid)['cursor'],0)
        state=step(self.db,sid,mock,NOW,budget_available=False)
        self.assertEqual(state['cursor'],1);self.assertEqual(len(calls),1)
        state=step(self.db,sid,mock,NOW)
        self.assertEqual(state['cursor'],3);self.assertEqual(len(calls[-1]),2)
    def test_invalid_policy_rejected_without_session(self):
        for value in [0,65,True,(),(1,0),(1,'2')]:
            with self.assertRaises(ValueError):begin(self.db,self.g,batch_size=value)
    def test_all_policies_budget_stop_preserves_article_cursor_and_tail(self):
        self.add_candidates()
        for policy in [1,2,4,8,(1,2,4,8),(4,64)]:
            sid=begin(self.db,self.g,batch_size=policy);before=inspect(self.db,sid)
            with patch('staged.reserve_mock',return_value={'status':'budget_deferred'}):
                state=step(self.db,sid,lambda *a:self.fail('mock must not be called'),NOW,charge_simulated=True)
            self.assertEqual(state['group'],before['group']);self.assertEqual(state['plan'],before['plan'])
            self.assertEqual(state['cursor'],0);self.assertEqual(state['records'],[])
            self.assertEqual(state['candidate_states'],before['candidate_states'])
            self.assertEqual(state['status'],'budget_deferred')
    def test_next_batch_and_completed_repeat_do_not_erase_unknowns(self):
        self.add_candidates();sid=self.session()
        first=step(self.db,sid,self.unknown,NOW)
        self.assertEqual(first['cursor'],4);self.assertEqual(first['status'],'ready')
        self.assertEqual(list(first['candidate_states'].values()).count('examined_unresolved'),4)
        second=step(self.db,sid,lambda p,b:self.answer(),NOW)
        self.assertEqual(second['status'],'accepted');self.assertEqual(second['cursor'],7)
        with patch('builtins.print'):
            result=step(self.db,sid,lambda *a:(_ for _ in ()).throw(AssertionError('must not repeat')),NOW)
        self.assertEqual(result['records'],second['records'])
        self.assertEqual(len(result['candidate_states']),7)
    def test_budget_before_and_between_batches_resumes_at_same_cursor(self):
        self.add_candidates();sid=self.session();mock=lambda *a:(_ for _ in ()).throw(AssertionError('budget must not invoke AI'))
        result=step(self.db,sid,mock,NOW,budget_available=False)
        self.assertEqual(result['cursor'],0);self.assertEqual(result['records'],[])
        step(self.db,sid,self.unknown,NOW)
        result=step(self.db,sid,mock,NOW,budget_available=False)
        self.assertEqual(result['cursor'],4);self.assertEqual(len(result['records']),1)
        self.assertEqual(step(self.db,sid,lambda p,b:self.answer(),NOW)['status'],'accepted')
    def test_malformed_answers_finish_unresolved_and_repeat_is_free(self):
        self.add_candidates();sid=self.session()
        calls=[]
        def malformed(p,b):calls.append(b);return ['broken answer']
        for _ in range(2):result=step(self.db,sid,malformed,NOW)
        self.assertEqual(result['status'],'needs_evidence');self.assertEqual(result['cursor'],7)
        self.assertEqual(len(result['records']),2)
        step(self.db,sid,malformed,NOW);self.assertEqual(len(calls),2)
        self.assertTrue(all(s=='examined_unresolved' for s in result['candidate_states'].values()))
    def test_unpresented_target_cannot_be_adopted(self):
        self.add_candidates();sid=self.session()
        first=step(self.db,sid,lambda p,b:self.answer(),NOW)
        self.assertEqual(first['records'][0]['outcome'],'invalid_answer_schema_or_target')
        self.assertEqual(len(events(self.db)[0]['articles']),1)
        self.assertEqual(step(self.db,sid,lambda p,b:self.answer(),NOW)['status'],'accepted')
    def test_save_failure_rolls_back_state_but_keeps_response_for_free_retry(self):
        sid=begin(self.db,self.g);before=events(self.db);calls=[]
        def good(p,b):calls.append(1);return self.answer()
        with self.assertRaises(RuntimeError):step(self.db,sid,good,NOW,fail_save=True)
        self.assertEqual(events(self.db),before)
        self.assertEqual(inspect(self.db,sid)['cursor'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM changes').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM batch_cache').fetchone()[0],1)
        result=step(self.db,sid,good,NOW,budget_available=False)
        self.assertEqual(result['status'],'accepted');self.assertEqual(len(calls),1)
    def test_source_revision_new_session_preserves_old_cursor_and_article(self):
        self.add_candidates();sid=self.session();step(self.db,sid,self.unknown,NOW)
        old=inspect(self.db,sid);self.g['articles'][0]['description']='changed'
        new=begin(self.db,self.g)
        self.assertNotEqual(sid,new);self.assertEqual(inspect(self.db,new)['cursor'],0)
        self.assertEqual(inspect(self.db,sid)['group'],old['group'])
        self.assertEqual(inspect(self.db,sid)['cursor'],4)
    def test_changed_target_context_stales_old_session_and_no_reuse(self):
        self.add_candidates();sid=self.session();step(self.db,sid,self.unknown,NOW)
        e=events(self.db)[0];e['summary']='new evidence'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),e['event_id']))
        result=step(self.db,sid,lambda *a:(_ for _ in ()).throw(AssertionError('stale')),NOW)
        self.assertEqual(result['status'],'stale_requires_new_session');self.assertEqual(result['cursor'],4)
        self.assertNotEqual(begin(self.db,self.g),sid)
    def test_operator_target_only_and_catalog_preserved(self):
        self.add_candidates()
        triage={group_key(self.g):{'operator_review':{'action':'same_event','event_id':'e1','source_fingerprint':fingerprint(self.g),'group_hash':digest(self.g)}}}
        sid=begin(self.db,self.g,triage)
        self.assertEqual(inspect(self.db,sid)['plan'],['e1'])
        result=step(self.db,sid,self.unknown,NOW,triage)
        self.assertEqual(result['status'],'needs_evidence')
        self.assertEqual(list(result['candidate_states'].values()).count('operator_not_selected'),6)
    def test_parse_failure_recorded_once_and_does_not_create_event(self):
        sid=begin(self.db,self.g)
        result=step(self.db,sid,lambda *a:(_ for _ in ()).throw(ValueError('bad json')),NOW)
        self.assertEqual(result['status'],'needs_evidence')
        self.assertEqual(len(result['records']),1);self.assertEqual(events(self.db),[self.e])
    def test_empty_candidates_never_register_new_event(self):
        with self.db:
            self.db.execute('DELETE FROM event_articles');self.db.execute('DELETE FROM aliases');self.db.execute('DELETE FROM events')
        sid=begin(self.db,self.g)
        result=step(self.db,sid,lambda *a:(_ for _ in ()).throw(AssertionError('no candidates')),NOW)
        self.assertEqual(result['status'],'needs_evidence');self.assertEqual(events(self.db),[])
    def test_nested_atomic_failure_cannot_commit_event_early(self):
        with self.assertRaises(RuntimeError):
            with atomic(self.db):
                with atomic(self.db):self.db.execute("UPDATE events SET payload='broken'")
                raise RuntimeError('outer transaction interruption')
        self.assertEqual(events(self.db),[self.e])
    def test_three_distinct_format_failures_pause_with_candidates_retained(self):
        self.add_candidates()
        with self.db:
            for i in range(12):
                e=event();e['event_id']='many'+str(i);e['articles']=[]
                self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        sid=begin(self.db,self.g)
        for _ in range(3):result=step(self.db,sid,lambda p,b:{},NOW)
        self.assertEqual(result['status'],'manual_review')
        self.assertEqual(result['cursor'],12)
        self.assertEqual(list(result['candidate_states'].values()).count('unexamined'),7)
        self.assertEqual(len(result['records']),3)
    def test_simulated_run_cap_and_next_run_preserve_day_month_spend(self):
        with self.db:
            for i in range(34):
                e=event();e['event_id']='limit'+str(i);e['articles']=[]
                self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        sid=begin(self.db,self.g)
        for _ in range(9):result=step(self.db,sid,self.unknown,NOW,charge_simulated=True,simulation_run_id='shadow-first')
        self.assertEqual(result['status'],'budget_deferred');self.assertEqual(result['cursor'],32)
        self.assertEqual(len(result['records']),8)
        state=json.loads(self.db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0])
        monthly_before=sum(x['reserved_micro_usd'] for x in state['months'].values())
        result=step(self.db,sid,self.unknown,NOW,charge_simulated=True,simulation_run_id='shadow-next')
        self.assertEqual(result['status'],'needs_evidence');self.assertEqual(len(result['records']),9)
        state=json.loads(self.db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0])
        self.assertGreater(sum(x['reserved_micro_usd'] for x in state['months'].values()),monthly_before)
    def test_wrong_scalar_types_are_not_accepted(self):
        sid=begin(self.db,self.g);answer=self.answer();answer['multi_event']='false'
        result=step(self.db,sid,lambda p,b:answer,NOW)
        self.assertEqual(result['status'],'needs_evidence')
        self.assertEqual(result['records'][0]['outcome'],'invalid_answer_schema_or_target')
        self.assertEqual(events(self.db),[self.e])
    def test_paid_simulation_failure_reuses_cached_answer_without_new_reservation(self):
        sid=begin(self.db,self.g);calls=[]
        def good(p,b):calls.append(1);return self.answer()
        with self.assertRaises(RuntimeError):
            step(self.db,sid,good,NOW,charge_simulated=True,simulation_run_id='shadow-first',fail_save=True)
        before=self.db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0]
        result=step(self.db,sid,good,NOW,charge_simulated=True,simulation_run_id='shadow-first')
        self.assertEqual(result['status'],'accepted');self.assertEqual(len(calls),1)
        self.assertEqual(self.db.execute("SELECT payload FROM budget WHERE name='simulation'").fetchone()[0],before)
    def test_prior_attempts_count_once_after_full_unresolved_search(self):
        self.add_candidates()
        triage={group_key(self.g):{'source_fingerprint':fingerprint(self.g),'status':'needs_evidence','decision_attempts':2}}
        sid=self.session(triage)
        self.assertEqual(step(self.db,sid,self.unknown,NOW,triage)['status'],'ready')
        self.assertEqual(step(self.db,sid,self.unknown,NOW,triage)['status'],'manual_review')
        self.assertEqual(triage[group_key(self.g)]['decision_attempts'],2)
    def test_partial_uncertainty_does_not_pause_before_valid_later_target(self):
        self.add_candidates()
        triage={group_key(self.g):{'source_fingerprint':fingerprint(self.g),'status':'needs_evidence','decision_attempts':2}}
        sid=self.session(triage)
        step(self.db,sid,self.unknown,NOW,triage)
        self.assertEqual(step(self.db,sid,lambda p,b:self.answer(),NOW,triage)['status'],'accepted')
    def test_temporary_db_reopen_preserves_cursor_unknowns_and_records(self):
        from engine import connect,import_snapshot
        path=str(self.root/'shadow.db')
        self.db.close();self.db=connect(path);import_snapshot(self.db,self.root)
        self.add_candidates();sid=self.session();first=step(self.db,sid,self.unknown,NOW)
        self.db.close();self.db=connect(path)
        resumed=inspect(self.db,sid)
        self.assertEqual(resumed['cursor'],first['cursor'])
        self.assertEqual(resumed['candidate_states'],first['candidate_states'])
        self.assertEqual(resumed['records'],first['records'])
        self.assertEqual(step(self.db,sid,lambda p,b:self.answer(),NOW)['status'],'accepted')
