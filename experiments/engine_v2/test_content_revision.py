import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_engine as fixtures
from test_tracking import NOW,report
from engine import connect,events,dumps
from event_tracker import TRACKING_RESPONSE
from failed_response import digest
from operator_review import digest as operator_digest
from pending_triage import fingerprint,group_key
import revision_sync as sync
import content_revision as revision
import publication as pub
import end_to_end as flow

class ContentRevisionTests(unittest.TestCase):
    write=fixtures.PrototypeTests.write
    tearDown=fixtures.PrototypeTests.tearDown
    def setUp(self):
        fixtures.PrototypeTests.setUp(self);flow.initialize(self.db);pub.initialize(self.db)
        self.article=self.g['articles'][0];self.url=self.article['url']
        self.old_body='SYNTHETIC 江別豊幌住宅強盗 被害者山田。金額は100万円。容疑者を起訴。'
        self.new_body=self.old_body.replace('100','120')
        sync.seed(self.db,self.e['articles'][0]);sync.seed(self.db,self.article,body_text=self.old_body)
        self.old_proof=sync.add_evidence(self.db,'e1',self.url,'body','100万円')
        self.title_proof=sync.add_evidence(self.db,'e1',self.url,'title',self.article['title'])
        job=sync.make_job(self.db,'e1',self.g,[self.old_proof,self.title_proof],NOW)
        sync.call_mock(self.db,job,lambda p:self.answer(),NOW)
        adopted=sync.adopt(self.db,'initial',job,NOW);self.assertEqual(adopted['status'],'accepted')
        self.old_did=adopted['decision_id'];self.calls=[]
    def answer(self,**values):
        value=copy.deepcopy(TRACKING_RESPONSE);value.update(report(self.article,**values));return value
    def change(self,body=None,article=None,outcome='updated',op='body-change'):
        kw={'body_text':body or self.new_body} if outcome=='updated' else {'outcome':outcome}
        if outcome=='request_failed':kw['error']='SYNTHETIC timeout'
        return sync.change_source(self.db,op,article or self.article,sync.head(self.db,self.url)['state_id'],NOW,**kw)
    def quote(self,text='120万円'):return sync.add_evidence(self.db,'e1',self.url,'body',text)
    def mock(self,prompt):
        self.calls.append(prompt);return self.answer(current_stage='起訴',summary='金額は120万円。',latest='金額を訂正',progress_evidence=[{'url':self.url,'quote':'金額は120万円。容疑者を起訴。'}])
    def run_revision(self,request='rejudge',proof=None,**kw):return revision.rejudge(self.db,request,'e1',self.g,[proof or self.quote()],self.mock,NOW,**kw)
    def budget(self):return self.db.execute('SELECT * FROM budget').fetchall()
    def approve(self,op,kind,key,proof,did,supersedes=None):
        value=pub.target(events(self.db)[0],kind,key)
        return pub.approve(self.db,op,'e1',kind,key,expected_hash=digest(value),actor='simulation:operator',at=NOW,evidence_ids=[proof],decision_ids=[did],basis='SYNTHETIC 検証用承認',supersedes=supersedes)['approval_id']
    def test_body_only_revision_is_new_judgment_and_canonical_metadata(self):
        before=copy.deepcopy(events(self.db)[0]['articles']);self.change();result=self.run_revision()
        self.assertEqual(result['status'],'accepted');self.assertNotEqual(result['decision_id'],self.old_did)
        event=events(self.db)[0];self.assertEqual(event['articles'],before);self.assertEqual(event['summary'],'金額は120万円。')
        self.assertNotIn(revision.MARKER,dumps(event));self.assertEqual(len(self.calls),1)
        self.assertTrue(event['source_revisions'][-1]['body_content_hash']);self.assertEqual(self.db.execute('SELECT count(*) FROM sync_revision_validations').fetchone()[0],2)
    def test_existing_guards_all_run(self):
        import engine
        self.change()
        with patch('engine.preflight',wraps=engine.preflight) as preflight,patch('engine.evidence',wraps=engine.evidence) as evidence,patch('engine.apply_report',wraps=engine.apply_report) as apply,patch('engine.validate_saved_update',wraps=engine.validate_saved_update) as save:
            self.assertEqual(self.run_revision()['status'],'accepted')
            self.assertGreater(preflight.call_count,0);self.assertGreater(evidence.call_count,0);self.assertEqual(apply.call_count,1);self.assertEqual(save.call_count,1)
    def test_unchanged_body_no_callback_reservation_or_adoption(self):
        before=self.budget();result=self.run_revision(proof=self.old_proof)
        self.assertEqual(result['status'],'already_saved');self.assertEqual(self.calls,[]);self.assertEqual(self.budget(),before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],1)
    def test_identical_observation_is_not_revision(self):
        self.change(body=self.old_body);self.assertEqual(self.run_revision(proof=self.old_proof)['status'],'already_saved');self.assertEqual(self.calls,[])
    def test_heading_only_revision_uses_existing_metadata_guards(self):
        updated=copy.deepcopy(self.article);updated['title']+=' SYNTHETIC 見出し訂正';self.change(body=self.old_body,article=updated)
        self.article=updated;self.g['articles']=[updated];ev=sync.add_evidence(self.db,'e1',self.url,'title',updated['title'])
        result=revision.rejudge(self.db,'heading','e1',self.g,[ev],lambda p:self.answer(),NOW);self.assertEqual(result['status'],'accepted');self.assertEqual(events(self.db)[0]['articles'][-1]['title'],updated['title'])
    def test_new_judgment_does_not_approve_publication(self):
        old_article=self.approve('article','article',self.url,self.old_proof,self.old_did)
        old_identity=self.approve('identity','event','identity',self.old_proof,self.old_did);flow.prepare_export(self.db,{})
        self.change();result=self.run_revision();self.assertEqual(result['status'],'accepted');self.assertEqual(pub.project(self.db,{})['events'],[])
        ev=self.quote();self.approve('new-article','article',self.url,ev,result['decision_id'],old_article)
        self.approve('new-identity','event','identity',ev,result['decision_id'],old_identity)
        self.approve('new-summary','field','summary',ev,result['decision_id'])
        projected=pub.project(self.db,{})['events'];self.assertEqual(len(projected),1);self.assertEqual(projected[0]['summary'],'金額は120万円。')
    def test_old_judgment_and_old_quote_cannot_reapprove(self):
        self.change();self.run_revision()
        with self.assertRaises(ValueError):self.approve('bad','field','summary',self.old_proof,self.old_did)
    def test_old_request_receipt_does_not_become_current_again(self):
        self.change();ev=self.quote();result=self.run_revision(proof=ev);self.change(body=self.new_body.replace('120','130'),op='third')
        before=len(self.calls);self.assertEqual(self.run_revision(proof=ev)['status'],'needs_recheck');self.assertEqual(len(self.calls),before)
    def test_uncertain_answer_keeps_all_sources_and_history(self):
        self.change();before=events(self.db)
        result=revision.rejudge(self.db,'uncertain','e1',self.g,[self.quote()],lambda p:copy.deepcopy(TRACKING_RESPONSE),NOW)
        self.assertEqual(result['status'],'needs_evidence');self.assertEqual(events(self.db),before)
        self.assertTrue(self.db.execute('SELECT command_payload FROM sync_revision_requests WHERE request_id=?',('uncertain',)).fetchone()[0])
    def test_missing_body_stops_without_mock_or_budget(self):
        self.change(outcome='empty_body');ev=sync.add_evidence(self.db,'e1',self.url,'body');before=self.budget()
        self.assertEqual(self.run_revision(proof=ev)['status'],'body_evidence_unavailable');self.assertEqual(self.calls,[]);self.assertEqual(self.budget(),before)
    def test_fetch_failure_stops_without_invalidating_old_approval(self):
        self.change(outcome='request_failed');before=self.budget()
        self.assertEqual(self.run_revision(proof=self.old_proof)['status'],'source_fetch_pending');self.assertEqual(self.calls,[]);self.assertEqual(self.budget(),before)
    def test_deleted_source_stops_without_mock(self):
        self.change(outcome='deleted');ev=sync.add_evidence(self.db,'e1',self.url,'body')
        self.assertEqual(self.run_revision(proof=ev)['status'],'body_evidence_unavailable');self.assertEqual(self.calls,[])
    def test_no_repeated_rejected_answer_or_budget(self):
        self.change();before=events(self.db);ev=self.quote()
        def invalid(prompt):self.calls.append(prompt);return self.answer(multi_event=True)
        a=revision.rejudge(self.db,'bad','e1',self.g,[ev],invalid,NOW);budget=self.budget()
        b=revision.rejudge(self.db,'bad','e1',self.g,[ev],invalid,NOW)
        self.assertEqual(a['status'],'needs_evidence');self.assertEqual(a,b);self.assertEqual(len(self.calls),1);self.assertEqual(self.budget(),budget);self.assertEqual(events(self.db),before)
    def test_failure_before_receipt_rolls_back_binding_and_can_resume(self):
        self.change();before=revision.bindings(self.db,'e1');events_before=events(self.db)
        with self.assertRaises(RuntimeError):self.run_revision(failpoint='before_receipt')
        self.assertEqual(revision.bindings(self.db,'e1'),before);self.assertEqual(events(self.db),events_before)
        self.assertEqual(self.run_revision()['status'],'accepted');self.assertEqual(len(self.calls),1)
    def test_lost_commit_ack_and_request_repeat_no_double_adoption(self):
        self.change()
        with self.assertRaises(RuntimeError):self.run_revision(failpoint='after_commit')
        budget=self.budget();self.assertEqual(self.run_revision()['status'],'accepted');self.assertEqual(self.budget(),budget)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],2);self.assertEqual(len(self.calls),1)
    def test_cancel_then_readopt_restores_binding_and_history(self):
        self.change();before=revision.bindings(self.db,'e1');result=self.run_revision()
        sync.cancel(self.db,'cancel-revision',result['adoption_id'],NOW);self.assertEqual(revision.bindings(self.db,'e1'),before)
        repeated=self.run_revision('readopt');self.assertEqual(repeated['status'],'accepted');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_adoptions').fetchone()[0],3)
    def test_request_id_cannot_be_reused_with_new_evidence(self):
        self.change();self.run_revision();self.change(body=self.new_body.replace('120','130'),op='third')
        with self.assertRaises(ValueError):self.run_revision(proof=self.quote('130万円'))
    def test_marker_is_never_accepted_as_source_quotation(self):
        self.change();ev=self.quote()
        def wrong(prompt):return self.answer(progress_evidence=[{'url':self.url,'quote':revision.MARKER}])
        result=revision.rejudge(self.db,'marker','e1',self.g,[ev],wrong,NOW)
        self.assertEqual(result['status'],'quote_validation_failed')
    def test_old_version_cache_is_not_used_for_changed_body(self):
        initial=self.db.execute('SELECT cache_key FROM sync_jobs').fetchone()[0];self.change();result=self.run_revision()
        new=self.db.execute('SELECT cache_key FROM sync_jobs WHERE job_id=?',(result['job_id'],)).fetchone()[0]
        self.assertNotEqual(initial,new);self.assertEqual(len(self.calls),1)
    def test_operator_selection_is_preserved_in_guard_view(self):
        self.change();selection={'action':'new_event','event_id':'','source_fingerprint':fingerprint(self.g),'group_hash':operator_digest(self.g)}
        triage={group_key(self.g):{'operator_review':selection}}
        self.assertEqual(self.run_revision(triage=triage)['status'],'needs_evidence')
    def test_manual_hold_is_not_lost_by_body_view_translation(self):
        self.change();triage={group_key(self.g):{'status':'operator_excluded','source_fingerprint':fingerprint(self.g)}}
        self.assertEqual(self.run_revision(triage=triage)['status'],'operator_excluded')
    def test_wrong_event_cannot_adopt_owned_article_revision(self):
        self.change();other=copy.deepcopy(self.e);other['event_id']='e2';other['articles'][0]['url']='https://example.test/unrelated'
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',('e2',dumps(other)))
        sync.seed(self.db,other['articles'][0]);ev=sync.add_evidence(self.db,'e2',self.url,'body','120万円')
        result=revision.rejudge(self.db,'wrong','e2',self.g,[ev],lambda p:self.answer(event_id='e2'),NOW)
        self.assertNotEqual(result['status'],'accepted')
    def test_disk_restart_recovers_cached_response(self):
        self.change();ev=self.quote()
        with tempfile.TemporaryDirectory() as out:
            path=Path(out)/'revision.sqlite';disk=connect(str(path));self.db.backup(disk)
            with self.assertRaises(RuntimeError):revision.rejudge(disk,'disk','e1',self.g,[ev],self.mock,NOW,failpoint='before_receipt')
            disk.close();disk=connect(str(path));result=revision.rejudge(disk,'disk','e1',self.g,[ev],self.mock,NOW)
            self.assertEqual(result['status'],'accepted');self.assertEqual(disk.execute('PRAGMA foreign_key_check').fetchall(),[]);disk.close();self.assertEqual(len(self.calls),1)
    def shared_event(self,body_dependent=True):
        other=copy.deepcopy(self.e);other['event_id']='e2';other['articles']=[copy.deepcopy(self.article)]
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',('e2',dumps(other)))
        incoming=copy.deepcopy(self.article);incoming['url']='https://example.test/e2-followup';sync.seed(self.db,incoming)
        title=sync.add_evidence(self.db,'e2',self.url,'title',self.article['title']);newtitle=sync.add_evidence(self.db,'e2',incoming['url'],'title',incoming['title'])
        ids=[title,newtitle]
        if body_dependent:ids.append(sync.add_evidence(self.db,'e2',self.url,'body','100万円'))
        group=copy.deepcopy(self.g);group['articles']=[incoming]
        jid=sync.make_job(self.db,'e2',group,ids,NOW)
        answer=self.answer(event_id='e2');answer['evidence_urls']=[incoming['url']]
        answer['progress_evidence']=[{'url':incoming['url'],'quote':incoming['title']}]
        for match in answer['identity_matches']:
            match['existing_url']=self.url;match['incoming_url']=incoming['url']
            for a in match['anchors']:a['existing_quote']=self.article['title'];a['incoming_quote']=incoming['title']
        sync.call_mock(self.db,jid,lambda p:answer,NOW);result=sync.adopt(self.db,'initial-B',jid,NOW);self.assertEqual(result['status'],'accepted')
        return result['decision_id']
    def test_shared_body_rejudges_each_event_against_its_own_version(self):
        b=self.shared_event();self.change();a_result=self.run_revision()
        ev=sync.add_evidence(self.db,'e2',self.url,'body','120万円')
        def second(prompt):
            answer=self.mock(prompt);answer['event_id']='e2'
            for match in answer['identity_matches']:
                match['existing_url']=self.url
                for anchor in match['anchors']:anchor['existing_quote']=self.article['title']
            return answer
        result=revision.rejudge(self.db,'B-rejudge','e2',self.g,[ev],second,NOW)
        self.assertEqual(a_result['status'],'accepted');self.assertEqual(result['status'],'accepted')
        self.assertEqual({e['event_id'] for e in events(self.db)},{'e1','e2'})
        self.assertEqual(revision.bindings(self.db,'e1')[self.url]['edition_id'],revision.bindings(self.db,'e2')[self.url]['edition_id'])
    def test_body_change_does_not_invalidate_title_only_other_event(self):
        b=self.shared_event(body_dependent=False);cached=self.db.execute('SELECT * FROM sync_answer_cache').fetchall();self.change();self.run_revision()
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(b,)).fetchone()[0],'current')
        self.assertTrue(all(row in self.db.execute('SELECT * FROM sync_answer_cache').fetchall() for row in cached))
    def test_approved_progress_has_its_actual_body_quote_bound_to_new_judgment(self):
        self.change();result=self.run_revision();proof=self.quote()
        self.approve('article','article',self.url,proof,result['decision_id']);self.approve('identity','event','identity',proof,result['decision_id'])
        progress=events(self.db)[0]['progress_history'][-1];quote=progress['progress_evidence'][0]['quote'];ev=sync.add_evidence(self.db,'e1',self.url,'body',quote)
        self.approve('progress','progress',pub.key_for('progress',progress),ev,result['decision_id'])
        self.assertEqual(pub.project(self.db,{})['events'][0]['progress_count'],1)
    def test_body_reverted_to_old_text_still_needs_new_judgment(self):
        self.change();self.change(body=self.old_body,op='restore')
        ev=sync.add_evidence(self.db,'e1',self.url,'body','100万円')
        result=revision.rejudge(self.db,'restored','e1',self.g,[ev],lambda p:self.answer(meaningful_change=False),NOW)
        self.assertEqual(result['status'],'accepted');self.assertNotEqual(result['decision_id'],self.old_did)
        self.assertEqual(events(self.db)[0]['progress_count'],1)
    def test_body_never_obtained_cannot_trigger_rejudgment(self):
        original=self.e['articles'][0];group=copy.deepcopy(self.g);group['articles']=[original]
        ev=sync.add_evidence(self.db,'e1',original['url'],'body');before=self.budget()
        result=revision.rejudge(self.db,'unobtained','e1',group,[ev],self.mock,NOW)
        self.assertEqual(result['status'],'body_evidence_unavailable');self.assertEqual(self.calls,[]);self.assertEqual(self.budget(),before)
    def test_budget_stop_preserves_request_and_source_version(self):
        self.change()
        with patch('revision_sync.reserve_mock',return_value={'status':'budget_deferred'}):result=self.run_revision()
        self.assertEqual(result['status'],'budget_deferred');self.assertEqual(self.calls,[])
        self.assertTrue(self.db.execute('SELECT command_payload FROM sync_revision_requests').fetchone()[0])
        self.assertEqual(self.run_revision()['status'],'accepted')
    def test_same_rejection_new_operation_reuses_validation_memo(self):
        self.change();ev=self.quote();calls=[]
        def uncertain(p):calls.append(p);return copy.deepcopy(TRACKING_RESPONSE)
        a=revision.rejudge(self.db,'first-failure','e1',self.g,[ev],uncertain,NOW);budget=self.budget()
        b=revision.rejudge(self.db,'second-failure','e1',self.g,[ev],uncertain,NOW)
        self.assertEqual(a['status'],'needs_evidence');self.assertEqual(b['status'],'needs_evidence');self.assertTrue(b['memo_reused'])
        self.assertEqual(len(calls),1);self.assertEqual(self.budget(),budget)
    def test_save_before_response_cache_is_uncertain_and_not_recalled(self):
        self.change()
        with self.assertRaises(RuntimeError):self.run_revision(transport_failpoint='after_response_before_cache')
        result=self.run_revision();self.assertEqual(result['status'],'uncertain_response_requires_reconciliation');self.assertEqual(len(self.calls),1)
    def test_reservation_failure_resumes_without_second_reservation(self):
        self.change()
        with self.assertRaises(RuntimeError):self.run_revision(transport_failpoint='after_reserve')
        budget=self.budget();self.assertEqual(self.run_revision()['status'],'accepted');self.assertEqual(self.budget(),budget);self.assertEqual(len(self.calls),1)
    def test_invalid_original_quote_does_not_update_canonical_data(self):
        self.change();before=events(self.db);ev=self.quote()
        result=revision.rejudge(self.db,'unproved','e1',self.g,[ev],lambda p:self.answer(progress_evidence=[{'url':self.url,'quote':'書かれていない起訴'}]),NOW)
        self.assertEqual(result['status'],'quote_validation_failed');self.assertEqual(events(self.db),before)
    def test_unknown_event_target_retains_request_without_mock(self):
        self.change();result=revision.rejudge(self.db,'unknown','missing',self.g,[self.quote()],self.mock,NOW)
        self.assertEqual(result['status'],'needs_evidence');self.assertEqual(self.calls,[])
        self.assertTrue(self.db.execute('SELECT command_payload FROM sync_revision_requests WHERE request_id=?',('unknown',)).fetchone()[0])
    def test_empty_article_group_is_invalid_not_already_saved(self):
        group=copy.deepcopy(self.g);group['articles']=[]
        result=revision.rejudge(self.db,'empty','e1',group,[],self.mock,NOW)
        self.assertEqual(result['status'],'invalid_source');self.assertEqual(self.calls,[])
    def test_budget_hold_is_resolved_and_transition_history_retained(self):
        self.change()
        with patch('revision_sync.reserve_mock',return_value={'status':'budget_deferred'}):self.run_revision()
        self.run_revision()
        row=self.db.execute('SELECT payload FROM holds WHERE stage=?',('revision:rejudge',)).fetchone()
        self.assertEqual(json.loads(row[0])['status'],'accepted')
        states=[r[0] for r in self.db.execute('SELECT new_status FROM sync_revision_transitions WHERE request_id=? ORDER BY rowid',('rejudge',))]
        self.assertEqual(states,['ready','budget_deferred','accepted'])
    def test_initial_attachment_cancelled_then_fresh_body_can_be_readopted(self):
        sync.cancel(self.db,'cancel-initial','initial',NOW);self.change()
        result=self.run_revision('readopt-original');self.assertEqual(result['status'],'accepted')
        self.assertIn(self.url,{a['url'] for a in events(self.db)[0]['articles']})
