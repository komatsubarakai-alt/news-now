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
from failed_response import digest
import revision_sync as sync
import publication as pub
import end_to_end as flow

class PublicationTests(unittest.TestCase):
    write=fixtures.PrototypeTests.write
    tearDown=fixtures.PrototypeTests.tearDown
    def setUp(self):
        fixtures.PrototypeTests.setUp(self);flow.initialize(self.db);pub.initialize(self.db)
        self.source=self.g['articles'][0];self.url=self.source['url'];self.body='SYNTHETIC 本文。金額100万円。'
        sync.seed(self.db,self.e['articles'][0]);sync.seed(self.db,self.source,body_text=self.body)
        self.proofs={}
        for a in [self.e['articles'][0],self.source]:self.proofs[a['url']]=sync.add_evidence(self.db,'e1',a['url'],'title',a['title'])
        self.did,self.adoption=self.guard('initial')
    def guard(self,name,evidence=None,group=None,**report_values):
        group=group or self.g;ids=evidence or list(self.proofs.values())
        jid=sync.make_job(self.db,'e1',group,ids,NOW)
        answer=copy.deepcopy(TRACKING_RESPONSE);answer.update(report(group['articles'][0],**report_values))
        sync.call_mock(self.db,jid,lambda p:answer,NOW)
        result=sync.adopt(self.db,name,jid,NOW);self.assertEqual(result['status'],'accepted')
        return result['decision_id'],result['adoption_id']
    def approve(self,op,kind,key,**kw):
        current=pub.target(events(self.db)[0],kind,key)
        proof=[self.proofs.get(key,self.proofs[self.url])] if kind=='article' else [self.proofs[self.url]]
        args=dict(expected_hash=digest(current),actor='simulation:operator',at=NOW,evidence_ids=proof,decision_ids=[self.did],basis='SYNTHETIC: 対象と原文を確認した模擬承認')
        args.update(kw);return pub.approve(self.db,op,'e1',kind,key,**args)['approval_id']
    def basic(self):
        refs=[self.approve('article:'+a['url'],'article',a['url']) for a in events(self.db)[0]['articles']]
        identity=self.approve('event','event','identity')
        return identity,refs
    def projection(self):return pub.project(self.db,{})
    def state(self,aid):return self.db.execute('SELECT status FROM pub_state WHERE approval_id=?',(aid,)).fetchone()[0]
    def withdraw(self,aid,op='withdraw',**kw):return pub.withdraw(self.db,op,aid,actor='simulation:operator',at=NOW,reason='SYNTHETIC 掲載取消し',**kw)
    def test_no_approval_no_public_event(self):
        self.assertEqual(self.projection()['events'],[]);self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
    def test_identity_does_not_approve_everything(self):
        self.basic();out=self.projection()['events'][0]
        self.assertEqual(out['summary'],'確認中');self.assertEqual(out['progress_count'],0);self.assertEqual(out['progress_history'],[])
        self.assertEqual(out['event_id'],'e1');self.assertEqual({a['url'] for a in out['articles']},{a['url'] for a in events(self.db)[0]['articles']})
    def test_correct_field_and_progress_approval_only(self):
        self.basic();self.approve('stage','field','current_stage')
        p=events(self.db)[0]['progress_history'][0];self.approve('progress','progress',pub.key_for('progress',p))
        out=self.projection()['events'][0];self.assertEqual(out['current_stage'],'起訴');self.assertEqual(out['progress_count'],1)
        self.assertEqual(out['summary'],'確認中');self.assertEqual(out['progress_history'],[p])
    def test_record_contains_actor_time_version_quote_decision_and_content(self):
        aid=self.approve('stage','field','current_stage');row=self.db.execute('SELECT * FROM pub_approvals WHERE approval_id=?',(aid,)).fetchone()
        self.assertEqual(row[6],'simulation:operator');self.assertEqual(row[7],NOW.isoformat());self.assertEqual(row[4],digest(json.loads(row[5])))
        proof=json.loads(row[9]);self.assertEqual(proof['decisions'][0]['decision_id'],self.did)
        self.assertEqual(proof['evidence'][0]['article_version'],sync.version_id(self.source));self.assertEqual(proof['evidence'][0]['quote'],self.source['title'])
    def test_wrong_hash_rejected(self):
        with self.assertRaises(ValueError):self.approve('bad','event','identity',expected_hash='stale')
    def test_wrong_decision_scope_rejected(self):
        with self.assertRaises(ValueError):self.approve('bad','event','identity',decision_ids=['missing'])
    def test_evaluation_review_is_not_guarded_adoption(self):
        review=sync.record_reference(self.db,'eval','e1','operator','same_event',{'scope':'evaluation_reference_only'},list(self.proofs.values()),NOW)
        with self.assertRaises(ValueError):self.approve('bad','event','identity',decision_ids=[review['decision_id']])
    def test_missing_quote_rejected(self):
        ev=sync.add_evidence(self.db,'e1',self.url,'metadata')
        with self.assertRaises(ValueError):self.approve('bad','event','identity',evidence_ids=[ev])
    def test_no_automatic_approval_after_guarded_adoption(self):
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
        self.assertEqual(self.projection()['events'],[])
    def test_heading_change_invalidates_approval_immediately(self):
        self.basic();aid=self.approve('stage','field','current_stage');a=copy.deepcopy(self.source);a['title']+=' 訂正'
        sync.change_source(self.db,'heading',a,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(self.state(aid),'needs_recheck');self.assertEqual(self.projection()['events'],[])
    def test_body_change_invalidates_body_dependent_approval(self):
        body=sync.add_evidence(self.db,'e1',self.url,'body','100万円');sync.cancel(self.db,'redo-body-job',self.adoption,NOW);did,_=self.guard('with-body',list(self.proofs.values())+[body])
        self.did=did;aid=self.approve('body','field','current_stage',evidence_ids=[body])
        sync.change_source(self.db,'body-change',self.source,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body.replace('100','120'))
        self.assertEqual(self.state(aid),'needs_recheck')
    def test_deleted_source_invalidates_and_preserves_history(self):
        self.basic();before=events(self.db)
        sync.change_source(self.db,'delete',self.source,sync.head(self.db,self.url)['state_id'],NOW,outcome='deleted')
        self.assertEqual(self.projection()['events'],[]);self.assertEqual(events(self.db),before)
        self.assertTrue(self.db.execute("SELECT 1 FROM pub_state WHERE status='needs_recheck'").fetchone())
    def test_failed_fetch_does_not_invalidate(self):
        aid,_=self.basic();sync.change_source(self.db,'timeout',self.source,sync.head(self.db,self.url)['state_id'],NOW,outcome='request_failed',error='SYNTHETIC timeout')
        self.assertEqual(self.state(aid),'approved');self.assertEqual(len(self.projection()['events']),1)
    def test_target_change_marks_only_changed_field_approval(self):
        identity,_=self.basic();summary=self.approve('summary','field','summary');stage=self.approve('stage','field','current_stage')
        e=events(self.db)[0];e['summary']='SYNTHETIC 訂正本文'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'e1'))
        pub.refresh(self.db,'manual-correction',NOW)
        self.assertEqual(self.state(summary),'needs_recheck');self.assertEqual(self.state(identity),'approved');self.assertEqual(self.state(stage),'approved')
        self.assertEqual(self.projection()['events'][0]['summary'],'確認中')
    def test_cancel_judgment_invalidates_public_approval(self):
        aid,_=self.basic();sync.cancel(self.db,'cancel',self.adoption,NOW)
        self.assertEqual(self.state(aid),'needs_recheck');self.assertEqual(self.projection()['events'],[])
    def test_withdrawal_and_notice_do_not_republish_old_facts(self):
        aid,_=self.basic();flow.prepare_export(self.db,{})
        self.withdraw(aid);out=self.projection();self.assertEqual(out['events'],[])
        self.assertTrue(any(n['status']=='withdrawn' for n in out['notices']))
        self.assertNotIn(self.source['title'],dumps(out['notices']));self.assertNotIn('simulation:operator',dumps(out['notices']))
    def test_reapproval_requires_explicit_new_record(self):
        aid,_=self.basic();self.withdraw(aid)
        with self.assertRaises(ValueError):self.approve('implicit','event','identity')
        new=self.approve('reapprove','event','identity',supersedes=aid)
        self.assertNotEqual(new,aid);self.assertEqual(self.state(aid),'superseded');self.assertEqual(self.state(new),'approved');self.assertEqual(len(self.projection()['events']),1)
    def test_old_operation_receipt_cannot_reactivate_withdrawn(self):
        aid=self.approve('identity','event','identity');self.withdraw(aid)
        self.assertEqual(self.approve('identity','event','identity'),aid);self.assertEqual(self.state(aid),'withdrawn')
    def test_approval_failure_rolls_back_all(self):
        with self.assertRaises(RuntimeError):self.approve('failed','event','identity',failpoint='after_approval')
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
        self.approve('failed','event','identity');self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],1)
    def test_lost_approval_acknowledgment_is_idempotent(self):
        with self.assertRaises(RuntimeError):self.approve('lost','event','identity',failpoint='after_commit')
        aid=self.approve('lost','event','identity');self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],1);self.assertEqual(self.state(aid),'approved')
    def test_withdrawal_failure_and_retry(self):
        aid=self.approve('identity','event','identity')
        with self.assertRaises(RuntimeError):self.withdraw(aid,failpoint='after_withdrawal')
        self.assertEqual(self.state(aid),'approved');self.withdraw(aid);self.withdraw(aid)
        self.assertEqual(self.db.execute("SELECT count(*) FROM pub_history WHERE new_status='withdrawn'").fetchone()[0],1)
    def test_content_revert_does_not_reactivate_old_approval(self):
        aid=self.approve('stage','field','current_stage');e=events(self.db)[0];old=copy.deepcopy(e);e['current_stage']='SYNTHETIC訂正'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'e1'))
        pub.refresh(self.db,'change',NOW)
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(old),'e1'))
        pub.refresh(self.db,'restore',NOW);self.assertEqual(self.state(aid),'needs_recheck')
    def test_dependency_withdrawal_invalidates_only_dependents(self):
        identity,articles=self.basic();stage=self.approve('stage','field','current_stage',requires=[identity]);summary=self.approve('summary','field','summary')
        self.withdraw(identity);self.assertEqual(self.state(stage),'needs_recheck');self.assertEqual(self.state(summary),'approved')
        self.assertTrue(all(self.state(a)=='approved' for a in articles))
    def test_guarded_snapshot_mismatch_is_not_approvable(self):
        e=events(self.db)[0];e['summary']='SYNTHETIC unchecked claim'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'e1'))
        with self.assertRaises(ValueError):self.approve('bad','field','summary')
    def test_manifest_distinguishes_approved_from_comparison(self):
        self.basic();generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            info=flow.materialize(self.db,generation,out,snapshot_root=self.root);p=Path(info['directory']);manifest=json.loads((p/'manifest.json').read_text())
            self.assertEqual(len(json.loads((p/'public_events.json').read_text())),1)
            self.assertEqual(manifest['roles']['compatibility_preview.json'],'unapproved_comparison_only');self.assertFalse(manifest['can_publish'])
            self.assertEqual(manifest['scope'],'isolated_simulation')
    def test_approval_change_invalidates_old_export_generation(self):
        aid,_=self.basic();generation=flow.prepare_export(self.db,{});self.withdraw(aid)
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaisesRegex(ValueError,'stale'):flow.materialize(self.db,generation,out,snapshot_root=self.root)
    def test_requires_simulated_actor_and_real_time_value(self):
        with self.assertRaises(ValueError):self.approve('bad','event','identity',actor='real-operator')
        with self.assertRaises(ValueError):self.approve('bad','event','identity',at=NOW.replace(tzinfo=None))
    def test_correction_then_explicit_reapproval(self):
        identity,articles=self.basic();flow.prepare_export(self.db,{})
        corrected=copy.deepcopy(self.source);corrected['title']+=' SYNTHETIC 訂正'
        sync.change_source(self.db,'correct',corrected,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(self.state(identity),'needs_recheck')
        self.source=corrected;self.g['articles']=[corrected]
        self.proofs[self.url]=sync.add_evidence(self.db,'e1',self.url,'title',corrected['title'])
        self.did,_=self.guard('corrected-adoption')
        for index,a in enumerate(events(self.db)[0]['articles']):self.approve('re-article:'+str(index),'article',a['url'],supersedes=articles[index])
        new=self.approve('re-identity','event','identity',supersedes=identity)
        out=self.projection();self.assertEqual(len(out['events']),1);self.assertEqual(self.state(new),'approved')
        self.assertTrue(any(n['status']=='reapproved' and n['current_status']=='approved' for n in out['notices']))
        self.assertTrue(events(self.db)[0]['source_revisions'])
    def second_event(self):
        from test_tracking import article
        other=copy.deepcopy(self.e);other['event_id']='e2';other['articles']=[copy.deepcopy(self.source)]
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',('e2',dumps(other)))
        incoming=copy.deepcopy(self.source);incoming['url']='https://example.test/e2-followup'
        sync.seed(self.db,incoming)
        oldproof=sync.add_evidence(self.db,'e2',other['articles'][0]['url'],'title',other['articles'][0]['title'])
        sharedproof=sync.add_evidence(self.db,'e2',self.url,'title',self.source['title'])
        newproof=sync.add_evidence(self.db,'e2',incoming['url'],'title',incoming['title'])
        group=copy.deepcopy(self.g);group['articles']=[incoming];jid=sync.make_job(self.db,'e2',group,[oldproof,sharedproof,newproof],NOW)
        answer=copy.deepcopy(TRACKING_RESPONSE);answer.update(report(incoming));answer['event_id']='e2'
        answer['identity_matches'][0]['existing_url']=other['articles'][0]['url']
        for anchor in answer['identity_matches'][0]['anchors']:anchor['existing_quote']=self.source['title']
        sync.call_mock(self.db,jid,lambda p:answer,NOW);result=sync.adopt(self.db,'adopt-e2',jid,NOW)
        self.assertEqual(result['status'],'accepted')
        def approve(op,kind,key,proof):
            value=pub.target(pub.event_by_id(self.db,'e2'),kind,key)
            return pub.approve(self.db,op,'e2',kind,key,expected_hash=digest(value),actor='simulation:operator',at=NOW,evidence_ids=[proof],decision_ids=[result['decision_id']],basis='SYNTHETIC 事件Bの見出し確認')['approval_id']
        shared=approve('e2-article','article',self.url,sharedproof)
        identity=approve('e2-identity','event','identity',sharedproof)
        return identity,shared,result['decision_id']
    def test_shared_url_withdrawal_does_not_withdraw_other_event(self):
        identity,_=self.basic();other,shared,_=self.second_event();self.withdraw(identity)
        self.assertEqual(self.state(other),'approved');self.assertEqual(self.state(shared),'approved')
        self.assertEqual([e['event_id'] for e in self.projection()['events']],['e2'])
    def test_shared_url_body_change_preserves_title_only_other_event(self):
        # Rebuild A's guard with a body dependency, but B depends on title only.
        body=sync.add_evidence(self.db,'e1',self.url,'body','100万円');sync.cancel(self.db,'reset-A',self.adoption,NOW)
        self.did,self.adoption=self.guard('body-A',list(self.proofs.values())+[body]);identity,_=self.basic()
        other,_,_=self.second_event()
        sync.change_source(self.db,'shared-body',self.source,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body.replace('100','120'))
        self.assertEqual(self.state(identity),'needs_recheck');self.assertEqual(self.state(other),'approved')
        self.assertEqual([e['event_id'] for e in self.projection()['events']],['e2'])
    def test_shared_url_heading_change_rechecks_both_dependent_events(self):
        identity,_=self.basic();other,_,_=self.second_event();a=copy.deepcopy(self.source);a['title']+=' SYNTHETIC訂正'
        sync.change_source(self.db,'both-heading',a,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(self.state(identity),'needs_recheck');self.assertEqual(self.state(other),'needs_recheck')
    def test_projection_excludes_unapproved_articles_and_derives_counts(self):
        self.approve('one-article','article',self.url);self.approve('identity','event','identity')
        out=self.projection()['events'][0];self.assertEqual(out['article_count'],1)
        self.assertEqual([a['url'] for a in out['articles']],[self.url]);self.assertEqual(len(events(self.db)[0]['articles']),2)
    def test_article_approval_requires_own_source_quote(self):
        with self.assertRaises(ValueError):self.approve('wrong-source','article',self.e['articles'][0]['url'],evidence_ids=[self.proofs[self.url]])
    def test_old_source_version_restored_does_not_reactivate_approval(self):
        aid=self.approve('stage','field','current_stage');a=copy.deepcopy(self.source);a['title']+=' SYNTHETIC変更'
        sync.change_source(self.db,'change-source',a,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        sync.change_source(self.db,'restore-source',self.source,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.assertEqual(self.state(aid),'needs_recheck')
    def test_filesystem_failure_after_approval_resumes_without_new_approval(self):
        self.basic();generation=flow.prepare_export(self.db,{});before=self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0]
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(RuntimeError):flow.materialize(self.db,generation,out,snapshot_root=self.root,failpoint='after_first_file')
            a=flow.materialize(self.db,generation,out,snapshot_root=self.root);b=flow.materialize(self.db,generation,out,snapshot_root=self.root)
            self.assertEqual(a,b)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],before)
    def test_cancelled_source_transaction_does_not_invalidate_approvals(self):
        aid,_=self.basic();a=copy.deepcopy(self.source);a['title']+=' SYNTHETIC変更'
        with self.assertRaises(RuntimeError):sync.change_source(self.db,'failed-change',a,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body,failpoint='before_receipt')
        self.assertEqual(self.state(aid),'approved');self.assertEqual(len(self.projection()['events']),1)
    def test_disk_reopen_keeps_approval_and_history(self):
        aid,_=self.basic()
        with tempfile.TemporaryDirectory() as out:
            path=Path(out)/'publication.sqlite';disk=connect(str(path));self.db.backup(disk);disk.close()
            disk=connect(str(path));self.assertEqual(pub.project(disk,{})['events'][0]['event_id'],'e1')
            self.assertEqual(disk.execute('SELECT status FROM pub_state WHERE approval_id=?',(aid,)).fetchone()[0],'approved');disk.close()
    def test_schedule_and_correction_are_separate_targets(self):
        sync.cancel(self.db,'reset-history',self.adoption,NOW)
        revised=copy.deepcopy(self.source);revised['title']+=' 2026年11月15日 初公判予定'
        sync.change_source(self.db,'future-title',revised,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body)
        self.source=revised;self.g['articles']=[revised]
        self.proofs[self.url]=sync.add_evidence(self.db,'e1',self.url,'title',revised['title'])
        e=events(self.db)[0];e['corrections']=[{'at':NOW.isoformat(),'content':'SYNTHETIC 訂正記録','source_urls':[self.url]}]
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'e1'))
        self.did,self.adoption=self.guard('history-adoption',schedules=[{'label':'初公判','quote':revised['title'],'source_url':self.url}]);self.basic()
        self.assertEqual(self.projection()['events'][0]['schedules'],[]);self.assertEqual(self.projection()['events'][0]['corrections'],[])
        e=events(self.db)[0];self.assertEqual(len(e['schedules']),1)
        self.approve('plan','schedule',e['schedules'][0]['schedule_id']);self.approve('correction','correction',pub.key_for('correction',e['corrections'][0]))
        out=self.projection()['events'][0];self.assertEqual(len(out['schedules']),1);self.assertEqual(len(out['corrections']),1)
    def test_approved_reader_rejects_renamed_comparison_even_with_new_hash(self):
        import hashlib
        self.basic();generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            result=flow.materialize(self.db,generation,out,snapshot_root=self.root);path=Path(result['directory'])
            self.assertEqual(len(flow.load_approved_simulation(self.db,generation,path)['events']),1)
            data=(path/'compatibility_preview.json').read_bytes();(path/'public_events.json').write_bytes(data)
            manifest=json.loads((path/'manifest.json').read_text());manifest['files']['public_events.json']=hashlib.sha256(data).hexdigest();(path/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'database generation'):flow.load_approved_simulation(self.db,generation,path)
    def test_approved_reader_rejects_old_files_after_withdrawal(self):
        aid,_=self.basic();generation=flow.prepare_export(self.db,{})
        with tempfile.TemporaryDirectory() as out:
            result=flow.materialize(self.db,generation,out,snapshot_root=self.root);self.withdraw(aid)
            with self.assertRaisesRegex(ValueError,'stale'):flow.load_approved_simulation(self.db,generation,result['directory'])
    def test_unprovided_body_quote_cannot_be_approval_proof(self):
        missing=sync.add_evidence(self.db,'e1',self.e['articles'][0]['url'],'body','SYNTHETIC missing')
        with self.assertRaises(ValueError):self.approve('missing','event','identity',evidence_ids=[missing])
    def test_latest_approval_not_legacy_receipt_is_used(self):
        old,_=self.basic();self.withdraw(old)
        new=self.approve('re-identity','event','identity',supersedes=old)
        self.assertEqual(self.approve('event','event','identity'),old)
        self.assertEqual(self.state(new),'approved');self.assertEqual(len(self.projection()['events']),1)
    def test_unrelated_event_cannot_be_approval_prerequisite(self):
        self.basic();other,_,_=self.second_event()
        with self.assertRaises(ValueError):self.approve('bad-requires','field','current_stage',requires=[other])
    def test_generated_notice_timestamp_is_not_invented(self):
        self.basic();flow.prepare_export(self.db,{})
        e=events(self.db)[0];e['event_name']='SYNTHETIC identity correction'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'e1'))
        pub.project(self.db,{})
        self.assertTrue(any(n['at'] is None for n in self.projection()['notices']))
    def test_source_update_failure_rolls_back_publication_history(self):
        identity,_=self.basic();flow.prepare_export(self.db,{})
        count=self.db.execute('SELECT count(*) FROM pub_history').fetchone()[0]
        a=copy.deepcopy(self.source);a['title']+=' SYNTHETIC変更'
        with self.assertRaises(RuntimeError):sync.change_source(self.db,'failed-notice',a,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body,failpoint='before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_history').fetchone()[0],count)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_notices').fetchone()[0],0);self.assertEqual(self.state(identity),'approved')
    def test_article_body_proof_change_does_not_clear_source_history(self):
        self.basic();original=self.db.execute('SELECT * FROM sync_source_states').fetchall()
        sync.change_source(self.db,'new-body',self.source,sync.head(self.db,self.url)['state_id'],NOW,body_text='SYNTHETIC 新本文')
        states=self.db.execute('SELECT * FROM sync_source_states').fetchall()
        self.assertTrue(all(r in states for r in original));self.assertEqual(len(events(self.db)),1)
    def test_existing_34_stay_unapproved_without_migration(self):
        from stage_compare import ROOT
        from engine import import_snapshot
        saved=connect();import_snapshot(saved,ROOT);pub.initialize(saved)
        before=events(saved);registry=json.loads((ROOT/'data_corrections.json').read_text())
        self.assertEqual(len(flow.legacy_public(saved,registry)),34)
        projected=pub.project(saved,registry)
        self.assertEqual(projected['events'],[]);self.assertEqual(saved.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
        self.assertEqual(events(saved),before);self.assertEqual(projected['notices'],[])
        self.assertEqual(sum(h['reason']=='event_identity_unapproved' for h in projected['holds']),34)
        saved.close()
    def test_body_only_reapproval_waits_for_new_guarded_judgment(self):
        body=sync.add_evidence(self.db,'e1',self.url,'body','100万円');sync.cancel(self.db,'reset-body-only',self.adoption,NOW)
        self.did,self.adoption=self.guard('guard-body-only',list(self.proofs.values())+[body])
        aid=self.approve('body-only','field','current_stage',evidence_ids=[body])
        sync.change_source(self.db,'body-only-change',self.source,sync.head(self.db,self.url)['state_id'],NOW,body_text=self.body.replace('100','120'))
        newproof=sync.add_evidence(self.db,'e1',self.url,'body','120万円')
        with self.assertRaises(ValueError):self.approve('body-only-reapproval','field','current_stage',evidence_ids=[newproof],supersedes=aid)
        self.assertEqual(self.state(aid),'needs_recheck')
    def test_reapproval_does_not_reuse_superseded_prerequisite(self):
        aid,_=self.basic()
        with self.assertRaises(ValueError):self.approve('bad-cycle','event','identity',requires=[aid],supersedes=aid)
    def test_explicit_evidence_invalidation_keeps_original_and_other_event(self):
        identity,_=self.basic();other,_,_=self.second_event();ev=self.proofs[self.url]
        before=self.db.execute('SELECT * FROM sync_evidence WHERE evidence_id=?',(ev,)).fetchone()
        result=pub.invalidate_evidence(self.db,'invalidate',ev,actor='simulation:operator',at=NOW,reason='SYNTHETIC 根拠失効')
        self.assertIn(identity,result['affected_approvals']);self.assertEqual(self.state(identity),'needs_recheck');self.assertEqual(self.state(other),'approved')
        self.assertEqual(self.db.execute('SELECT * FROM sync_evidence WHERE evidence_id=?',(ev,)).fetchone(),before)
        with self.assertRaises(ValueError):self.approve('reuse-bad-proof','event','identity',supersedes=identity)
    def test_evidence_invalidation_rolls_back_and_retry_is_idempotent(self):
        identity,_=self.basic();args=dict(actor='simulation:operator',at=NOW,reason='SYNTHETIC 根拠失効')
        with self.assertRaises(RuntimeError):pub.invalidate_evidence(self.db,'invalidate-failure',self.proofs[self.url],failpoint='after_evidence_invalidation',**args)
        self.assertEqual(self.state(identity),'approved');self.assertEqual(self.db.execute('SELECT count(*) FROM pub_evidence_invalidations').fetchone()[0],0)
        pub.invalidate_evidence(self.db,'invalidate-failure',self.proofs[self.url],**args);pub.invalidate_evidence(self.db,'invalidate-failure',self.proofs[self.url],**args)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_evidence_invalidations').fetchone()[0],1)
    def test_empty_body_is_recorded_and_requires_review(self):
        identity,_=self.basic();before=events(self.db)
        sync.change_source(self.db,'empty',self.source,sync.head(self.db,self.url)['state_id'],NOW,outcome='empty_body')
        self.assertEqual(self.state(identity),'needs_recheck');self.assertEqual(events(self.db),before)
        self.assertEqual(sync.head(self.db,self.url)['availability'],'empty_body')
    def test_every_displayed_quote_needs_its_own_source_proof(self):
        proof=[{'url':self.url,'quote':self.source['title']}]
        pub.verify_display_quotes({'quote':self.source['title'],'source_url':self.url},proof)
        with self.assertRaises(ValueError):pub.verify_display_quotes({'quote':'SYNTHETIC 根拠にない引用','source_url':self.url},proof)
        with self.assertRaises(ValueError):pub.verify_display_quotes({'quote':self.source['title'],'source_url':'https://example.test/wrong'},proof)
    def test_non_temporary_database_cannot_receive_publication_records(self):
        disk=connect(str(self.root/'not-the-configured-temp.sqlite'))
        with patch('publication.tempfile.gettempdir',return_value='/different-isolation-root'):
            with self.assertRaises(ValueError):pub.initialize(disk)
        self.assertFalse(disk.execute("SELECT 1 FROM sqlite_master WHERE name='pub_approvals'").fetchone());disk.close()
