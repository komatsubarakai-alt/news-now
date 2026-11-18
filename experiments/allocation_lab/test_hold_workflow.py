import copy,tempfile,unittest,socket,json
from pathlib import Path
from unittest.mock import patch
from .hold_workflow import HoldWorkflow,MockSession,MockIntent,assess
from .allocation import native,PLANS
from .session import digest

NOW='2026-11-15T12:00:00Z'

def side(eid,which='same'):
    vals={'occurred_at':'2026-10-01T10:00:00Z','location':'架空市A倉庫','participants':['架空団体A'],'history':'倉庫での架空の火災'}
    if which=='different':vals.update(occurred_at='2026-10-05T20:00:00Z',location='架空町B港湾',participants=['架空団体B'],history='港湾での架空の事故')
    text=' '.join(str(v) if not isinstance(v,list) else ' '.join(v) for v in vals.values())
    article={'url':'https://example.test/'+eid,'title':text,'description':text,'body_status':'unobtained','case_number':'fictional-number-'+eid}
    citation={'id':'cite-'+eid,'event_id':eid,'article_version':digest(article),'source_url':article['url'],'quote':text,'kind':'title','source_status':'operator_checked','source_type':'official_claim_unverified_by_program'}
    return {'event_id':eid,'event_state':'fictional-state','article':article,'facts':{k:{'value':v,'kind':'incident' if k=='occurred_at' else 'fact','citation_ids':[citation['id']]} for k,v in vals.items()},'citations':[citation]}
def bundle(which='same'):return {'existing':side('fictional-e1'),'incoming':side('fictional-incoming',which)}
def principal(scopes=('review','publish')):return MockSession('fictional-operator','mock-not-a-token','2026-12-01T00:00:00Z',frozenset(scopes))

class HoldWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'flow.sqlite';self.w=HoldWorkflow(self.path);self.addCleanup(lambda:self.w.close());self.b=bundle();self.case='fictional-case';self.rev=self.w.ingest('ingest',self.case,self.b,'2026-10-01T00:00:00Z')['revision']
        for target,name in [(socket.socket,'connect'),(native,'response')]:
            g=patch.object(target,name,side_effect=AssertionError('API/network forbidden'));g.start();self.addCleanup(g.stop)
    def act(self,action,data,op=None,**kw):
        return self.w.act(op or action,self.case,self.rev,action,data,principal(),MockIntent(self.rev,action,digest(data)),NOW,**kw)
    def review(self,relation='same',op='review'):return self.act('review',{'relation':relation,'reason':'架空の保存根拠を確認'},op)
    def publish(self,op='publish'):
        data={'confirmed':True,'preview_hash':digest(self.w.preview(self.case))};return self.act('publish_lab',data,op)
    def test_comparison_has_all_four_fields(self):
        a=assess(self.b);self.assertEqual(set(a['rows']),{'occurred_at','location','participants','history'});self.assertTrue(all(v['comparison']=='match' for v in a['rows'].values()))
    def test_missing_information_is_unknown_not_difference(self):
        self.b['incoming']['facts'].pop('location');a=assess(self.b);self.assertEqual(a['rows']['location']['comparison'],'unknown')
    def test_publication_time_not_incident_time(self):
        self.b['incoming']['facts']['occurred_at']['kind']='publication';self.assertEqual(assess(self.b)['rows']['occurred_at']['comparison'],'unknown')
    def test_case_numbers_do_not_override_same_incident_facts(self):
        self.assertNotEqual(self.b['existing']['article']['case_number'],self.b['incoming']['article']['case_number']);self.assertEqual(assess(self.b)['signal'],'same_evidence_signal');self.assertEqual(self.review('distinct')['state'],'held')
    def test_wrong_same_with_distinct_facts_is_held(self):
        self.b=bundle('different');self.rev=self.w.ingest('new',self.case,self.b,NOW)['revision'];self.assertEqual(self.review('same')['state'],'held')
    def test_valid_distinct_is_only_a_review_record(self):
        self.b=bundle('different');self.rev=self.w.ingest('new',self.case,self.b,NOW)['revision'];r=self.review('distinct');self.assertEqual(r['state'],'reviewed');self.assertEqual(r['new_event_ids'],0);self.assertFalse(r['publication_approval']);self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible')
    def test_literal_match_not_source_verification(self):
        self.b['incoming']['citations'][0]['source_status']='unverified';self.assertIn('location',assess(self.b)['missing']);self.assertEqual(assess(self.b)['rows']['location']['incoming_check'],'source_unconfirmed')
    def test_title_match_does_not_claim_body_confirmed(self):self.assertEqual(assess(self.b)['body_status']['incoming'],'unobtained')
    def test_shared_article_cannot_mix_event_quotes(self):
        self.b['incoming']['citations'][0]['event_id']='fictional-other';self.assertEqual(assess(self.b)['signal'],'uncertain')
    def test_stale_article_version_is_unverified(self):
        self.b['incoming']['article']['description']='改訂';self.assertEqual(assess(self.b)['rows']['history']['comparison'],'unknown')
    def test_JSON_cannot_claim_authenticated_session(self):
        d={'relation':'same','reason':'fake'}
        with self.assertRaises(PermissionError):self.w.act('forged',self.case,self.rev,'review',d,{'actor':'owner'},MockIntent(self.rev,'review',digest(d)),NOW)
    def test_expired_session_refused(self):
        d={'relation':'same','reason':'fake'};s=MockSession('operator','expired','2020-01-01T00:00:00Z',frozenset(['review']))
        with self.assertRaises(PermissionError):self.w.act('expired',self.case,self.rev,'review',d,s,MockIntent(self.rev,'review',digest(d)),NOW)
    def test_scope_separation(self):
        self.review();d={'confirmed':True,'preview_hash':digest(self.w.preview(self.case))}
        with self.assertRaises(PermissionError):self.w.act('publish',self.case,self.rev,'publish_lab',d,principal(('review',)),MockIntent(self.rev,'publish_lab',digest(d)),NOW)
    def test_explicit_confirmation_bound_to_action(self):
        with self.assertRaises(PermissionError):self.w.act('bad',self.case,self.rev,'review',{'relation':'same','reason':'x'},principal(),MockIntent(self.rev,'publish_lab','wrong'),NOW)
    def test_review_does_not_publish_or_merge(self):
        r=self.review();self.assertEqual(r['new_event_ids'],0);self.assertFalse(r['automatic_merge']);self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible')
    def test_publish_requires_current_preview(self):
        self.review()
        with self.assertRaises(ValueError):self.act('publish_lab',{'confirmed':True,'preview_hash':'old'},'old-preview')
    def test_hold_cannot_publish(self):
        self.review('hold')
        with self.assertRaises(ValueError):self.publish()
    def test_preview_and_lab_output_agree(self):
        self.review();preview=self.w.preview(self.case);self.publish();self.assertEqual(self.w.export_lab(self.case)['reader_content'],preview)
    def test_revoke_preserves_review_and_stops_output(self):
        self.review();self.publish();self.act('revoke',{'reason':'確認を取り消す'});self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible');self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='review'").fetchone()[0],1)
    def test_same_export_operation_does_not_restore_revoked_output(self):
        self.review();d={'confirmed':True,'preview_hash':digest(self.w.preview(self.case))};self.act('publish_lab',d,'publish');self.act('revoke',{'reason':'取消'});r=self.act('publish_lab',d,'publish');self.assertEqual(r['outcome'],'historical_operation_not_reapplied');self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible')
    def test_new_review_requires_new_publication_confirmation(self):
        self.review();self.publish();self.review('same','review-again');self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible')
    def test_article_revision_invalidates_and_keeps_history(self):
        self.review();self.publish();old=self.rev;new=copy.deepcopy(self.b);new['incoming']['article']['description']='訂正';self.rev=self.w.ingest('revision',self.case,new,NOW)['revision'];self.assertNotEqual(self.rev,old);self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible');self.assertEqual(self.w.bundle(self.case,old),self.b)
    def test_old_revision_operation_refused(self):
        old=self.rev;new=copy.deepcopy(self.b);new['incoming']['event_state']='changed';self.rev=self.w.ingest('changed',self.case,new,NOW)['revision'];d={'relation':'same','reason':'old'}
        with self.assertRaises(ValueError):self.w.act('old',self.case,old,'review',d,principal(),MockIntent(old,'review',digest(d)),NOW)
    def test_same_evidence_suppressed(self):
        self.assertFalse(self.w.local_check(self.case)['suppressed']);self.assertTrue(self.w.local_check(self.case)['suppressed'])
    def test_new_evidence_rechecks(self):
        self.w.local_check(self.case);new=copy.deepcopy(self.b);new['incoming']['article']['body']='架空の追加本文';self.rev=self.w.ingest('new-evidence',self.case,new,NOW)['revision'];self.assertFalse(self.w.local_check(self.case)['suppressed'])
    def test_hold_not_requeued_by_same_evidence(self):
        self.review('hold');self.w.ingest('same-evidence',self.case,self.b,NOW);metadata=copy.deepcopy(self.b);metadata['source_note']='メモだけの変更';self.w.ingest('memo-only',self.case,metadata,NOW);self.assertEqual(self.w.head(self.case)['state'],'held');self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='metadata_observed'").fetchone()[0],1);q=self.w.tasks(NOW);self.assertEqual(q['actionable_total'],0);self.assertEqual(len(q['waiting_for_evidence']),1)
    def test_explicit_manual_recheck_is_separate(self):
        self.review('hold');self.act('recheck',{'reason':'確認ミスの疑いを再点検'});self.assertEqual(self.w.tasks(NOW)['actionable_total'],1)
    def test_archive_keeps_evidence_and_reopens_only_on_change(self):
        self.act('archive',{'reason':'現在は確認不能'});self.assertEqual(self.w.bundle(self.case),self.b);self.assertEqual(self.w.tasks(NOW)['actionable_total'],0);new=copy.deepcopy(self.b);new['incoming']['article']['description']='架空の追加根拠';self.w.ingest('new',self.case,new,NOW);self.assertEqual(self.w.tasks(NOW)['actionable_total'],1)
    def test_budget_resume_loses_nothing(self):
        self.act('budget_pause',{});self.w.close();self.w=HoldWorkflow(self.path);self.assertTrue(self.w.head(self.case)['budget_wait']);self.assertEqual(self.w.bundle(self.case),self.b);self.act('budget_resume',{});self.assertFalse(self.w.head(self.case)['budget_wait'])
    def test_oldest_has_one_manual_card(self):
        self.w.ingest('older','fictional-older',bundle('different'),'2020-01-01T00:00:00Z');self.assertEqual(self.w.tasks(NOW,1)['actionable'][0]['case_id'],'fictional-older')
    def test_completed_work_not_counted_again(self):
        self.review();self.publish();self.assertEqual(self.w.tasks(NOW)['actionable_total'],0)
    def test_repeated_reviews_not_counted_as_more_cases(self):
        self.review();self.review('same','second-review');self.assertEqual(self.w.tasks(NOW)['actionable_total'],1)
    def test_duplicate_correction_stops_but_does_not_merge(self):
        self.w.ingest('second','fictional-second',self.b,NOW);self.review();self.publish();r=self.act('correct',{'type':'duplicate_incident','related_cases':['fictional-second'],'reason':'重複の疑い'});self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible');self.assertEqual(self.w.head('fictional-second')['state'],'needs_review');self.assertFalse(r['automatic_merge'])
    def test_wrong_merge_correction_preserves_original(self):
        self.review();self.publish();self.act('correct',{'type':'wrong_merge','related_cases':[],'reason':'別事件混入の疑い'});self.assertEqual(self.w.bundle(self.case),self.b);self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='correct'").fetchone()[0],1)
    def test_stop_and_send_back_keep_publication_history(self):
        self.review();self.publish();self.act('stop_lab',{'reason':'確認中'});self.act('send_back',{'reason':'根拠を点検'});self.assertEqual(self.w.head(self.case)['state'],'needs_review');self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='publish_lab'").fetchone()[0],1)
    def test_save_failure_is_not_success(self):
        with self.assertRaises(OSError):self.review_with_failure()
        self.assertEqual(self.w.head(self.case)['state'],'needs_review');self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='review'").fetchone()[0],0)
    def review_with_failure(self):return self.act('review',{'relation':'same','reason':'fake'},'failed-review',fail=True)
    def test_operation_replay_after_restart_is_idempotent(self):
        r=self.review();self.w.close();self.w=HoldWorkflow(self.path);self.assertEqual(self.review(),r);self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='review'").fetchone()[0],1)
    def test_conflicting_operation_rejected(self):
        self.review()
        with self.assertRaises(ValueError):self.review('distinct')
    def test_audit_tampering_fails_closed(self):
        self.review();self.w.db.execute("UPDATE audit SET payload='{}' WHERE action='review'");self.w.db.commit()
        with self.assertRaises(ValueError):self.publish()
    def test_A_and_input_unchanged(self):
        before=copy.deepcopy(self.b);self.review();self.w.local_check(self.case);self.assertEqual(self.b,before);self.assertEqual(PLANS['A'],{'pair':4,'merge':2,'status':4})
    def test_known_dependent_stopped_unrelated_maintained(self):
        dependent=copy.deepcopy(self.b);dependent['depends_on_cases']=[self.case]
        self.w.ingest('dep','fictional-dependent',dependent,NOW);self.w.ingest('unrelated','fictional-unrelated',self.b,NOW)
        for case in ['fictional-dependent','fictional-unrelated']:
            rev=self.w.head(case)['revision'];data={'relation':'same','reason':'架空の確認'};self.w.act('review-'+case,case,rev,'review',data,principal(),MockIntent(rev,'review',digest(data)),NOW)
            data={'confirmed':True,'preview_hash':digest(self.w.preview(case))};self.w.act('pub-'+case,case,rev,'publish_lab',data,principal(),MockIntent(rev,'publish_lab',digest(data)),NOW)
        self.review();self.act('correct',{'type':'wrong_merge','reason':'訂正'})
        self.assertEqual(self.w.export_lab('fictional-dependent')['state'],'not_visible');self.assertEqual(self.w.export_lab('fictional-unrelated')['state'],'lab_visible')
    def test_dependency_cycle_refused(self):
        dependent=copy.deepcopy(self.b);dependent['depends_on_cases']=[self.case];self.w.ingest('dep','fictional-dependent',dependent,NOW)
        revised=copy.deepcopy(self.b);revised['depends_on_cases']=['fictional-dependent']
        with self.assertRaises(ValueError):self.w.ingest('cycle',self.case,revised,NOW)
    def test_invalid_time_type_stays_unknown(self):
        self.b['incoming']['facts']['occurred_at']['value']=['not-a-date'];self.assertEqual(assess(self.b)['rows']['occurred_at']['comparison'],'unknown')
    def test_duplicate_quote_ids_stay_unknown(self):
        self.b['incoming']['citations']*=2;self.assertEqual(assess(self.b)['rows']['history']['comparison'],'unknown')
    def test_publication_save_failure_rolls_back_then_recovers(self):
        self.review();data={'confirmed':True,'preview_hash':digest(self.w.preview(self.case))}
        with self.assertRaises(OSError):self.act('publish_lab',data,'publish',fail=True)
        self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible');self.act('publish_lab',data,'publish');self.assertEqual(self.w.export_lab(self.case)['state'],'lab_visible')
    def test_correction_reapproval_preserves_old_publication(self):
        self.review();old_preview=self.w.preview(self.case);self.publish();old=self.rev;self.act('correct',{'type':'source_correction','reason':'訂正'})
        revised=bundle('different');self.rev=self.w.ingest('corrected-version',self.case,revised,NOW)['revision'];self.review('distinct','new-review');self.publish('new-publish')
        self.assertEqual(self.w.export_lab(self.case)['state'],'lab_visible');self.assertEqual(self.w.bundle(self.case,old),self.b);self.assertEqual(self.w.db.execute("SELECT count(*) FROM audit WHERE action='publish_lab'").fetchone()[0],2);stored=json.loads(self.w.db.execute("SELECT payload FROM audit WHERE action='publish_lab' ORDER BY seq LIMIT 1").fetchone()[0]);self.assertEqual(stored['lab_preview'],old_preview)
    def test_rule_change_invalidates_old_review_and_preview(self):
        self.review();self.publish();self.w.local_check(self.case)
        with patch('experiments.allocation_lab.hold_workflow.rule_revision',return_value='changed-rules'):
            self.assertFalse(self.w.local_check(self.case)['suppressed']);self.assertFalse(self.w.review_is_current(self.case));self.assertEqual(self.w.export_lab(self.case)['state'],'not_visible');self.assertEqual(self.w.tasks(NOW)['actionable'][0]['type'],'review')
    def test_old_unready_hold_gets_storage_reminder_not_AI_retry(self):
        self.review('hold');queue=self.w.tasks(NOW);self.assertEqual(queue['actionable_total'],0);self.assertTrue(queue['waiting_for_evidence'][0]['storage_review_due'])
    def test_workflow_record_does_not_auto_authorize_old_new_event_guard(self):
        from .new_event_guard import NewEventGuard,Snapshot
        from .test_new_event_guard import event,group,report
        import hashlib
        self.b=bundle('different');self.rev=self.w.ingest('different',self.case,self.b,NOW)['revision'];record=self.review('distinct')
        path=Path(self.temp.name)/'fake-events.json';path.write_text(json.dumps([event()]));snapshot=Snapshot(path,hashlib.sha256(path.read_bytes()).hexdigest());guard=NewEventGuard(Path(self.temp.name)/'gate.sqlite');g=group();c=guard.queue('q',g,snapshot)['context_id']
        self.assertEqual(guard.decide('d',c,g,snapshot,report(g),record)['state'],'held');guard.close()
    def test_counterexample_coherently_wrong_sources_can_mislead_lab_review(self):
        # Ground truth is same incident; every stored source fact is intentionally wrong.
        # Matching strings cannot independently establish truth. This remains a counterexample.
        self.b=bundle('different');self.rev=self.w.ingest('coherently-wrong',self.case,self.b,NOW)['revision'];self.assertEqual(self.review('distinct')['state'],'reviewed');self.publish();self.assertEqual(self.w.export_lab(self.case)['state'],'lab_visible')

if __name__=='__main__':unittest.main()
