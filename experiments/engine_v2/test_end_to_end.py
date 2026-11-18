import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_engine as fixtures
from engine import connect,events,dumps
from event_tracker import TRACKING_RESPONSE
from test_tracking import NOW,report
import staged
import revision_sync as sync
import end_to_end as flow

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        fixtures.PrototypeTests.setUp(self);flow.initialize(self.db)
        self.sid=flow.begin(self.db,self.g)
        self.calls=[]
    tearDown=fixtures.PrototypeTests.tearDown
    write=fixtures.PrototypeTests.write
    def mock(self,prompt,batch):
        self.calls.append(prompt);a=copy.deepcopy(TRACKING_RESPONSE);a.update(report(self.g['articles'][0]));return a
    def advance(self,**kw):return flow.step(self.db,self.sid,self.mock,NOW,**kw)
    def test_guarded_adoption_and_cursor_share_transaction(self):
        state=self.advance();self.assertEqual(state['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_links WHERE is_current=1').fetchone()[0],2)
        self.assertEqual(len(state['records']),1)
    def test_failed_cursor_rolls_back_adoption_but_keeps_response(self):
        before=events(self.db)
        with self.assertRaises(RuntimeError):self.advance(failpoint='before_cursor')
        self.assertEqual(events(self.db),before);self.assertEqual(staged.inspect(self.db,self.sid)['cursor'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0],1)
        self.assertEqual(self.advance()['status'],'accepted');self.assertEqual(len(self.calls),1)
    def test_guard_failure_rolls_back_then_resume(self):
        with self.assertRaises(RuntimeError):self.advance(failpoint='after_guard')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],0)
        self.assertEqual(self.advance()['status'],'accepted');self.assertEqual(len(self.calls),1)
    def test_repeated_run_no_double_adoption_or_budget(self):
        self.advance(charge=True);before=self.db.execute('SELECT * FROM budget').fetchall()
        self.advance(charge=True)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.db.execute('SELECT * FROM budget').fetchall(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
    def test_budget_stop_keeps_cursor_group_candidates(self):
        state=self.advance(budget_available=False)
        self.assertEqual(state['status'],'budget_deferred');self.assertEqual(state['cursor'],0)
        self.assertEqual(state['group'],self.g);self.assertTrue(state['candidate_states']);self.assertEqual(self.calls,[])
        self.assertEqual(self.advance()['status'],'accepted')
    def test_actual_simulation_cap_stops_without_cache_or_loss(self):
        with patch('end_to_end.reserve_mock',return_value={'status':'budget_deferred'}):state=self.advance(charge=True)
        self.assertEqual(state['status'],'budget_deferred');self.assertEqual(state['cursor'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_reservations').fetchone()[0],0)
    def test_after_reserve_failure_can_resume_without_double_reserve(self):
        first=self.advance(charge=True,transport_failpoint='after_reserve');self.assertEqual(first['status'],'retry_api')
        budget=self.db.execute('SELECT * FROM budget').fetchall();self.assertEqual(self.advance(charge=True)['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT * FROM budget').fetchall(),budget);self.assertEqual(len(self.calls),1)
    def test_captured_response_survives_before_stage_cache(self):
        self.assertEqual(self.advance(transport_failpoint='after_cache')['status'],'retry_api')
        self.assertEqual(self.advance()['status'],'accepted');self.assertEqual(len(self.calls),1)
    def test_uncertain_response_gap_is_not_recalled(self):
        self.assertEqual(self.advance(transport_failpoint='after_response_before_cache')['status'],'retry_api')
        self.assertEqual(self.advance()['status'],'retry_api');self.assertEqual(len(self.calls),1)
        self.assertEqual(staged.inspect(self.db,self.sid)['cursor'],0)
    def test_uncertain_is_retained_not_different(self):
        state=flow.step(self.db,self.sid,lambda p,b:copy.deepcopy(TRACKING_RESPONSE),NOW)
        self.assertEqual(state['status'],'needs_evidence');self.assertTrue(all(v=='examined_unresolved' for v in state['candidate_states'].values()))
        self.assertEqual(self.db.execute('SELECT outcome FROM sync_decisions').fetchone()[0],'unresolved')
    def test_invalid_schema_keeps_reference_and_cursor(self):
        state=flow.step(self.db,self.sid,lambda p,b:{'relation':'same_event'},NOW)
        self.assertEqual(state['status'],'needs_evidence');self.assertEqual(state['records'][0]['outcome'],'invalid_answer_schema_or_target')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],1)
    def test_candidate_first4_then_entire_remainder(self):
        # Reinitialize session against a larger catalog; no candidate is cut.
        for i in range(2,13):
            e=copy.deepcopy(self.e);e['event_id']='e'+str(i);e['event_name']='other'+str(i);e['articles'][0]['url']='https://example.test/'+str(i)
            with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        sid=flow.begin(self.db,self.g);sizes=[]
        def unknown(prompt,batch):sizes.append(len(batch));return copy.deepcopy(TRACKING_RESPONSE)
        flow.step(self.db,sid,unknown,NOW);state=flow.step(self.db,sid,unknown,NOW)
        self.assertEqual(sizes,[4,8]);self.assertEqual(state['cursor'],12);self.assertEqual(len(state['candidate_states']),12)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_decisions').fetchone()[0],12)
    def test_target_not_presented_never_adopted(self):
        def bad(prompt,batch):a=self.mock(prompt,batch);a['event_id']='unpresented';return a
        state=flow.step(self.db,self.sid,bad,NOW);self.assertNotEqual(state['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
    def test_selected_source_change_invalidates_only_dependent_ai(self):
        self.advance();article=copy.deepcopy(self.g['articles'][0]);article['title']+=' 訂正'
        result=sync.change_source(self.db,'title',article,sync.head(self.db,article['url'])['state_id'],NOW)
        self.assertEqual(len(result['affected_decisions']),1)
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state').fetchone()[0],'needs_recheck')
    def test_cancel_and_readopt_preserve_history(self):
        self.advance();adoption=self.db.execute('SELECT adoption_id FROM sync_adoptions').fetchone()[0]
        self.assertEqual(sync.cancel(self.db,'cancel',adoption,NOW)['status'],'cancelled')
        sid=flow.begin(self.db,self.g);self.assertEqual(flow.step(self.db,sid,self.mock,NOW)['status'],'accepted')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions WHERE cancelled=1').fetchone()[0],1)
        self.assertEqual(len(self.calls),1)
    def test_no_mock_fact_enters_strict_public_json(self):
        self.advance();generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            result=flow.materialize(self.db,generation,out,snapshot_root=self.root)
            path=Path(result['directory']);self.assertEqual(json.loads((path/'public_events.json').read_text()),[])
            self.assertEqual(len(json.loads((path/'compatibility_preview.json').read_text())),1)
            self.assertFalse(json.loads((path/'manifest.json').read_text())['can_publish'])
    def test_export_crash_and_resume_manifest_idempotent(self):
        self.advance();generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(RuntimeError):flow.materialize(self.db,generation,out,snapshot_root=self.root,failpoint='after_first_file')
            self.assertFalse((Path(out)/generation/'manifest.json').exists())
            first=flow.materialize(self.db,generation,out,snapshot_root=self.root)
            second=flow.materialize(self.db,generation,out,snapshot_root=self.root);self.assertEqual(first,second)
    def test_export_stale_after_revision_is_rejected(self):
        self.advance();generation=flow.prepare_export(self.db,{})
        a=copy.deepcopy(self.g['articles'][0]);a['title']+=' 訂正';sync.change_source(self.db,'change',a,sync.head(self.db,a['url'])['state_id'],NOW)
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError,'stale'):flow.materialize(self.db,generation,out,snapshot_root=self.root)
    def test_export_rejects_snapshot_and_symlinks(self):
        generation=flow.prepare_export(self.db,{})
        with self.assertRaises(ValueError):flow.materialize(self.db,generation,str(self.root),snapshot_root=self.root)
        with tempfile.TemporaryDirectory() as out:
            link=Path(out)/'link';link.symlink_to(Path(out)/'elsewhere')
            with self.assertRaises(ValueError):flow.materialize(self.db,generation,str(link),snapshot_root=self.root)
    def test_tampered_export_does_not_get_manifest(self):
        generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            path=Path(out)/generation;path.mkdir();(path/'public_events.json').write_text('[123]')
            with self.assertRaises(ValueError):flow.materialize(self.db,generation,out,snapshot_root=self.root)
            self.assertFalse((path/'manifest.json').exists())
    def test_candidate_catalog_over_64_is_not_cut(self):
        for i in range(2,71):
            e=copy.deepcopy(self.e);e['event_id']='e'+str(i);e['event_name']='other'+str(i);e['articles'][0]['url']='https://example.test/'+str(i)
            with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        sid=flow.begin(self.db,self.g);sizes=[]
        def unknown(prompt,batch):sizes.append(len(batch));return copy.deepcopy(TRACKING_RESPONSE)
        flow.step(self.db,sid,unknown,NOW);state=flow.step(self.db,sid,unknown,NOW)
        self.assertEqual(sizes,[4,66]);self.assertEqual(len(state['candidate_states']),70)
    def test_source_changed_during_mock_cannot_advance_cursor(self):
        def changing(prompt,batch):
            answer=self.mock(prompt,batch);a=copy.deepcopy(self.g['articles'][0]);a['title']+=' 訂正'
            sync.change_source(self.db,'mid-response',a,sync.head(self.db,a['url'])['state_id'],NOW)
            return answer
        with self.assertRaises(ValueError):flow.step(self.db,self.sid,changing,NOW)
        self.assertEqual(staged.inspect(self.db,self.sid)['cursor'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
    def test_raw_answer_cache_survives_dependent_source_change(self):
        original=self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0]
        self.advance();self.assertEqual(self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0],original+1)
        a=copy.deepcopy(self.g['articles'][0]);a['title']+=' 訂正'
        sync.change_source(self.db,'revise',a,sync.head(self.db,a['url'])['state_id'],NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_answer_cache').fetchone()[0],original+1)
    def test_restart_disk_recovers_cached_reply_and_adoption(self):
        with tempfile.TemporaryDirectory() as out:
            path=Path(out)/'shadow.sqlite';disk=connect(str(path));self.db.backup(disk);disk.close()
            disk=connect(str(path))
            with self.assertRaises(RuntimeError):flow.step(disk,self.sid,self.mock,NOW,failpoint='before_cursor')
            disk.close();disk=connect(str(path))
            state=flow.step(disk,self.sid,self.mock,NOW);self.assertEqual(state['status'],'accepted');self.assertEqual(len(self.calls),1)
            self.assertEqual(disk.execute('PRAGMA foreign_key_check').fetchall(),[]);disk.close()
    def test_operator_recheck_blocks_adoption_not_cursor_history(self):
        a=self.g['articles'][0];ev=sync.add_evidence(self.db,'e1',a['url'],'body')
        review=sync.record_reference(self.db,'operator','e1','operator','hold',{'scope':'synthetic'},[ev],NOW)
        state=self.advance();self.assertNotEqual(state['status'],'accepted')
        self.assertEqual(state['records'][0]['outcome'],'operator_reconfirmation_required')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(review['decision_id'],)).fetchone()[0],'current')
    def test_progress_schedule_correction_fields_compatible(self):
        self.advance();event=events(self.db)[0];self.assertEqual(event['progress_count'],1)
        event['corrections']=[{'at':NOW.isoformat(),'content':'SYNTHETIC訂正','source_urls':[self.g['articles'][0]['url']]}]
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(event),event['event_id']))
        public=flow.legacy_public(self.db,{})
        self.assertEqual(public[0],event);self.assertEqual(flow.compare_public(public,copy.deepcopy(public))['differences'],[])
