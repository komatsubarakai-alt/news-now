import copy,tempfile,unittest,socket
from pathlib import Path
from unittest.mock import patch
from .allocation import dt,opportunities,simulate,native
from .run import fictional
from .priority import rank_jobs,signals,work_key
from .session import Session

class SafetyPriorityTests(unittest.TestCase):
    def setUp(self):
        self.jobs,self.other,self.seed,self.at,self.last,self.profiles=fictional();self.windows=opportunities(self.at,self.last)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'mock.sqlite'
        for target,name in [(socket.socket,'connect'),(native,'response')]:
            guard=patch.object(target,name,side_effect=AssertionError('network/API forbidden'));guard.start();self.addCleanup(guard.stop)
    def apply(self,session,op,windows,**kw):return session.apply(op,'C',self.jobs,self.seed,windows,self.profiles,asof=self.at,**kw)
    def test_resume_matches_uninterrupted_A_and_C(self):
        for plan in ('A','C'):
            path=Path(self.temp.name)/(plan+'.sqlite');s=Session(path)
            first=s.apply('first',plan,self.jobs,self.seed,self.windows[:2],self.profiles,asof=self.at);s.close();s=Session(path)
            resumed=s.apply('second',plan,self.jobs,self.seed,self.windows[2:],self.profiles,asof=self.at);s.close()
            direct=simulate(plan,self.jobs,self.seed,self.windows,self.profiles,asof=self.at)
            self.assertEqual(resumed,direct);self.assertGreater(first['unexamined_pair_jobs'],0)
    def test_same_operation_retry_is_idempotent(self):
        s=Session(self.path);r=self.apply(s,'one',self.windows[:1]);s.close();s=Session(self.path)
        self.assertEqual(self.apply(s,'one',self.windows[:1]),r);self.assertEqual(s.db.execute('SELECT count(*) FROM operations').fetchone()[0],1);s.close()
    def test_operation_conflict_refused(self):
        s=Session(self.path);self.apply(s,'one',self.windows[:1]);self.jobs['status'][0]['prompt_bytes']+=1
        with self.assertRaises(ValueError):self.apply(s,'one',self.windows[:1])
        s.close()
    def test_save_failure_rolls_back_budget_and_manifest(self):
        s=Session(self.path)
        with self.assertRaises(OSError):self.apply(s,'one',self.windows[:1],fail_before_commit=True)
        self.assertEqual(s.records(),[]);self.assertEqual(s.db.execute('SELECT count(*) FROM checkpoint').fetchone()[0],0);s.close()
        s=Session(self.path);r=self.apply(s,'one',self.windows[:1]);self.assertEqual(r,simulate('C',self.jobs,self.seed,self.windows[:1],self.profiles,asof=self.at));s.close()
    def test_same_window_new_operation_does_not_spend_again(self):
        s=Session(self.path);self.apply(s,'one',self.windows[:1])
        with self.assertRaises(ValueError):self.apply(s,'two',self.windows[:1])
        s.close()
    def test_unexamined_work_is_persisted(self):
        s=Session(self.path);r=self.apply(s,'one',self.windows[:1]);records=s.records();s.close()
        self.assertEqual(len(records),sum(map(len,self.jobs.values())));self.assertEqual(r['unexamined_pair_jobs'],46)
    def test_revision_and_old_judgment_history_retained(self):
        self.jobs['status'][0]['history']=[{'decision_id':'fictional-old','quote':'fictional quotation'}]
        s=Session(self.path);self.apply(s,'one',self.windows[:1]);old=copy.deepcopy(self.jobs['status'][0]);self.jobs['status'][0]['context_signature']='fictional-revision-2';self.jobs['status'][0]['versions']=['fictional-article-revision2']
        r=self.apply(s,'two',self.windows[1:2]);records=s.records();s.close()
        self.assertIn(old,records);self.assertIn(self.jobs['status'][0],records);self.assertNotEqual(work_key(old),work_key(self.jobs['status'][0]));self.assertEqual(r['event_merges'],0)
    def test_unknown_outcome_is_held_after_restart(self):
        s=Session(self.path);r=self.apply(s,'one',self.windows[:1],outcome='unknown');s.close();s=Session(self.path);r=self.apply(s,'two',self.windows[1:2],outcome='unknown');s.close()
        ids=[x['id'] for x in r['receipts'] if 'reservation_micro_usd' in x];self.assertEqual(len(ids),len(set(ids)));self.assertEqual(r['final_waiting_article_versions'],120);self.assertEqual(r['completed_actual_groups'],0)
    def test_validation_revision_opens_new_work(self):
        j=self.jobs['status'][0];self.assertNotEqual(work_key(j),work_key({**j,'validation_revision':'changed'}))
    def test_unverified_context_cannot_be_persisted(self):
        self.jobs['pair'][0].pop('context_signature');s=Session(self.path)
        with self.assertRaises(ValueError):self.apply(s,'one',self.windows[:1])
        s.close()
    def test_plan_cannot_change_silently_on_resume(self):
        s=Session(self.path);self.apply(s,'one',self.windows[:1])
        with self.assertRaises(ValueError):s.apply('two','A',self.jobs,self.seed,self.windows[1:2],self.profiles,asof=self.at)
        s.close()
    def test_oldest_first_and_unique(self):
        items=copy.deepcopy(self.jobs['status'][:10]);items[-1]['first_seen_at']='2020-01-01T00:00:00Z'
        for j in items:j['progress_candidate']=True;j['followup_candidate']=True
        for policy in ('oldest_follow','oldest_progress'):
            chosen=rank_jobs(items,policy,6);self.assertEqual(chosen[0]['id'],items[-1]['id']);self.assertEqual(len({j['id'] for j in chosen}),6)
    def test_progress_lane_not_starved_by_older_due_candidates(self):
        items=copy.deepcopy(self.jobs['status'][:10])
        for j in items:j['due_related_candidate']=True
        items[-1]['progress_candidate']=True;items[-1]['first_seen_at']='2026-01-31T00:00:00Z'
        self.assertIn(items[-1]['id'],[j['id'] for j in rank_jobs(items,'oldest_progress',6)])
    def test_due_lane_has_separate_article_candidate_slot(self):
        items=copy.deepcopy(self.jobs['status'][:10]);items[-1]['due_related_candidate']=True
        self.assertIn(items[-1]['id'],[j['id'] for j in rank_jobs(items,'oldest_due',6)])
    def test_oldest_compares_instants_not_timezone_strings(self):
        items=copy.deepcopy(self.jobs['status'][:2]);items[0]['first_seen_at']='2026-01-01T10:00:00+09:00';items[1]['first_seen_at']='2026-01-01T02:00:00Z'
        self.assertEqual(rank_jobs(items,'oldest_follow',1)[0]['id'],items[0]['id'])
    def test_catalogue_is_atomic_and_retained(self):
        catalogue=[{'versions':['fictional-a','fictional-b'],'state':'unexamined'}];s=Session(self.path)
        self.apply(s,'one',self.windows[:1],catalogue=catalogue);s.close();s=Session(self.path);self.apply(s,'two',self.windows[1:2]);self.assertEqual(s.db.execute('SELECT count(*) FROM catalogue').fetchone()[0],1);s.close()
    def test_missing_lane_reallocates_without_deleting(self):
        items=copy.deepcopy(self.jobs['status'][:10]);before=copy.deepcopy(items)
        self.assertEqual(len(rank_jobs(items,'oldest_progress',6)),6);self.assertEqual(items,before)
    def test_progress_word_never_confirms_identity(self):
        e=[{'event_id':'fictional-e','event_name':'架空市の公的施設再建計画','articles':[],'schedules':[]}];g=[{'articles':[{'title':'別の場所で起訴','url':'https://example.test/fictional'}]}]
        hints,_=signals(g,e,dt(self.at));h=next(iter(hints.values()));self.assertTrue(h['progress_candidate']);self.assertFalse(h['followup_candidate']);self.assertEqual(h['unexamined_event_ids'],['fictional-e'])
    def test_shared_url_never_merges_multiple_events(self):
        a={'title':'架空の二つの事件を紹介する記事','url':'https://example.test/shared'};events=[{'event_id':eid,'event_name':'架空事件','articles':[a],'schedules':[]} for eid in ['fictional-1','fictional-2']]
        hints,_=signals([{'articles':[a]}],events,dt(self.at));h=next(iter(hints.values()));self.assertEqual(len(h['retrieval_matches']),2);self.assertTrue(all(not m['identity_confirmed'] for m in h['retrieval_matches']))
    def test_shared_media_suffix_is_not_followup_evidence(self):
        a={'title':'架空事件その一 - Example Long Media','url':'https://example.test/one'};b={'title':'無関係な予定 - Example Long Media','url':'https://example.test/two'}
        events=[{'event_id':'fictional-e','event_name':a['title'],'articles':[a],'schedules':[]}]
        h,_=signals([{'articles':[b]}],events,dt(self.at));self.assertFalse(next(iter(h.values()))['followup_candidate'])
    def test_no_cross_article_invented_quote_fragment(self):
        events=[{'event_id':'fictional-e','event_name':'甲乙丙丁 戊己庚辛','articles':[],'schedules':[]}]
        h,_=signals([{'articles':[{'title':'甲乙丙丁'},{'title':'戊己庚辛'}]}],events,dt(self.at));self.assertFalse(next(iter(h.values()))['followup_candidate'])
    def test_due_dates_separate_from_publication(self):
        e=[{'event_id':'fictional-e','event_name':'架空','articles':[],'schedules':[{'schedule_id':'past','due_end':'2026-01-01','status':'planned'},{'schedule_id':'unknown','published':'2020-01-01'},{'schedule_id':'future','due_end':'2030-01-01'}]}]
        _,s=signals([],e,dt(self.at));self.assertEqual([x['state'] for x in s],['overdue','date_unknown','future']);self.assertTrue(all(not x['search_performed'] for x in s))
    def test_corrections_and_multi_event_do_not_publish(self):
        self.jobs['status'][0].update(correction_possible=True,unexamined_event_ids=['fictional-other'])
        r=simulate('C',self.jobs,self.seed,self.windows,self.profiles,asof=self.at,priority_policy='oldest_progress',outcome='mock_downstream')
        self.assertEqual(r['event_merges'],0);self.assertEqual(r['approval_count'],0);self.assertEqual(r['final_waiting_article_versions'],120);self.assertTrue(all(not x.get('can_publish',False) for x in r['receipts']))

if __name__=='__main__':unittest.main()
