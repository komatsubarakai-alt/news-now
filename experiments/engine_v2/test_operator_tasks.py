import copy
import json
import tempfile
import unittest
from pathlib import Path
import operator_app as app
import operator_tasks as tasks
from operator_task_demo import populate
from engine import events,dumps
import revision_sync as sync
import publication as pub

class HumanTaskTests(unittest.TestCase):
    def setUp(self):self.db=app.connect();app.initialize(self.db);populate(self.db)
    def tearDown(self):self.db.close()
    def state(self):return app.snapshot(self.db)
    def task(self,number,eid=None):
        return next(t for t in self.state()['work']['tasks'] if t.get('article',{}).get('url')=='https://example.test/demo-'+number and (eid is None or t.get('required_event_id')==eid))
    def command(self,number='01',relation='same_event',progress='none',op='human-task-0001',**kw):
        t=self.task(number,kw.pop('required',None));c=dict(action='task_review',op=op,revision=self.state()['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=t['suggested_event_id'],relation=relation,progress=progress,reason='架空の引用と事件を確認',proposed_stage=None);c.update(kw);return c
    def bundle(self,**kw):
        i=next(i for i in self.state()['items'] if i['event']['event_id']=='demo-e1')
        c=dict(action='approve_bundle',op='bundle-operation-001',revision=self.state()['revision'],event_id='demo-e1',targets=[{k:t[k] for k in ('kind','key','content_hash')} for t in i['targets'] if t['status']!='approved'],confirmed=True,reason='架空の全項目を確認');c.update(kw);return c
    def test_same_event_no_progress_never_changes_facts(self):
        before=events(self.db);out=app.operate(self.db,self.command());self.assertFalse(out['new_progress_counted']);self.assertEqual(events(self.db),before)
        self.assertEqual(len(self.state()['work']['completed']),1)
    def test_arrest_to_indictment_is_record_only(self):
        before=events(self.db);out=app.operate(self.db,self.command('02',progress='new',proposed_stage='起訴'))
        self.assertEqual(out['progress_assessment'],'operator_reported_new');self.assertEqual(events(self.db),before);self.assertFalse(out['adopted'])
    def test_different_case_is_not_auto_registered_or_merged(self):
        before=events(self.db);app.operate(self.db,self.command('03',relation='different_event',progress=None));self.assertEqual(events(self.db),before)
    def test_uncertain_identity_retained_as_hold(self):
        out=app.operate(self.db,self.command('04',relation='hold',progress=None,event_id=None));self.assertEqual(out['relation'],'hold');self.assertEqual(self.task('04')['category'],'hold')
    def test_missing_quote_blocks_same_event_and_keeps_task(self):
        with self.assertRaisesRegex(ValueError,'引用'):app.operate(self.db,self.command('05'))
        self.assertEqual(self.task('05')['category'],'identity')
    def test_missing_progress_fact_is_hold_not_new_progress(self):
        out=app.operate(self.db,self.command('01',progress='new',proposed_stage='起訴'));self.assertEqual(out['progress_assessment'],'needs_evidence');self.assertEqual(self.task('01')['category'],'hold')
    def test_repeated_indictment_quote_is_duplicate_candidate(self):
        before=events(self.db);out=app.operate(self.db,self.command('06',progress='new',proposed_stage='起訴'));self.assertEqual(out['progress_assessment'],'already_recorded_literal_candidate');self.assertFalse(out['new_progress_counted']);self.assertEqual(events(self.db),before)
    def test_correction_alert_is_separate_and_stops_publication(self):
        app.operate(self.db,self.bundle());before=events(self.db)
        app.operate(self.db,self.command('07',progress='possible_correction',op='correction-reference'))
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_correction_alerts').fetchone()[0],1)
        self.assertEqual(events(self.db),before);self.assertEqual(self.state()['approved_preview'],[]);self.assertEqual(self.task('07')['category'],'hold')
        with self.assertRaises(ValueError):app.operate(self.db,self.bundle(op='cannot-reapprove'))
    def test_body_missing_is_explicit_not_literal_body_match(self):
        t=self.task('08');self.assertEqual(t['source']['availability'],'unknown');self.assertIsNone(t['source']['body_id']);self.assertEqual({e['field'] for e in t['evidence']},{'title'})
    def test_article_revision_reopens_only_its_task_and_keeps_history(self):
        app.operate(self.db,self.command());before=self.state();other=self.task('02')['context_hash'];url='https://example.test/demo-01'
        a=next(t['article'] for t in before['work']['completed'] if t['article']['url']==url)
        sync.change_source(self.db,'edit-completed-article',a,sync.head(self.db,url)['state_id'],app.now(),body_text='改訂された架空本文')
        t=self.task('01');self.assertEqual(t['category'],'hold');self.assertEqual(len(t['history']),1);self.assertEqual(self.task('02')['context_hash'],other)
    def test_heading_revision_displays_current_head_not_stale_queue(self):
        t=self.task('01');a=copy.deepcopy(t['article']);a['title']+=' 見出し訂正'
        sync.change_source(self.db,'heading-edit',a,sync.head(self.db,a['url'])['state_id'],app.now())
        updated=self.task('01');self.assertEqual(updated['article']['title'],a['title']);self.assertEqual(updated['task_id'],t['task_id']);self.assertNotEqual(updated['context_hash'],t['context_hash'])
    def test_shared_article_keeps_both_incident_tasks(self):
        a=self.task('10','demo-e2');b=self.task('10','demo-e3');self.assertNotEqual(a['task_id'],b['task_id']);before=events(self.db)
        app.operate(self.db,self.command('10',required='demo-e2'));self.assertEqual(self.task('10','demo-e3')['task_id'],b['task_id']);self.assertEqual(events(self.db),before)
    def test_shared_article_wrong_target_rejected(self):
        with self.assertRaises(ValueError):app.operate(self.db,self.command('10',required='demo-e2',event_id='demo-e1'))
    def test_first_stage_only_moves_to_progress_badge(self):
        app.operate(self.db,self.command(progress=None));self.assertEqual(self.task('01')['category'],'progress');self.assertEqual(self.state()['work']['counts']['progress'],1)
    def test_second_stage_without_same_event_rejected(self):
        with self.assertRaises(ValueError):app.operate(self.db,self.command(relation='different_event',progress='new'))
    def test_dashboard_deduplicates_repeated_stages(self):
        before=self.state()['work']['counts'];q=self.db.execute("SELECT payload FROM holds WHERE stage='tracking_pending.json' AND group_key='demo-case-01'").fetchone()[0]
        with self.db:self.db.execute('INSERT INTO holds VALUES(?,?,?)',('status_pending.json','repeat-stage',q))
        self.assertEqual(self.state()['work']['counts'],before)
    def test_terminal_and_ai_waiting_not_human_counts(self):
        before=self.state()['work']['counts'];g={'event_name':'過去記録','articles':[dict(title='別記事',url='https://example.test/old-history',published='2020-01-01')]}
        with self.db:
            self.db.execute('INSERT INTO holds VALUES(?,?,?)',('tracking_triage','history',dumps({'status':'out_of_scope','group':g})))
            self.db.execute('INSERT INTO holds VALUES(?,?,?)',('status_pending.json','history-stage',dumps(g)))
            g2=copy.deepcopy(g);g2['articles'][0]['url']='https://example.test/ai-queued';self.db.execute('INSERT INTO holds VALUES(?,?,?)',('status_pending.json','new-auto',dumps(g2)))
        self.assertEqual(self.state()['work']['counts'],before);self.assertEqual(self.state()['work']['ai_waiting'],1)
    def test_same_command_receipt_prevents_double_review(self):
        c=self.command();r=app.operate(self.db,c);self.assertEqual(app.operate(self.db,c),r);self.assertEqual(self.db.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0],1)
    def test_review_rollback_and_retry(self):
        c=self.command()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'before_receipt')
        self.assertEqual(self.db.execute('SELECT count(*) FROM ui_task_reviews').fetchone()[0],0);app.operate(self.db,c)
    def test_stale_task_context_stops(self):
        with self.assertRaises(ValueError):app.operate(self.db,self.command(context_hash='stale'))
    def test_public_badge_counts_incidents_not_eight_fields(self):
        s=self.state();self.assertEqual(s['work']['counts']['publication'],1);self.assertGreater(len(s['items'][0]['targets']),1)
    def test_legacy_unapproved_event_does_not_inflate_public_badge(self):
        e=copy.deepcopy(events(self.db)[0]);e['event_id']='legacy-extra'
        with self.db:self.db.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        self.assertEqual(self.state()['work']['counts']['publication'],1)
    def test_bundle_preview_rolls_back_and_matches_approved_output(self):
        before=self.state();c=self.bundle();out=app.preview_bundle(self.db,c);self.assertEqual(before,self.state());self.assertTrue(out['events'])
        result=app.operate(self.db,c);self.assertEqual(result['events'],out['events']);self.assertEqual(self.state()['approved_preview'],out['events']);self.assertEqual(len(result['results']),len(c['targets']))
    def test_one_invalid_target_rolls_back_all_bundle_approvals(self):
        c=self.bundle();c['targets'][-1]['content_hash']='changed'
        with self.assertRaises(ValueError):app.operate(self.db,c)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
    def test_bundle_mid_save_failure_rolls_back_and_retry_once(self):
        c=self.bundle()
        with self.assertRaises(RuntimeError):app.operate(self.db,c,'after_first')
        self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],0)
        app.operate(self.db,c);count=self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0];app.operate(self.db,c);self.assertEqual(self.db.execute('SELECT count(*) FROM pub_approvals').fetchone()[0],count)
    def test_bundle_confirmation_required(self):
        with self.assertRaises(ValueError):app.operate(self.db,self.bundle(confirmed=False))
    def test_publication_badge_disappears_only_after_all_selected_approvals(self):
        app.operate(self.db,self.bundle());self.assertEqual(self.state()['work']['counts']['publication'],0)
    def test_publication_recheck_appears_after_source_change(self):
        app.operate(self.db,self.bundle());a=events(self.db)[0]['articles'][0]
        sync.change_source(self.db,'public-source-correction',dict(a,title=a['title']+' 訂正'),sync.head(self.db,a['url'])['state_id'],app.now())
        self.assertTrue(any((t.get('event_id')=='demo-e1' or any(p['event_id']=='demo-e1' for p in t.get('publication_rechecks',[]))) and t['category']=='hold' for t in self.state()['work']['tasks']));self.assertEqual(self.state()['approved_preview'],[])
    def test_occurred_date_not_invented_from_publication_date(self):
        c=self.command();r=app.operate(self.db,c);row=self.db.execute('SELECT event_occurred_at FROM ui_task_reviews').fetchone();self.assertIsNone(row[0])
        self.assertEqual(events(self.db)[0]['progress_count'],1)
    def test_deletion_and_fetch_failure_cannot_be_confirmed_same(self):
        t=self.task('01');sync.change_source(self.db,'deleted-demo',t['article'],t['source']['state_id'],app.now(),outcome='deleted')
        with self.assertRaisesRegex(ValueError,'削除'):app.operate(self.db,self.command())
    def test_missing_stage_is_progress_hold(self):
        r=app.operate(self.db,self.command('02',progress='new'));self.assertEqual(r['progress_assessment'],'needs_evidence');self.assertEqual(self.task('02')['category'],'hold')
    def test_changed_quote_recheck_does_not_promote_correctness(self):
        r=app.operate(self.db,self.command('09'));self.assertEqual(r['status'],'recorded_reference_only');self.assertFalse(r['published']);self.assertFalse(r['adopted'])

    def test_identical_source_observation_does_not_reopen_completed_work(self):
        app.operate(self.db,self.command());url='https://example.test/demo-01';a=next(t['article'] for t in self.state()['work']['completed'] if t['article']['url']==url)
        old=sync.head(self.db,url);body=json.loads(self.db.execute('SELECT payload FROM sep_contents WHERE content_id=?',(old['body_id'],)).fetchone()[0])['body_text']
        sync.change_source(self.db,'same-observation',a,old['state_id'],app.now(),body_text=body)
        self.assertTrue(any(t['article']['url']==url for t in self.state()['work']['completed']))
    def test_unrelated_incident_change_does_not_reopen_review(self):
        app.operate(self.db,self.command());e=next(e for e in events(self.db) if e['event_id']=='demo-e3');e['summary']='無関係な別事件の概要変更'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'demo-e3'))
        self.assertTrue(any(t['article']['url'].endswith('demo-01') for t in self.state()['work']['completed']))
    def test_selected_incident_change_does_reopen_review(self):
        app.operate(self.db,self.command());e=next(e for e in events(self.db) if e['event_id']=='demo-e2');e['current_stage']='判決'
        with self.db:self.db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(e),'demo-e2'))
        self.assertEqual(self.task('01')['category'],'hold')
    def test_switching_candidate_records_exact_selected_context(self):
        app.operate(self.db,self.command(event_id='demo-e1',relation='hold',progress=None))
        self.assertEqual(self.task('01')['record']['event_id'],'demo-e1');self.assertEqual(self.task('01')['category'],'hold')
    def test_conflicting_unknown_editions_require_hold(self):
        url='https://example.test/unknown-editions'
        with self.db:
            for i in (1,2):
                g={'articles':[{'url':url,'title':'記事版'+str(i),'published':'2026-10-08T11:00:00Z'}]}
                self.db.execute('INSERT INTO holds VALUES(?,?,?)',('tracking_triage','variant'+str(i),dumps({'status':'manual_review','group':g})))
        t=next(t for t in self.state()['work']['tasks'] if t.get('article',{}).get('url')==url)
        self.assertTrue(t['edition_conflict']);self.assertEqual(t['category'],'hold');self.assertEqual(len(t['article_variants']),2)
    def test_failed_fetch_keeps_task_but_stops_confirmation(self):
        t=self.task('01');sync.change_source(self.db,'failed-fetch',t['article'],t['source']['state_id'],app.now(),outcome='request_failed',error='架空タイムアウト')
        with self.assertRaises(ValueError):app.operate(self.db,self.command())
        self.assertTrue(self.task('01')['fetch_wait'])

    def test_audit_keeps_unassigned_article_after_queue_removal(self):
        c=self.command('04',relation='hold',progress=None,event_id=None);app.operate(self.db,c)
        with self.db:
            self.db.execute("DELETE FROM holds WHERE group_key='demo-case-04'")
        history=self.state()['work']['review_history'];record=next(r for r in history if r['op']==c['op'])
        self.assertIsNone(record['event_id']);self.assertTrue(json.loads(record['article_payload'])['url'].endswith('demo-04'))
    def test_new_review_survives_sqlite_restart_without_budget_change(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'operator.sqlite';db=app.open_owned(path);populate(db);before=list(db.execute('SELECT * FROM budget ORDER BY name'))
            state=app.snapshot(db);t=next(t for t in state['work']['tasks'] if t.get('article',{}).get('url','').endswith('demo-01'))
            c=dict(action='task_review',op='restart-human-review',revision=state['revision'],task_id=t['task_id'],context_hash=t['context_hash'],event_id=t['suggested_event_id'],relation='same_event',progress='none',reason='架空の確認')
            r=app.operate(db,c);saved=app.snapshot(db);db.close();db=app.open_owned(path)
            self.assertEqual(app.snapshot(db),saved);self.assertEqual(app.operate(db,c),r);self.assertEqual(list(db.execute('SELECT * FROM budget ORDER BY name')),before);db.close()
    def test_restoring_old_body_never_restores_old_review(self):
        app.operate(self.db,self.command());a=next(t['article'] for t in self.state()['work']['completed'] if t['article']['url'].endswith('demo-01'));url=a['url'];old=sync.head(self.db,url)
        body=json.loads(self.db.execute('SELECT payload FROM sep_contents WHERE content_id=?',(old['body_id'],)).fetchone()[0])['body_text']
        sync.change_source(self.db,'change-body',a,old['state_id'],app.now(),body_text='別の架空本文')
        sync.change_source(self.db,'restore-body',a,sync.head(self.db,url)['state_id'],app.now(),body_text=body)
        self.assertEqual(self.task('01')['category'],'hold')

    def test_new_queue_edition_is_not_overwritten_by_seeded_old_head(self):
        app.operate(self.db,self.command());g=json.loads(self.db.execute("SELECT payload FROM holds WHERE stage='tracking_pending.json' AND group_key='demo-case-01'").fetchone()[0]);g['articles'][0]['title']+=' 新しい未照合の見出し'
        with self.db:self.db.execute('INSERT INTO holds VALUES(?,?,?)',('status_pending.json','new-edition',dumps(g)))
        t=self.task('01');self.assertEqual(t['category'],'hold');self.assertTrue(t['edition_conflict']);self.assertEqual(len(t['article_variants']),2)
        self.assertTrue(any('新しい未照合' in a['title'] for a in t['article_variants']))
        with self.assertRaises(ValueError):app.operate(self.db,self.command())

    def test_correction_words_cannot_be_recorded_as_ordinary_new_progress(self):
        before=events(self.db);r=app.operate(self.db,self.command('07',progress='new',proposed_stage='逮捕'))
        self.assertEqual(r['progress_assessment'],'possible_correction');self.assertEqual(r['correction'],'possible');self.assertEqual(self.task('07')['category'],'hold');self.assertEqual(events(self.db),before)

    def test_completed_review_can_be_explicitly_reopened_with_history(self):
        app.operate(self.db,self.command());s=self.state();t=s['work']['completed'][0]
        c=dict(action='reopen_task',op='reopen-reference',revision=s['revision'],event_id=t['record']['event_id'],task_id=t['task_id'],reason='判断を再確認するため')
        app.operate(self.db,c);self.assertEqual(self.task('01')['category'],'hold');self.assertEqual(len(self.task('01')['history']),2)
        app.operate(self.db,self.command(op='review-again'));self.assertTrue(any(t['article']['url'].endswith('demo-01') for t in self.state()['work']['completed']))
    def test_manual_hold_survives_automatic_queue_status_changes(self):
        app.operate(self.db,self.command(relation='hold',progress=None))
        g=json.loads(self.db.execute("SELECT payload FROM holds WHERE stage='tracking_triage' AND group_key='demo-case-01'").fetchone()[0]);g['status']='queued'
        with self.db:self.db.execute("UPDATE holds SET payload=? WHERE stage='tracking_triage' AND group_key='demo-case-01'",(dumps(g),))
        self.assertEqual(self.task('01')['category'],'hold')

if __name__=='__main__':unittest.main()
