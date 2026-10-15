import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from engine import atomic,connect,import_snapshot
from article_store import (initialize,import_saved,import_reviews,save_version,version_id,
    compare_articles,quote_template,validate_quotes,import_quotes,record_fetch,digest)

HERE=Path(__file__).parent;ROOT=HERE.parents[1]
DATASET_RAW=(HERE/'evaluation.json').read_bytes();REVIEW_RAW=(HERE/'operator_review_records.json').read_bytes()
ROWS={r['sample_id']:r for r in json.loads(DATASET_RAW)['rows']}

def core_state(db):
    tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sep_')]
    return digest({table:db.execute('SELECT * FROM '+table).fetchall() for table in tables})

class ArticleStoreTests(unittest.TestCase):
    def setUp(self):
        self.db=connect();import_snapshot(self.db,ROOT);initialize(self.db)
    def tearDown(self):self.db.close()
    def test_real_022_shared_article_keeps_two_distinct_incidents(self):
        import_saved(self.db);row=ROWS['real-022'];before=core_state(self.db)
        result=compare_articles(self.db,row['reference_article'],row['incoming_article'])
        self.assertEqual(result['relation'],'exact_saved_version');self.assertEqual(result['left_version'],result['right_version'])
        mentions=self.db.execute("SELECT event_id FROM sep_event_mentions WHERE version_id=? AND role='current'",(result['left_version'],)).fetchall()
        self.assertEqual({x[0] for x in mentions},set(row['stored_reference_event_ids']))
        self.assertEqual(len(mentions),2);self.assertFalse(result['automatic_event_merge'])
        self.assertEqual(core_state(self.db),before)
    def test_real_022_existing_guard_still_blocks_unscoped_new_incident(self):
        from merge_safety import validate_update,UpdateHeld
        from event_tracker import TRACKING_RESPONSE
        row=ROWS['real-022'];answer=copy.deepcopy(TRACKING_RESPONSE)
        answer.update(relation='new_event',confidence='high',multi_event=False)
        with self.assertRaisesRegex(UpdateHeld,'multiple_independent_events'):
            validate_update({'event_name':row['incoming_quote']},[row['incoming_article']],answer,initial=True)
    def test_real_030_keeps_different_urls_and_versions_unconfirmed(self):
        row=ROWS['real-030'];result=compare_articles(self.db,row['reference_article'],row['incoming_article'])
        self.assertEqual(result['relation'],'same_headline_time_candidate');self.assertNotEqual(result['left_version'],result['right_version'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_urls').fetchone()[0],2)
        self.assertEqual(result['status'],'unconfirmed');self.assertFalse(result['syndication_confirmed'])
    def test_real_033_not_exact_not_proven_syndication(self):
        row=ROWS['real-033'];result=compare_articles(self.db,row['reference_article'],row['incoming_article'])
        self.assertEqual(result['relation'],'similar_headline_candidate');self.assertEqual(result['status'],'unconfirmed')
        self.assertFalse(result['automatic_event_merge']);self.assertEqual(result['body_equality'],'unconfirmed')
        rows=self.db.execute('SELECT published,payload FROM sep_versions').fetchall()
        self.assertEqual(len({r[0] for r in rows}),2)
    def test_new_publication_time_same_url_keeps_both_versions(self):
        a=copy.deepcopy(ROWS['real-030']['incoming_article']);b=copy.deepcopy(a);b['published']='changed publication metadata'
        with atomic(self.db):
            va=save_version(self.db,a,'synthetic','/0');vb=save_version(self.db,b,'synthetic','/1')
        self.assertNotEqual(va,vb);self.assertEqual(self.db.execute('SELECT count(*) FROM sep_versions').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_contents').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_urls').fetchone()[0],1)
    def test_changed_content_same_url_preserves_content_history(self):
        a=copy.deepcopy(ROWS['real-030']['incoming_article']);b=copy.deepcopy(a);b['description']='synthetic correction'
        with atomic(self.db):
            save_version(self.db,a,'synthetic','/0');save_version(self.db,b,'synthetic','/1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_contents').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_versions').fetchone()[0],2)
    def test_collection_metadata_does_not_create_an_article_revision(self):
        a=copy.deepcopy(ROWS['real-030']['incoming_article']);b=copy.deepcopy(a);b['collection_reason']='synthetic collection observation'
        with atomic(self.db):
            va=save_version(self.db,a,'synthetic','/0');vb=save_version(self.db,b,'synthetic','/0')
        self.assertEqual(va,vb);self.assertEqual(self.db.execute('SELECT count(*) FROM sep_versions').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_occurrences').fetchone()[0],2)
        self.assertIn('synthetic collection observation',str(self.db.execute('SELECT payload FROM sep_occurrences').fetchall()))
    def test_import_repeat_retains_occurrences_and_legacy_projections(self):
        before=core_state(self.db);import_saved(self.db)
        counts={t:self.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['sep_versions','sep_occurrences','sep_event_mentions']}
        import_saved(self.db)
        self.assertEqual(counts,{t:self.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in counts})
        self.assertGreater(counts['sep_occurrences'],counts['sep_versions']);self.assertEqual(core_state(self.db),before)
        self.assertFalse(self.db.execute('PRAGMA foreign_key_check').fetchall())
    def test_only_separate_tables_can_be_written(self):
        before=core_state(self.db)
        core={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'") if not r[0].startswith('sep_')}
        def authorize(action,table,*unused):
            if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE) and table in core:return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.db.set_authorizer(authorize)
        import_saved(self.db);import_reviews(self.db,DATASET_RAW,REVIEW_RAW)
        doc=quote_template(DATASET_RAW,REVIEW_RAW);import_quotes(self.db,doc,DATASET_RAW,REVIEW_RAW)
        for sid in ['real-022','real-030','real-033']:
            compare_articles(self.db,ROWS[sid]['reference_article'],ROWS[sid]['incoming_article'])
        self.assertEqual(core_state(self.db),before)
    def test_import_failure_rolls_back_without_losing_legacy_data(self):
        before=core_state(self.db)
        with self.assertRaises(RuntimeError):import_saved(self.db,fail_after=3)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_versions').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_occurrences').fetchone()[0],0)
        self.assertEqual(core_state(self.db),before)
    def test_operator_deletion_and_blank_display_are_not_observed_fetches(self):
        import_saved(self.db);import_reviews(self.db,DATASET_RAW,REVIEW_RAW)
        counts=dict(self.db.execute("SELECT result,count(*) FROM sep_fetch_observations WHERE basis='operator_report' GROUP BY result"))
        self.assertEqual(counts,{'reported_deleted':5,'reported_blank_display':1})
        self.assertEqual(self.db.execute("SELECT count(*) FROM sep_fetch_observations WHERE basis='operator_report' AND (observed_at IS NOT NULL OR http_status IS NOT NULL)").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM sep_fetch_observations WHERE result IN ('request_failed','deleted','empty_body','available')").fetchone()[0],0)
    def test_failed_deleted_empty_and_available_are_separate_synthetic_records(self):
        with atomic(self.db):vid=save_version(self.db,ROWS['real-030']['incoming_article'],'synthetic','/0')
        for status,args in [('request_failed',{'error':'synthetic timeout'}),('deleted',{'http_status':410}),
                            ('empty_body',{'http_status':200,'body_text':''}),('available',{'http_status':200,'body_text':'synthetic body'})]:
            record_fetch(self.db,vid,status,'synthetic',observed_at='2026-01-01T00:00:00Z',**args)
        result=dict(self.db.execute("SELECT result,count(*) FROM sep_fetch_observations WHERE basis='synthetic' GROUP BY result"))
        self.assertEqual(result,{k:1 for k in ['request_failed','deleted','empty_body','available']})
        self.assertEqual(self.db.execute("SELECT count(*) FROM sep_contents WHERE representation='provided_body'").fetchone()[0],2)
    def test_invalid_fetch_claims_rejected_without_writes(self):
        with atomic(self.db):vid=save_version(self.db,ROWS['real-030']['incoming_article'],'synthetic','/0')
        before=self.db.execute('SELECT count(*) FROM sep_fetch_observations').fetchone()[0]
        for status,basis,args in [('deleted','operator_report',{}),('deleted','synthetic',{'http_status':200}),
            ('empty_body','synthetic',{'http_status':200,'body_text':'not empty'}),('request_failed','synthetic',{}),
            ('reported_deleted','operator_report',{'http_status':404,'report_hash':'x'}),('available','synthetic',{'http_status':200,'body_text':''})]:
            with self.assertRaises(ValueError):record_fetch(self.db,vid,status,basis,observed_at='2026-01-01T00:00:00Z',**args)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_fetch_observations').fetchone()[0],before)
    def test_quote_template_has_no_invented_quotes_or_check_times(self):
        doc=quote_template(DATASET_RAW,REVIEW_RAW)
        self.assertEqual({p['sample_id'] for p in doc['pairs']},{'real-024','real-025','real-027','real-028','real-029','real-031','real-032'})
        self.assertTrue(all(s['checked_at'] is None and s['source_body_sha256'] is None and s['quotes']==[] for p in doc['pairs'] for s in p['sources']))
        info=import_quotes(self.db,doc,DATASET_RAW,REVIEW_RAW)
        self.assertEqual(info['sources_with_quotes'],0);self.assertEqual(info['sources_with_check_time'],0)
        self.assertFalse(info['gold_promoted']);self.assertEqual(self.db.execute('SELECT count(*) FROM sep_quote_sources').fetchone()[0],14)
    def test_quote_submission_history_is_append_only_and_unverified(self):
        doc=quote_template(DATASET_RAW,REVIEW_RAW);import_quotes(self.db,doc,DATASET_RAW,REVIEW_RAW)
        changed=copy.deepcopy(doc);changed['pairs'][0]['sources'][0].update(checked_at='2026-01-01T00:00:00Z',quotes=['SYNTHETIC QUOTE, NOT A REAL ARTICLE'])
        before=core_state(self.db);info=import_quotes(self.db,changed,DATASET_RAW,REVIEW_RAW)
        self.assertEqual(info['sources_with_quotes'],1);self.assertFalse(info['source_verified'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_quote_documents').fetchone()[0],2)
        self.assertEqual(core_state(self.db),before)
    def test_tampered_quote_sources_and_auto_verification_flags_rejected(self):
        original=quote_template(DATASET_RAW,REVIEW_RAW)
        edits=[lambda d:d.update(record_scope='gold'),lambda d:d['pairs'].pop(),
            lambda d:d['pairs'][0].update(event_id='other'),lambda d:d['pairs'][0]['sources'][0].update(url='https://other.test/'),
            lambda d:d['pairs'][0]['sources'][0].update(version_id='stale'),lambda d:d['pairs'][0]['sources'][0].update(verified=True),
            lambda d:d['pairs'][0]['sources'][0].update(checked_at='2026-01-01'),lambda d:d['pairs'][0]['sources'][0].update(quotes=['x'*501]),
            lambda d:d['pairs'][0]['sources'][0].update(source_body_sha256='stale'),
            lambda d:d['pairs'][0].update(operator_decision_recorded_at='changed')]
        for edit in edits:
            doc=copy.deepcopy(original);edit(doc)
            with self.assertRaises(ValueError):import_quotes(self.db,doc,DATASET_RAW,REVIEW_RAW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sep_quote_documents').fetchone()[0],0)
    def test_foreign_keys_prevent_unknown_incident_association(self):
        with atomic(self.db):vid=save_version(self.db,ROWS['real-030']['incoming_article'],'synthetic','/0')
        with self.assertRaises(sqlite3.IntegrityError):
            with atomic(self.db):self.db.execute('INSERT INTO sep_event_mentions VALUES(?,?,?,?,?)',('unknown',vid,'current',None,'imported_not_revalidated'))
    def test_temporary_db_reopen_keeps_multi_event_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.db.close();self.db=connect(str(Path(tmp)/'shadow.db'));import_snapshot(self.db,ROOT);import_saved(self.db)
            vid=version_id(ROWS['real-022']['incoming_article']);before=self.db.execute('SELECT * FROM sep_event_mentions WHERE version_id=?',(vid,)).fetchall()
            self.db.commit();self.db.close();self.db=connect(str(Path(tmp)/'shadow.db'))
            self.assertEqual(self.db.execute('SELECT * FROM sep_event_mentions WHERE version_id=?',(vid,)).fetchall(),before)
            self.assertEqual(len(before),2)
