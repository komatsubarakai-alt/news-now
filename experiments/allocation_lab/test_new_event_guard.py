import copy,hashlib,importlib.util,json,socket,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from .allocation import native,PLANS
from .new_event_guard import NewEventGuard,Snapshot,binding,digest,revision,MockOperatorConfirmation

STAMP='2020-01-02T00:00:00Z'

def source(value,name='架空港倉庫侵入事件',url='https://example.test/new'):
    return {'url':url,'title':f'{name} 警察案件{value} 被害者架空B 容疑者を逮捕','description':'','published':'2020-01-01T00:00:00Z'}
def identity(article,value):
    return {'namespace':'fictional-police-jurisdiction-1','value':value,'article_url':article['url'],'article_version':digest(article),'source_kind':'title','quote':article['title'],'source_confirmed_by_operator':True}
def event(number=1):
    value='F-100'+str(number);a=source(value,name='架空旧事件'+str(number),url='https://example.test/old'+str(number))
    return {'event_id':'fictional-e'+str(number),'event_name':'架空旧事件'+str(number),'articles':[a],'case_identity':identity(a,value),'publication_status':'held' if number==2 else 'published','lifecycle':'dormant'}
def group(value='F-2001'):
    return {'event_name':'架空港倉庫侵入事件','category':'事件・事故','tracking_value':'high','articles':[source(value)]}
def report(g):
    a=g['articles'][0]
    return {'relation':'new_event','event_id':'','confidence':'high','evidence_urls':[a['url']],
            'event_identity':{'value':g['event_name'],'url':a['url'],'quote':a['title']},
            'meaningful_change':False,'current_stage':'逮捕'}

class NewEventGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'guard.sqlite';self.file=Path(self.temp.name)/'events.json'
        self.events=[event()];self.group=group();self.guard=NewEventGuard(self.path);self.addCleanup(lambda:self.guard.close());self.snapshot=self.snap()
        for target,name in [(socket.socket,'connect'),(native,'response')]:
            guard=patch.object(target,name,side_effect=AssertionError('API/network forbidden'));guard.start();self.addCleanup(guard.stop)
    def snap(self):
        raw=json.dumps(self.events,ensure_ascii=False).encode();self.file.write_bytes(raw);return Snapshot(self.file,hashlib.sha256(raw).hexdigest())
    def queue(self,op='queue'):
        return self.guard.queue(op,self.group,self.snapshot,retrieved_ids=[])['context_id']
    def review(self,context,event_id='fictional-e1',op='review',authority='operator',relation='distinct',**changes):
        e=next(e for e in self.events if e['event_id']==event_id)
        r={'authority':authority,'relation':relation,'reviewer':'fictional-operator','checked_at':STAMP,'reason':'架空の同一管轄の案件番号を確認','separate_incident_confirmed_by_operator':True,'incident_distinction_reason':'架空の別の発生事実を独立に確認したという人手記録','binding_hash':context,'event_hash':digest(e),'incoming_identity':identity(self.group['articles'][0],'F-2001'),**changes}
        proof=MockOperatorConfirmation(context,'review',event_id,digest(r)) if authority=='operator' else None
        return self.guard.review(op,context,event_id,r,self.group,self.snapshot,confirmation=proof)
    def auth(self,c,value='F-2001'):
        return {'authority':'operator','reviewer':'fictional-operator','checked_at':STAMP,'reason':'架空の全候補確認完了','context_id':c,'authorize_lab_proposal':True,'identity':identity(self.group['articles'][0],value)}
    def decide(self,c,op='decide',authorization=None,**kw):
        proof=MockOperatorConfirmation(c,'new_proposal','',digest(authorization)) if authorization else None
        return self.guard.decide(op,c,self.group,self.snapshot,report(self.group),authorization,confirmation=proof,**kw)
    def test_ai_new_event_alone_is_held(self):
        c=self.queue();r=self.decide(c);self.assertEqual(r['state'],'held');self.assertEqual(self.guard.db.execute('SELECT count(*) FROM proposals').fetchone()[0],0)
    def test_original_native_duplicate_counterexample_is_held(self):
        root=Path(__file__).resolve().parents[2];spec=importlib.util.spec_from_file_location('guard_fictional_helpers',root/'tests/test_tracking.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
        self.events=[f.event()];self.group={'event_name':self.events[0]['event_name'],'category':'事件・事故','tracking_value':'high','articles':[f.article('容疑者を起訴','https://example.com/later',f.NOW.isoformat())]};self.snapshot=self.snap();c=self.queue()
        wrong=f.report(self.group['articles'][0],relation='new_event',event_id='')
        r=self.guard.decide('wrong-new',c,self.group,self.snapshot,wrong);self.assertEqual(r['state'],'held');self.assertEqual(len(self.events),1)
    def test_wrong_same_event_never_auto_merges(self):
        c=self.queue();wrong={**report(self.group),'relation':'same_event','event_id':'fictional-e1'};before=copy.deepcopy(self.events)
        r=self.guard.decide('wrong-same',c,self.group,self.snapshot,wrong);self.assertEqual(r['state'],'held');self.assertEqual(self.events,before)
    def test_retrieval_miss_is_still_unexamined(self):
        c=self.queue();self.assertEqual(self.guard.status(c),{'fictional-e1':'unexamined'})
    def test_all_events_include_held_and_dormant(self):
        self.events.append(event(2));self.snapshot=self.snap();c=self.queue();self.assertEqual(len(self.guard.status(c)),2)
    def test_truncated_snapshot_checksum_is_rejected(self):
        expected=self.snapshot.file_hash;self.file.write_text('[]')
        with self.assertRaises(ValueError):Snapshot(self.file,expected)
    def test_budget_interruption_keeps_unexamined_candidates(self):
        self.events.append(event(2));self.snapshot=self.snap();c=self.queue();self.review(c);self.guard.close();self.guard=NewEventGuard(self.path)
        self.assertEqual(self.guard.status(c)['fictional-e2'],'unexamined');self.assertEqual(self.decide(c,authorization=self.auth(c))['state'],'held')
    def test_ai_negative_answer_is_not_verified_distinct(self):
        c=self.queue();r=self.review(c,authority='ai');self.assertEqual(r['state'],'needs_review');self.assertEqual(self.decide(c,authorization=self.auth(c))['state'],'held')
    def test_all_distinct_without_independent_authorization_still_held(self):
        c=self.queue();self.review(c);self.assertEqual(self.decide(c)['state'],'held')
    def test_explicit_verified_mock_case_allows_lab_proposal_only(self):
        c=self.queue();self.review(c);r=self.decide(c,authorization=self.auth(c));self.assertEqual(r['state'],'lab_proposal_only');self.assertTrue(r['lab_id'].startswith('lab-only-'));self.assertEqual(r['new_production_ids'],0);self.assertFalse(r['publication_allowed'])
    def test_save_restart_and_same_operation_do_not_duplicate(self):
        c=self.queue();self.review(c);a=self.auth(c);r=self.decide(c,authorization=a);self.guard.close();self.guard=NewEventGuard(self.path);self.assertEqual(self.decide(c,authorization=a),r);self.assertEqual(self.guard.db.execute('SELECT count(*) FROM proposals').fetchone()[0],1)
    def test_new_operation_same_case_reuses_proposal(self):
        c=self.queue();self.review(c);a=self.auth(c);first=self.decide(c,authorization=a);second=self.decide(c,'new-operation',authorization=a);self.assertEqual(first['lab_id'],second['lab_id']);self.assertTrue(second['reused_proposal'])
    def test_other_context_never_auto_links_or_duplicates_by_case_number(self):
        c=self.queue();self.review(c);old=self.decide(c,authorization=self.auth(c));self.group['articles'][0]['description']='別の発生事実かもしれない新しい記事版'
        new=self.queue('queue-new');self.review(new,op='review-new');r=self.decide(new,'decide-new',authorization=self.auth(new))
        self.assertEqual(r['state'],'held');self.assertEqual(r['reason'],'other_lab_proposals_require_reconciliation');self.assertNotIn('lab_id',r);self.assertEqual(self.guard.db.execute('SELECT count(*) FROM proposals').fetchone()[0],1)
    def test_operation_id_conflict_is_refused(self):
        c=self.queue();self.decide(c)
        with self.assertRaises(ValueError):self.decide(c,authorization=self.auth(c))
    def test_save_failure_rolls_back_creation_and_operation(self):
        c=self.queue();self.review(c);a=self.auth(c)
        with self.assertRaises(OSError):self.decide(c,authorization=a,fail=True)
        self.assertEqual(self.guard.db.execute('SELECT count(*) FROM proposals').fetchone()[0],0);self.assertEqual(self.guard.db.execute('SELECT count(*) FROM proposal_links').fetchone()[0],0);self.assertEqual(self.decide(c,authorization=a)['state'],'lab_proposal_only')
    def test_queue_failure_rolls_back_catalogue(self):
        with self.assertRaises(OSError):self.guard.queue('queue',self.group,self.snapshot,fail=True)
        self.assertEqual(self.guard.db.execute('SELECT count(*) FROM contexts').fetchone()[0],0)
    def test_review_failure_preserves_unexamined(self):
        c=self.queue();e=self.events[0];r={'authority':'ai'}
        with self.assertRaises(OSError):self.guard.review('review',c,e['event_id'],r,self.group,self.snapshot,fail=True)
        self.assertEqual(self.guard.status(c)[e['event_id']],'unexamined')
    def test_article_revision_keeps_old_context_and_reviews(self):
        c=self.queue();self.review(c);old=copy.deepcopy(self.group);self.group['articles'][0]['title']+=' 訂正'
        self.assertEqual(self.decide(c,authorization=self.auth(c))['reason'],'stale_context');new=self.queue('revision');self.assertNotEqual(c,new);self.assertEqual(self.guard.status(new)['fictional-e1'],'unexamined')
        payload=json.loads(self.guard.db.execute('SELECT payload FROM contexts WHERE id=?',(c,)).fetchone()[0]);self.assertEqual(payload['group'],old);self.assertEqual(self.guard.db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)
    def test_event_added_invalidates_review(self):
        c=self.queue();self.review(c);self.events.append(event(2));self.snapshot=self.snap();self.assertEqual(self.decide(c,authorization=self.auth(c))['reason'],'stale_context')
    def test_snapshot_changed_on_disk_is_held(self):
        c=self.queue();self.file.write_text('[]');self.assertEqual(self.decide(c)['reason'],'stale_context')
    def test_old_success_is_not_reused_for_revised_article(self):
        c=self.queue();self.review(c);a=self.auth(c);self.assertEqual(self.decide(c,authorization=a)['state'],'lab_proposal_only');self.group['articles'][0]['description']='変更';self.assertEqual(self.decide(c,authorization=a)['reason'],'stale_context')
    def test_validator_change_requires_reconfirmation(self):
        c=self.queue()
        with patch('experiments.allocation_lab.new_event_guard.revision',return_value='new-validator'):self.assertEqual(self.decide(c)['reason'],'stale_context')
    def test_unknown_case_identity_is_not_invented(self):
        self.events[0].pop('case_identity');self.snapshot=self.snap();c=self.queue();self.assertEqual(self.review(c)['state'],'needs_review')
    def test_different_case_numbers_alone_are_not_distinct_proof(self):
        c=self.queue();self.assertEqual(self.review(c,separate_incident_confirmed_by_operator=False)['state'],'needs_review');self.assertEqual(self.decide(c,authorization=self.auth(c))['state'],'held')
    def test_missing_snapshot_is_held(self):
        c=self.queue();self.file.unlink();self.assertEqual(self.decide(c)['reason'],'stale_context')
    def test_counterexample_wrong_human_distinct_claim_can_pass_lab_proposal(self):
        # Same physical incident by construction, with two formal reference numbers.
        # BOTH independent human assertions are deliberately false in this fixture.
        self.events[0]['event_name']=self.group['event_name'];self.events[0]['articles'][0]['title']=self.group['articles'][0]['title'].replace('F-2001','F-1001')
        self.events[0]['case_identity']=identity(self.events[0]['articles'][0],'F-1001');self.snapshot=self.snap();c=self.queue();self.review(c)
        result=self.decide(c,authorization=self.auth(c));self.assertEqual(result['state'],'lab_proposal_only');self.assertEqual(result['new_production_ids'],0)
        # This is a documented residual semantic counterexample, NOT a safety pass.
    def test_same_formal_case_cannot_be_marked_distinct(self):
        self.group=group('F-1001');self.snapshot=self.snap();c=self.queue();incoming=identity(self.group['articles'][0],'F-1001');self.assertEqual(self.review(c,incoming_identity=incoming)['state'],'needs_review');self.assertEqual(self.decide(c)['state'],'held')
    def test_same_verified_blocks_new_event(self):
        self.group=group('F-1001');c=self.queue();incoming=identity(self.group['articles'][0],'F-1001');self.assertEqual(self.review(c,relation='same',incoming_identity=incoming)['state'],'same_verified');self.assertEqual(self.decide(c)['state'],'held')
    def test_wrong_version_in_quote_is_not_verified(self):
        c=self.queue();incoming=identity(self.group['articles'][0],'F-2001');incoming['article_version']='old';self.assertEqual(self.review(c,incoming_identity=incoming)['state'],'needs_review')
    def test_literal_match_does_not_confirm_source(self):
        c=self.queue();incoming=identity(self.group['articles'][0],'F-2001');incoming['source_confirmed_by_operator']=False;self.assertEqual(self.review(c,incoming_identity=incoming)['state'],'needs_review')
    def test_different_namespaces_are_not_distinct_proof(self):
        c=self.queue();incoming=identity(self.group['articles'][0],'F-2001');incoming['namespace']='different-jurisdiction';self.assertEqual(self.review(c,incoming_identity=incoming)['state'],'needs_review')
    def test_unknown_event_review_cannot_apply(self):
        c=self.queue()
        with self.assertRaises(ValueError):self.guard.review('outside',c,'not-in-snapshot',{},self.group,self.snapshot)
    def test_conflicting_review_is_not_last_write_wins(self):
        c=self.queue();self.review(c);self.assertEqual(self.review(c,op='conflicting',relation='same')['state'],'conflict');self.assertEqual(self.decide(c,authorization=self.auth(c))['state'],'held')
    def test_native_evidence_and_validation_are_still_required(self):
        c=self.queue();self.review(c);r={**report(self.group),'evidence_urls':['https://example.test/not-input']}
        a=self.auth(c);proof=MockOperatorConfirmation(c,'new_proposal','',digest(a));result=self.guard.decide('invalid',c,self.group,self.snapshot,r,a,confirmation=proof);self.assertEqual(result['reason'],'native_evidence_failed')
    def test_input_data_unchanged(self):
        c=self.queue();before=copy.deepcopy((self.group,self.events));raw=self.file.read_bytes();self.review(c);self.decide(c,authorization=self.auth(c));self.assertEqual((self.group,self.events),before);self.assertEqual(self.file.read_bytes(),raw)
    def test_AI_JSON_cannot_self_assert_operator_confirmation(self):
        c=self.queue();self.review(c);a=self.auth(c)
        forged={'context':c,'purpose':'new_proposal','event_id':'','record_hash':digest(a)}
        r=self.guard.decide('forged',c,self.group,self.snapshot,report(self.group),a,confirmation=forged);self.assertEqual(r['state'],'held')
    def test_confirmation_for_another_record_is_rejected(self):
        c=self.queue();self.review(c);a=self.auth(c);proof=MockOperatorConfirmation(c,'new_proposal','','wrong-record-hash')
        r=self.guard.decide('wrong-proof',c,self.group,self.snapshot,report(self.group),a,confirmation=proof);self.assertEqual(r['state'],'held')
    def test_A_caps_unchanged(self):self.assertEqual(PLANS['A'],{'pair':4,'merge':2,'status':4})

if __name__=='__main__':unittest.main()
