"""Fictional offline response capture fixtures."""
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(pathlib.Path(__file__).parent)]
from engine import connect
from failed_response import digest
import response_provenance as p
import status_cache_replay as cache
import revision_sync as sync

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def group():
    return {'event_name': '架空の施設制度変更', 'articles': [{'url': 'https://example.test/news',
        'title': '架空の施設制度変更を発表', 'description': '架空の説明', 'published': '2026-01-01T00:00:00Z'}]}


def payload():
    return {'id': 'fictional-response', 'status': 'completed', 'output': [{'content': [
        {'type': 'output_text', 'text': json.dumps({'category': '政策・制度', 'tracking_value': 'medium'})}]}]}


class ProvenanceTests(unittest.TestCase):
    def setUp(self): self.db = connect(); self.g = group()
    def tearDown(self): self.db.close()
    def prepare(self): return p.prepare_status(self.db, self.g, {}, {}, NOW)
    def save(self): return p.save(self.db, 'response-op', self.prepare(), payload(), NOW)

    def test_complete_metadata_record_and_source_binding(self):
        rid = self.save(); row = self.db.execute('SELECT capture_id,response_external_id,saved_at,completion,format_result,reuse_reason FROM response_records WHERE record_id=?', (rid,)).fetchone()
        source = p.input_record(self.db, row[0]); a = source['sources'][0]
        self.assertEqual(a['url'], self.g['articles'][0]['url']); self.assertTrue(a['version_id']); self.assertEqual(a['content_hash'], digest(self.g['articles'][0]))
        self.assertEqual(source['event_ids'], []); self.assertTrue(source['validator_version']); self.assertTrue(source['request_hash']); self.assertTrue(source['context_hash'])
        self.assertEqual(row[1:], ('fictional-response', NOW.isoformat(), 'completed', 'valid_format', 'requires_current_validation'))

    def test_future_provided_response_installs_and_reuses_without_mock(self):
        capture = self.prepare()  # before the response exists
        rid = p.save(self.db, 'provided-arrival', capture, payload(), NOW)
        self.assertTrue(p.install_status(self.db, rid, self.g, {}, {})['allowed'])
        result = cache.run(self.db, [self.g], NOW)
        self.assertEqual(result['cache_validated'], 1); self.assertEqual(result['mock_requests'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_request_change_prevents_reuse(self):
        rid = self.save(); meta = cache.binding(self.db, self.g, {}, {}); request = cache.request(self.g); request['max_output_tokens'] = 1199
        self.assertFalse(p.reusable(self.db, rid, request, self.g['articles'], {'group': self.g, 'binding': meta}, meta['validator_hash'])['allowed'])

    def test_article_change_during_response_keeps_history_but_cannot_install(self):
        capture = self.prepare(); changed = copy.deepcopy(self.g); changed['articles'][0]['description'] += '訂正'
        rid = p.save(self.db, 'late-response', capture, payload(), NOW)
        self.assertFalse(p.install_status(self.db, rid, changed, {}, {})['allowed'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM response_records').fetchone()[0], 1)

    def test_registry_change_prevents_reuse(self):
        rid = self.save(); self.assertFalse(p.install_status(self.db, rid, self.g, {}, {'changed': True})['allowed'])

    def test_validator_change_prevents_reuse(self):
        rid = self.save(); meta = cache.binding(self.db, self.g, {}, {})
        self.assertFalse(p.reusable(self.db, rid, cache.request(self.g), self.g['articles'], {'group': self.g, 'binding': meta}, 'changed')['allowed'])

    def test_capture_metadata_tampering_is_rejected(self):
        rid = self.save()
        with self.db: self.db.execute("UPDATE response_inputs SET sources='[]'")
        self.assertFalse(p.install_status(self.db, rid, self.g, {}, {})['allowed'])

    def test_payload_change_with_replaced_cache_checksum_is_not_provenance(self):
        rid = self.save(); p.install_status(self.db, rid, self.g, {}, {})
        value = payload(); value['output'][0]['content'][0]['text'] = json.dumps({'category': '対象外', 'tracking_value': 'low'})
        with self.db: self.db.execute('UPDATE status_exact_cache SET payload=?,answer_hash=?', (json.dumps(value), digest(value)))
        self.assertEqual(cache.run(self.db, [self.g], NOW)['cache_validated'], 0)

    def test_incomplete_answer_is_stored_not_reusable(self):
        capture = self.prepare(); value = payload(); value['status'] = 'incomplete'
        rid = p.save(self.db, 'incomplete', capture, value, NOW)
        self.assertFalse(p.install_status(self.db, rid, self.g, {}, {})['allowed'])
        self.assertEqual(self.db.execute('SELECT completion FROM response_records').fetchone()[0], 'incomplete')

    def test_unknown_completion_is_not_invented(self):
        value = payload(); value.pop('status'); value.pop('id'); rid = p.save(self.db, 'unknown', self.prepare(), value, NOW)
        self.assertEqual(self.db.execute('SELECT completion,response_external_id FROM response_records').fetchone(), ('unknown', None))
        self.assertFalse(p.install_status(self.db, rid, self.g, {}, {})['allowed'])

    def test_invalid_format_is_preserved_without_upgrade(self):
        value = payload(); value['output'][0]['content'][0]['text'] = '[]'
        rid = p.save(self.db, 'invalid', self.prepare(), value, NOW)
        self.assertEqual(self.db.execute('SELECT format_result FROM response_records').fetchone()[0], 'invalid_format')
        self.assertFalse(p.install_status(self.db, rid, self.g, {}, {})['allowed'])

    def test_legacy_missing_record_cannot_install(self):
        p.initialize(self.db); self.assertFalse(p.install_status(self.db, 'unknown-old-response', self.g, {}, {})['allowed'])

    def test_old_raw_response_cannot_be_relabelled_as_new_capture(self):
        value = payload()
        with self.db: self.db.execute('INSERT INTO source_files VALUES(?,?,?)', ('ai_response_cache.json', 'fictional-hash', json.dumps({'old-key': value})))
        capture = self.prepare()
        with self.assertRaises(ValueError): p.save(self.db, 'retrofit', capture, value, NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM response_records').fetchone()[0], 0)

    def test_response_operation_is_idempotent_and_conflicting_retry_rejected(self):
        capture = self.prepare(); first = p.save(self.db, 'op', capture, payload(), NOW)
        self.assertEqual(p.save(self.db, 'op', capture, payload(), NOW), first)
        changed = payload(); changed['id'] = 'different'
        with self.assertRaises(ValueError): p.save(self.db, 'op', capture, changed, NOW)
        self.assertEqual(self.db.execute('SELECT count(*) FROM response_records').fetchone()[0], 1)

    def test_response_save_failure_is_atomic(self):
        capture = self.prepare()
        with self.assertRaises(RuntimeError): p.save(self.db, 'op', capture, payload(), NOW, fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM response_records').fetchone()[0], 0)
        p.save(self.db, 'op', capture, payload(), NOW)

    def test_response_records_survive_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(pathlib.Path(td) / 'private.sqlite'); db = connect(path)
            capture = p.prepare_status(db, self.g, {}, {}, NOW); rid = p.save(db, 'op', capture, payload(), NOW); db.close()
            db = connect(path); self.assertTrue(p.install_status(db, rid, self.g, {}, {})['allowed']); db.close()

    def test_selected_head_change_invalidates(self):
        old = sync.seed(self.db, self.g['articles'][0], body_text='架空本文')
        rid = self.save()
        sync.change_source(self.db, 'body-update', self.g['articles'][0], old['state_id'], NOW, body_text='変更本文')
        self.assertFalse(p.install_status(self.db, rid, self.g, {}, {})['allowed'])

    def test_mock_capture_hook_records_metadata_too(self):
        cache.capture(self.db, self.g, payload())
        self.assertEqual(self.db.execute('SELECT count(*) FROM response_records').fetchone()[0], 1)

    def test_tracking_capture_hook_precedes_and_saves_mock_response(self):
        from test_revision_sync import RevisionSyncTests
        from test_tracking import NOW as SYNC_NOW
        fixture = RevisionSyncTests('test_body_change_invalidates_only_dependent_decisions')
        # Use public fixture setup only; no external news or API.
        fixture.setUp()
        try:
            jid = fixture.job(); prepared = fixture.db.execute('SELECT capture_id FROM response_inputs WHERE legacy_job=?', (jid,)).fetchone()
            self.assertIsNotNone(prepared)
            sync.call_mock(fixture.db, jid, lambda _: fixture.answer(), SYNC_NOW)
            record = fixture.db.execute('SELECT record_id FROM response_records WHERE capture_id=?', prepared).fetchone()
            self.assertIsNotNone(record)
            context = json.loads(fixture.db.execute('SELECT context FROM sync_jobs WHERE job_id=?', (jid,)).fetchone()[0])
            request = {'model': 'gpt-5.4-mini', 'input': fixture.db.execute('SELECT prompt FROM sync_jobs WHERE job_id=?', (jid,)).fetchone()[0], 'max_output_tokens': 1200}
            before = list(fixture.db.execute('SELECT * FROM events'))
            result = p.reusable(fixture.db, record[0], request, context['group']['articles'], context, context['guard']['validator'])
            self.assertTrue(result['allowed']); self.assertEqual(before, list(fixture.db.execute('SELECT * FROM events')))
            self.assertEqual(fixture.db.execute('SELECT count(*) FROM changes').fetchone()[0], 0)
        finally: fixture.tearDown()
