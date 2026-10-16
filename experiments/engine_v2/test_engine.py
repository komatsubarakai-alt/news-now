import copy
import json
import socket
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from engine import connect, import_snapshot, events, rank, judge, undo, reserve_mock, dumps
from event_tracker import merge_incoming
from operator_review import digest
from pending_triage import fingerprint, group_key
from test_tracking import NOW, article, event, report, FakeAI
from test_merge_safety import group


class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.e=event(); self.g=group(article('容疑者を起訴','https://example.test/new',NOW.isoformat()))
        self.write('events.json',[self.e]); self.write('tracking_pending.json',[self.g])
        self.db=connect(); import_snapshot(self.db,self.root)
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('real API prohibited'));self.network.start()
    def tearDown(self):
        self.network.stop(); self.db.close(); self.tmp.cleanup()
    def write(self,name,value):
        (self.root/name).write_text(json.dumps(value,ensure_ascii=False))
    def judgment(self,answer=None,**kwargs):
        return judge(self.db,self.g,answer or report(self.g['articles'][0]),NOW,**kwargs)
    def test_correct_followup_and_undo_preserve_id_url_history(self):
        self.assertEqual(self.judgment(),'accepted')
        updated=events(self.db)[0]; self.assertEqual(updated['event_id'],self.e['event_id'])
        self.assertIn(self.g['articles'][0]['url'],[a['url'] for a in updated['articles']])
        undo(self.db,1);self.assertEqual(events(self.db),[self.e])
        self.assertEqual(self.db.execute('SELECT count(*) FROM changes WHERE reversed=1').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0],1)
    def test_same_person_different_case_rejected(self):
        a=copy.deepcopy(self.g['articles'][0]);a['title']='被害者山田の別の交通事故を調査'
        self.g['articles']=[a]
        self.assertEqual(self.judgment(),'needs_evidence');self.assertEqual(events(self.db),[self.e])
    def test_same_region_similar_event_rejected(self):
        a=copy.deepcopy(self.g['articles'][0]);a['title']='札幌の別の住宅で火災'
        self.g['articles']=[a]
        self.assertEqual(self.judgment(),'needs_evidence')
    def test_months_later_indictment_and_years_later_verdict_are_candidates(self):
        for title,delay in [('容疑者を起訴',180),('判決',730)]:
            g=group(article(title,'https://example.test/later',(NOW+timedelta(days=delay)).isoformat()))
            self.assertEqual(rank(self.db,g)[0]['event_id'],self.e['event_id'])
    def test_missing_location_and_changed_category_do_not_remove_candidates(self):
        g=copy.deepcopy(self.g);g['category']='企業・組織';g['articles'][0]['title']='被害者山田の事件の続報'
        self.assertEqual([x['event_id'] for x in rank(self.db,g)],[self.e['event_id']])
    def test_url_revision_keeps_both_versions(self):
        self.g=group(copy.deepcopy(self.e['articles'][0]));self.g['articles'][0]['description']='追加された根拠'
        self.assertEqual(self.judgment(report(self.g['articles'][0],meaningful_change=False)),'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM revisions WHERE url=?',(self.g['articles'][0]['url'],)).fetchone()[0],2)
    def test_correction_hold_wins(self):
        registry={'held_articles':[{'status':'held','article':self.g['articles'][0]}]}
        self.assertEqual(self.judgment(registry=registry),'editorial_hold')
        self.assertEqual(events(self.db),[self.e])
    def test_operator_target_kept_but_does_not_override_evidence(self):
        state={group_key(self.g):{'operator_review':{'action':'same_event','event_id':self.e['event_id'],
             'source_fingerprint':fingerprint(self.g),'group_hash':digest(self.g)}}}
        self.assertTrue(rank(self.db,self.g,state)[0]['reasons']['operator_target'])
        self.assertEqual(self.judgment({'relation':'same_event','event_id':'e1','confidence':'low'},triage=state),'needs_evidence')
    def test_manual_review_does_not_reopen(self):
        state={group_key(self.g):{'status':'manual_review','source_fingerprint':fingerprint(self.g)}}
        self.assertEqual(self.judgment(triage=state),'manual_review')
    def test_rejected_answer_repeat_and_changed_answer(self):
        bad={'relation':'uncertain','confidence':'low'}
        self.assertEqual(self.judgment(bad),'needs_evidence')
        count=self.db.execute('SELECT count(*) FROM decisions').fetchone()[0]
        self.assertEqual(self.judgment(bad),'same_rejected_answer')
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0],count)
        self.assertEqual(self.judgment(),'accepted')
    def test_changed_source_reopens_rejection(self):
        bad={'relation':'uncertain','confidence':'low'};self.judgment(bad)
        self.g['articles'][0]['description']='新根拠'
        self.assertEqual(self.judgment(bad),'needs_evidence')
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0],2)
    def test_bookkeeping_does_not_reopen_rejected_answer(self):
        bad={'relation':'uncertain','confidence':'low'};self.judgment(bad)
        e=events(self.db)[0];e['backfill']={'attempts':2};e['last_schedule_search_at']=NOW.isoformat()
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),e['event_id']))
        self.assertEqual(self.judgment(bad),'same_rejected_answer')
    def test_delayed_followup_can_advance_through_existing_guards(self):
        for days,stage in [(180,'起訴'),(730,'判決')]:
            db=connect();import_snapshot(db,self.root)
            g=group(article(stage,'https://example.test/late',(NOW+timedelta(days=days)).isoformat()))
            answer=report(g['articles'][0],current_stage=stage)
            self.assertEqual(judge(db,g,answer,NOW+timedelta(days=days)),'accepted')
            self.assertEqual(events(db)[0]['current_stage'],stage)
            db.close()
    def test_revision_projection_and_progress_projection_match_payload(self):
        self.assertEqual(self.judgment(),'accepted')
        updated=events(self.db)[0]
        self.assertEqual(self.db.execute('SELECT count(*) FROM progress').fetchone()[0],len(updated['progress_history']))
        self.assertEqual(self.db.execute("SELECT count(*) FROM event_articles WHERE role='current'").fetchone()[0],len(updated['articles']))
    def test_budget_exhaustion_keeps_group_and_unexamined_candidates(self):
        self.assertEqual(self.judgment(budget_available=False),'budget_deferred')
        row=self.db.execute("SELECT payload FROM holds WHERE stage='judgment'").fetchone()
        self.assertEqual(json.loads(row[0])['group'],self.g)
        self.assertEqual(self.db.execute("SELECT count(*) FROM candidates WHERE state='unexamined'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM decisions').fetchone()[0],0)
    def test_daily_monthly_caps_and_imported_ledger_not_reset(self):
        from ai_cost_control import periods
        day,month=periods(NOW)
        for key,amount in [('days',200000),('months',5000000)]:
            state={'days':{day:{'reserved_micro_usd':0,'calls':0}},'months':{month:{'reserved_micro_usd':0,'calls':0}}}
            state[key][day if key=='days' else month]['reserved_micro_usd']=amount
            with self.db:self.db.execute('INSERT OR REPLACE INTO budget VALUES(?,?)',('legacy',dumps(state)))
            self.assertEqual(reserve_mock(self.db,100,NOW)['status'],'budget_deferred')
            self.assertEqual(json.loads(self.db.execute("SELECT payload FROM budget WHERE name='legacy'").fetchone()[0]),state)
    def test_reservation_is_simulation_only(self):
        result=reserve_mock(self.db,100,NOW)
        self.assertEqual(result['status'],'reserved_simulation_only')
        self.assertGreater(result['reserved_micro_usd'],0)
    def test_import_interruption_rolls_back_all_tables(self):
        db=connect()
        with self.assertRaises(RuntimeError):import_snapshot(db,self.root,fail_after=0)
        for table in ['source_files','events','articles','revisions','event_articles']:
            self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        db.close()
    def test_save_interruption_rolls_back_state_and_links(self):
        counts=list(self.db.execute('SELECT count(*) FROM revisions'))
        with self.assertRaises(RuntimeError):self.judgment(fail_save=True)
        self.assertEqual(events(self.db),[self.e])
        self.assertEqual(list(self.db.execute('SELECT count(*) FROM revisions')),counts)
        self.assertEqual(self.db.execute('SELECT count(*) FROM changes').fetchone()[0],0)
    def test_no_candidates_does_not_create_new_event(self):
        with self.db:self.db.execute('DELETE FROM event_articles');self.db.execute('DELETE FROM aliases');self.db.execute('DELETE FROM events')
        self.assertEqual(rank(self.db,self.g),[])
        self.assertEqual(self.judgment({'relation':'new_event','confidence':'high','evidence_urls':[self.g['articles'][0]['url']]}),'needs_evidence')
        self.assertEqual(events(self.db),[])
    def test_many_candidates_preserved_and_old_event_searchable(self):
        with self.db:
            for i in range(100):
                e=copy.deepcopy(self.e);e['event_id']='other'+str(i);e['event_name']='別案件'+str(i);e['articles']=[];e['lifecycle']='completed'
                self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        rows=rank(self.db,self.g)
        self.assertEqual(len(rows),101)
        self.assertIn('e1',[r['event_id'] for r in rows])
    def test_schedule_postpone_cancel_and_complete_keep_history(self):
        from tracking_v1 import merge_schedules
        e=copy.deepcopy(self.e)
        for title,status in [('公園A開業予定2027年3月','scheduled'),('公園A開業延期2027年4月予定','scheduled'),('公園A開業中止','cancelled'),('公園A正式開業しました','completed')]:
            a=article(title,url='https://example.test/schedule')
            merge_schedules(e,[{'label':'開業','quote':a['title'],'source_url':a['url'],'status':status}],[a])
            self.assertEqual(e['schedules'][0]['status'],status)
        self.assertEqual(len(e['schedules'][0]['history']),3)
    def test_old_and_new_validation_agree_on_same_inputs(self):
        for answer in [report(self.g['articles'][0]),report(self.g['articles'][0],identity_matches=[])]:
            db=connect();import_snapshot(db,self.root)
            legacy=copy.deepcopy([self.e])
            with patch('event_tracker.load_corrections',return_value={}):
                waiting=merge_incoming(legacy,[self.g],FakeAI(answer),NOW,triage={})
            result=judge(db,self.g,answer,NOW)
            self.assertEqual(result=='accepted',not waiting)
            self.assertEqual(events(db),legacy)
            db.close()


if __name__=='__main__':unittest.main()
