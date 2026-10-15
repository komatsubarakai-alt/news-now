import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import operator_app as app
from operator_demo import populate
from engine import connect, events
import publication as pub
import revision_sync as sync

class OperatorAppTests(unittest.TestCase):
    def setUp(self):
        self.db=connect();app.initialize(self.db);populate(self.db)
    def tearDown(self): self.db.close()
    def state(self): return app.snapshot(self.db)
    def target(self,kind='event',key='identity'):
        return next(t for t in self.state()['items'][0]['targets'] if t['kind']==kind and t['key']==key)
    def command(self,action='approve',**kw):
        c=dict(op='test-op-0001',action=action,event_id='demo-e1',revision=self.state()['revision'],kind='event',key='identity',confirmed=True,reason='SYNTHETIC 明示確認');c.update(kw);return c
    def approve_basics(self):
        for i,t in enumerate(self.state()['items'][0]['targets']):
            if t['kind'] in ('event','article'):
                app.operate(self.db,self.command(op='base-op-'+str(i),kind=t['kind'],key=t['key']))
    def test_snapshot_never_approves_during_readiness_check(self):
        first=self.state();self.assertEqual(first,self.state());self.assertEqual(first['approved_preview'],[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
        self.assertTrue(all(t['ready'] for t in first['items'][0]['targets']))
    def test_only_approved_projection_is_preview(self):
        self.approve_basics();s=self.state();self.assertEqual(s['approved_preview'],pub.project(self.db,{})['events']);self.assertEqual(len(s['approved_preview']),1)
        self.assertEqual(s['approved_preview'][0]['summary'],'確認中');self.assertFalse(s['can_publish'])
    def test_fields_and_progress_approve_individually(self):
        self.approve_basics();app.operate(self.db,self.command(kind='field',key='current_stage'))
        p=next(t for t in self.state()['items'][0]['targets'] if t['kind']=='progress');app.operate(self.db,self.command(op='test-progress',kind='progress',key=p['key']))
        self.assertEqual(self.state()['approved_preview'][0]['progress_count'],1)
    def test_confirmation_required(self):
        with self.assertRaisesRegex(ValueError,'confirmation'): app.operate(self.db,self.command(confirmed=False))
    def test_stale_screen_rejected(self):
        cmd=self.command();app.operate(self.db,self.command(op='other-op-001',kind='field',key='current_stage'))
        with self.assertRaisesRegex(ValueError,'古く'):app.operate(self.db,cmd)
    def test_same_command_double_send_exact_receipt(self):
        c=self.command();a=app.operate(self.db,c);self.assertEqual(app.operate(self.db,c),a)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],1)
    def test_operation_id_changed_input_rejected(self):
        c=self.command();app.operate(self.db,c);c['reason']='different'
        with self.assertRaisesRegex(ValueError,'reused'):app.operate(self.db,c)
    def test_fault_before_receipt_rolls_back_approval_and_history(self):
        c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
        app.operate(self.db,c);self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],1)
    def test_lost_ack_does_not_reapprove(self):
        c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'after_commit')
        app.operate(self.db,c);self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],1)
    def test_withdraw_then_explicit_reapprove(self):
        self.approve_basics();app.operate(self.db,self.command(action='withdraw'))
        self.assertEqual(self.state()['approved_preview'],[])
        app.operate(self.db,self.command(op='test-reapprove'))
        self.assertEqual(len(self.state()['approved_preview']),1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM pub_state WHERE status='superseded'").fetchone()[0],1)
    def test_source_heading_change_marks_affected_only(self):
        self.approve_basics();old=self.state();a=copy.deepcopy(events(self.db)[0]['articles'][-1]);a['title']+=' 訂正'
        sync.change_source(self.db,'synthetic-heading',a,sync.head(self.db,a['url'])['state_id'],app.now())
        s=self.state();self.assertEqual(s['approved_preview'],[]);self.assertTrue(any(j['status']=='needs_recheck' for j in s['items'][0]['judgments']))
        self.assertNotEqual(old['revision'],s['revision']);self.assertTrue(any(t['status']=='needs_recheck' for t in s['items'][0]['targets']))
    def test_missing_evidence_blocks_even_direct_post(self):
        self.db.execute('DELETE FROM sync_dependencies');self.db.commit()
        self.assertFalse(self.target()['ready'])
        with self.assertRaises(ValueError):app.operate(self.db,self.command())
    def test_operator_judgment_is_reference_not_adoption(self):
        item=self.state()['items'][0];before=events(self.db);calls=self.db.execute('SELECT count(*) FROM sync_reservations').fetchone()[0]
        app.operate(self.db,self.command(action='review',result='same_event',group={'articles':item['event']['articles']}))
        self.assertEqual(events(self.db),before);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_reservations').fetchone()[0],calls)
        self.assertEqual(self.state()['approved_preview'],[])
    def test_hold_requires_reason(self):
        item=self.state()['items'][0]
        with self.assertRaises(ValueError):app.operate(self.db,self.command(action='review',result='hold',reason='',group={'articles':item['event']['articles']}))
    def test_operator_hold_invalidates_and_replacement_never_auto_reapproves(self):
        self.approve_basics();item=self.state()['items'][0];group={'articles':item['event']['articles']}
        app.operate(self.db,self.command(action='review',result='hold',group=group));self.assertEqual(self.state()['approved_preview'],[])
        app.operate(self.db,self.command(op='replacement-001',action='review',result='same_event',group=group));self.assertEqual(self.state()['approved_preview'],[])
        self.assertTrue(self.target()['ready']);self.assertEqual(self.target()['status'],'needs_recheck')
    def test_review_group_tampering_rejected(self):
        with self.assertRaisesRegex(ValueError,'scope'):app.operate(self.db,self.command(action='review',result='same_event',group={'articles':[{'url':'invented'}]}))
    def test_queue_keeps_all_candidates_and_hold(self):
        q=self.state()['queues'][0];self.assertEqual([c['event_id'] for c in q['candidates']],['demo-e1'])
        app.operate(self.db,self.command(action='queue_review',event_id=None,queue_id=q['queue_id'],result='hold'))
        after=self.state()['queues'][0];self.assertEqual(after['group'],q['group']);self.assertEqual(after['candidates'],q['candidates']);self.assertEqual(len(after['records']),1)
    def test_queue_missing_evidence_stops_same_event(self):
        q=self.state()['queues'][0];self.db.execute('DELETE FROM sync_evidence WHERE evidence_id NOT IN (SELECT evidence_id FROM sync_dependencies)');self.db.commit()
        self.db.execute("UPDATE sync_evidence SET quote=NULL");self.db.commit()
        with self.assertRaisesRegex(ValueError,'不足'):app.operate(self.db,self.command(action='queue_review',queue_id=q['queue_id'],result='same_event'))
    def test_unknown_candidate_rejected(self):
        q=self.state()['queues'][0]
        with self.assertRaises(ValueError):app.operate(self.db,self.command(action='queue_review',event_id='unknown',queue_id=q['queue_id'],result='hold'))
    def test_disk_restart_preserves_state_and_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'ui.sqlite';db=app.open_owned(p);populate(db);c=self.command();c['revision']=app.snapshot(db)['revision'];r=app.operate(db,c);saved=app.snapshot(db);db.close()
            db=app.open_owned(p);self.assertEqual(saved,app.snapshot(db));self.assertEqual(r,app.operate(db,c));db.close()
    def test_existing_unmarked_db_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'x.sqlite';db=connect(p);db.close();before=p.read_bytes()
            with self.assertRaises(ValueError):app.open_owned(p)
            self.assertEqual(before,p.read_bytes())
    def test_repository_db_path_rejected(self):
        with self.assertRaises(ValueError):app.open_owned(app.ROOT/'ui.sqlite')
    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td);(p/'target').touch();(p/'link').symlink_to(p/'target')
            with self.assertRaises(ValueError):app.open_owned(p/'link')
    def test_body_and_unknown_source_not_claimed_verified(self):
        for a in self.state()['items'][0]['articles']:
            self.assertEqual(a['version']['availability'],'unknown')
            self.assertTrue(all(not e['verification']['source_verified'] for e in a['evidence']))

    def test_hypothetical_preview_rolls_back_and_matches_actual_approval(self):
        self.approve_basics();before=self.state()
        command=self.command(op='preview-field',kind='field',key='current_stage')
        projection=app.preview_target(self.db,command)
        self.assertEqual(before,self.state());self.assertEqual(projection['scope'],'hypothetical_not_approved')
        app.operate(self.db,command);self.assertEqual(projection['after'],self.state()['approved_preview'])
    def test_hypothetical_stale_input_rejected(self):
        with self.assertRaises(ValueError):app.preview_target(self.db,self.command(revision='stale'))
    def test_missing_proof_stops_hypothetical_preview(self):
        self.db.execute('DELETE FROM sync_dependencies');self.db.commit()
        with self.assertRaises(ValueError):app.preview_target(self.db,self.command())
    def test_invalid_reason_type_rejected_without_side_effects(self):
        before=self.state()
        with self.assertRaises(ValueError):app.operate(self.db,self.command(reason=[]))
        self.assertEqual(before,self.state())
    def test_existing_rehearsal_copy_keeps_history_readonly(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'source.sqlite';db=app.open_owned(src);populate(db)
            state=app.snapshot(db);db.close();before=src.read_bytes()
            copied=app.copy_isolated(src,Path(td)/'copy.sqlite')
            self.assertEqual(state,app.snapshot(copied));copied.close();self.assertEqual(before,src.read_bytes())
    def test_copy_destination_cannot_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'source.sqlite';db=app.open_owned(src);db.close();before=src.read_bytes()
            with self.assertRaises(ValueError):app.copy_isolated(src,src)
            self.assertEqual(before,src.read_bytes())
    def test_catalog_retains_schedule_and_correction_without_autoapproval(self):
        event=copy.deepcopy(events(self.db)[0]);event['schedules']=[{'schedule_id':'demo-plan','date':'2026-12-01'}]
        event['corrections']=[{'reason':'SYNTHETIC 訂正'}]
        self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(app.dumps(event),'demo-e1'));self.db.commit()
        ts=self.state()['items'][0]['targets']
        self.assertEqual({t['kind'] for t in ts}&{'schedule','correction'},{'schedule','correction'})
        self.assertTrue(all(not t['ready'] for t in ts if t['kind'] in ('schedule','correction')))

if __name__=='__main__':unittest.main()
