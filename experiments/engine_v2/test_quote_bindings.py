"""Event-scoped literal quotation tests; all data are fictional."""
import copy
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(pathlib.Path(__file__).parent)]
from engine import connect, dumps
import revision_sync as sync
import quote_bindings as q
import operator_app as app
from failed_response import digest

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


class QuoteTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(); app.initialize(self.db)
        self.article = {'url': 'https://example.test/shared', 'title': '架空事件Aを起訴。架空事件Bは別件。',
                        'description': '架空の紹介文', 'published': '2026-01-01T00:00:00Z'}
        for eid in ('event-A', 'event-B'):
            self.db.execute('INSERT INTO events VALUES(?,?)', (eid, dumps({'event_id': eid, 'event_name': eid, 'articles': []})))
        self.db.commit(); self.state = sync.seed(self.db, self.article, body_text='架空事件Aを起訴。架空事件Bは捜査中。')
    def tearDown(self): self.db.close()
    def record(self, **kw):
        args = dict(event_id='event-A', url=self.article['url'], quote='架空事件Aを起訴', kind='headline',
                    origin='operator_input', now=NOW, state_id=self.state['state_id'])
        args.update(kw); return q.record(self.db, 'quote:' + digest({**args, 'now': args['now'].isoformat()}), **args)

    def test_headline_literal_not_body_or_source_confirmed(self):
        value = q.inspect(self.db, self.record())
        self.assertEqual(value['literal_status'], 'exact_literal_match'); self.assertEqual(value['start'], 0)
        self.assertEqual(value['end'], len('架空事件Aを起訴')); self.assertTrue(value['content_hash']); self.assertTrue(value['version_id'])
        self.assertEqual(value['body_confirmation'], 'unconfirmed'); self.assertEqual(value['source_confirmation'], 'unconfirmed')

    def test_rss_quote_uses_original_description_position(self):
        value = q.inspect(self.db, self.record(kind='rss_description', quote='紹介文'))
        self.assertEqual(value['literal_status'], 'exact_literal_match'); self.assertEqual(value['start'], 3)

    def test_body_literal_is_separate_from_human_body_check(self):
        value = q.inspect(self.db, self.record(kind='body'))
        self.assertEqual(value['literal_status'], 'exact_literal_match'); self.assertEqual(value['body_confirmation'], 'unconfirmed')

    def test_missing_body_remains_unknown(self):
        a = {**self.article, 'url': 'https://example.test/no-body'}; state = sync.seed(self.db, a)
        value = q.inspect(self.db, self.record(url=a['url'], state_id=state['state_id'], kind='body'))
        self.assertEqual(value['literal_status'], 'body_missing'); self.assertIsNone(value['start']); self.assertIsNone(value['evidence_id'])

    def test_old_quote_without_version_does_not_bind_to_matching_current_head(self):
        value = q.inspect(self.db, self.record(state_id=None, origin='saved_ai_response', response_record_id='legacy-reference'))
        self.assertEqual(value['literal_status'], 'unbound_version'); self.assertIsNone(value['version_id']); self.assertIsNone(value['content_hash'])
        self.assertIsNone(value['evidence_id']); self.assertEqual(value['confirmations'], [])

    def test_old_ai_quote_cannot_be_assigned_current_version_without_provenance(self):
        with self.assertRaises(ValueError): self.record(origin='saved_ai_response', response_record_id='legacy-reference')

    def captured_ai(self, quote):
        import response_provenance as p
        import json
        group = {'event_name': '架空事件A', 'articles': [self.article]}
        capture = p.prepare(self.db, {'input': 'fictional request', 'model': 'gpt-5.4-mini', 'max_output_tokens': 1200}, [self.article], ['event-A'], {'group': group}, 'fictional-validator', NOW, stage='tracking')
        value = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'quote': quote, 'event_id': 'event-A'})}]}]}
        return p.save(self.db, 'captured:' + digest(quote), capture, value, NOW)

    def test_captured_ai_quote_can_be_linked_without_promoting_truth(self):
        rid = self.captured_ai('架空事件Aを起訴')
        value = q.inspect(self.db, self.record(origin='saved_ai_response', response_record_id=rid))
        self.assertEqual(value['literal_status'], 'exact_literal_match')
        self.assertEqual(value['source_confirmation'], 'unconfirmed'); self.assertFalse(value['gold_promoted'])

    def test_quote_not_in_original_response_cannot_claim_ai_origin(self):
        rid = self.captured_ai('架空事件Aを起訴')
        with self.assertRaises(ValueError): self.record(quote='架空事件Bは別件', origin='saved_ai_response', response_record_id=rid)

    def test_other_event_cannot_use_captured_ai_scope(self):
        rid = self.captured_ai('架空事件Aを起訴')
        with self.assertRaises(ValueError): self.record(event_id='event-B', origin='saved_ai_response', response_record_id=rid)

    def test_presented_candidate_is_not_the_answered_event(self):
        import response_provenance as p
        import json
        group = {'event_name': '架空', 'articles': [self.article]}
        capture = p.prepare(self.db, {'input': 'fictional', 'model': 'gpt-5.4-mini', 'max_output_tokens': 1200}, [self.article], ['event-A', 'event-B'], {'group': group}, 'fictional', NOW, stage='tracking')
        value = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'quote': '架空事件Bは別件', 'event_id': 'event-A'})}]}]}
        rid = p.save(self.db, 'two-candidates', capture, value, NOW)
        with self.assertRaises(ValueError): self.record(event_id='event-B', quote='架空事件Bは別件', origin='saved_ai_response', response_record_id=rid)

    def test_deleted_source_does_not_supply_new_usable_quote(self):
        sync.change_source(self.db, 'delete', self.article, self.state['state_id'], NOW, outcome='deleted')
        self.state = sync.head(self.db, self.article['url'])
        value = q.inspect(self.db, self.record())
        self.assertEqual(value['literal_status'], 'source_unavailable'); self.assertIsNone(value['evidence_id'])

    def test_mismatched_quote_does_not_gain_evidence(self):
        value = q.inspect(self.db, self.record(quote='本文に存在しない引用'))
        self.assertEqual(value['literal_status'], 'literal_mismatch'); self.assertIsNone(value['evidence_id'])

    def test_multiple_occurrences_need_explicit_position(self):
        a = {**self.article, 'url': 'https://example.test/repeat', 'title': '同じ語 同じ語'}; state = sync.seed(self.db, a)
        self.assertEqual(q.inspect(self.db, self.record(url=a['url'], state_id=state['state_id'], quote='同じ語'))['literal_status'], 'ambiguous_position')
        self.assertEqual(q.inspect(self.db, self.record(url=a['url'], state_id=state['state_id'], quote='同じ語', position=[4, 7]))['literal_status'], 'exact_literal_match')

    def test_wrong_range_not_accepted(self):
        value = q.inspect(self.db, self.record(position=[1, 4]))
        self.assertEqual(value['literal_status'], 'literal_mismatch')

    def test_shared_url_keeps_event_quotes_separate(self):
        a = self.record(); b = self.record(event_id='event-B', quote='架空事件Bは別件')
        self.assertNotEqual(a, b)
        self.assertEqual([r['quote_id'] for r in q.for_event(self.db, 'event-A', self.article['url'])], [a])
        self.assertEqual([r['quote_id'] for r in q.for_event(self.db, 'event-B', self.article['url'])], [b])

    def reference(self, qid, eid='event-A'):
        value = q.inspect(self.db, qid)
        ids = [value['evidence_id'], sync.add_evidence(self.db, eid, self.article['url'], 'metadata')]
        return sync.record_reference(self.db, 'reference:' + qid, eid, 'operator', 'hold', {'basis': 'fictional confirmation only'}, ids, NOW)['decision_id']

    def test_explicit_source_confirmation_records_actual_time_and_reference(self):
        qid = self.record(); did = self.reference(qid)
        q.confirm(self.db, 'confirm', qid, kind='source', actor='fictional-operator', checked_at=NOW.isoformat(), decision_id=did)
        value = q.inspect(self.db, qid)
        self.assertEqual(value['source_confirmation'], 'confirmed'); self.assertEqual(value['body_confirmation'], 'unconfirmed')
        self.assertEqual(value['confirmations'][0]['decision_id'], did); self.assertFalse(value['gold_promoted'])

    def test_headline_cannot_be_body_confirmation(self):
        qid = self.record(); did = self.reference(qid)
        with self.assertRaises(ValueError): q.confirm(self.db, 'body', qid, kind='body', actor='fictional', checked_at=NOW.isoformat(), decision_id=did)

    def test_source_confirmation_requires_explicit_time_and_matching_decision(self):
        qid = self.record(); did = self.reference(qid)
        with self.assertRaises(ValueError): q.confirm(self.db, 'none', qid, kind='source', actor='fictional', checked_at=None, decision_id=did)
        other = self.record(event_id='event-B', quote='架空事件Bは別件'); other_did = self.reference(other, 'event-B')
        with self.assertRaises(ValueError): q.confirm(self.db, 'cross', qid, kind='source', actor='fictional', checked_at=NOW.isoformat(), decision_id=other_did)

    def test_changed_version_invalidates_confirmation_and_judgment(self):
        qid = self.record(); did = self.reference(qid)
        q.confirm(self.db, 'confirm', qid, kind='source', actor='fictional', checked_at=NOW.isoformat(), decision_id=did)
        changed = {**self.article, 'published': '2026-01-02T00:00:00Z'}
        sync.change_source(self.db, 'change', changed, self.state['state_id'], NOW)
        self.assertEqual(q.inspect(self.db, qid)['literal_status'], 'stale_source')
        self.assertEqual(q.inspect(self.db, qid)['source_confirmation'], 'unconfirmed')
        self.assertEqual(self.db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?', (did,)).fetchone()[0], 'needs_recheck')

    def test_quote_save_failure_rolls_back_evidence_and_receipt(self):
        before = self.db.execute('SELECT count(*) FROM sync_evidence').fetchone()[0]
        with self.assertRaises(RuntimeError): self.record(fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM sync_evidence').fetchone()[0], before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM bound_quotes').fetchone()[0], 0)

    def test_duplicate_quote_operation_and_conflicting_retry(self):
        qid = self.record(); self.assertEqual(self.record(), qid)
        op = self.db.execute('SELECT operation_id FROM bound_quotes WHERE quote_id=?', (qid,)).fetchone()[0]
        with self.assertRaises(ValueError): q.record(self.db, op, event_id='event-A', url=self.article['url'], quote='別の文字', kind='headline', origin='operator_input', now=NOW)

    def test_unknown_source_does_not_create_confirmation_timestamp(self):
        value = q.inspect(self.db, self.record(state_id=None))
        self.assertEqual(value['confirmations'], []); self.assertEqual(value['source_confirmation'], 'unconfirmed')

    def test_operator_ui_saves_quote_reference_without_event_or_approval_change(self):
        from operator_task_demo import populate
        db = connect(); app.initialize(db); populate(db); state = app.snapshot(db)
        task = next(t for t in state['work']['tasks'] if t.get('article', {}).get('url', '').endswith('demo-08'))
        eid = task['suggested_event_id']; before = list(db.execute('SELECT * FROM events')); approvals = list(db.execute('SELECT * FROM pub_approvals'))
        command = dict(action='quote_record', op='fictional-ui-quote-save', revision=state['revision'], task_id=task['task_id'],
                       context_hash=task['review_contexts'][eid], event_id=eid, quote=task['article']['title'][:12], kind='headline', confirmed=True)
        result = app.operate(db, command)
        self.assertEqual(result['status'], 'quote_reference_saved'); self.assertEqual(app.operate(db, command), result)
        self.assertEqual(before, list(db.execute('SELECT * FROM events'))); self.assertEqual(approvals, list(db.execute('SELECT * FROM pub_approvals')))
        new = app.snapshot(db); task = next(t for t in new['work']['tasks'] if t['task_id'] == task['task_id'])
        self.assertTrue(task['drafts_by_event'][eid]['quote_binding_notes'])
        self.assertEqual(q.inspect(db, result['quote_id'])['body_confirmation'], 'unconfirmed'); db.close()

    def test_operator_ui_requires_explicit_confirmation(self):
        from operator_task_demo import populate
        db = connect(); app.initialize(db); populate(db); state = app.snapshot(db); task = next(t for t in state['work']['tasks'] if t.get('article'))
        eid = task['suggested_event_id']
        with self.assertRaises(ValueError): app.operate(db, dict(action='quote_record', op='fictional-unconfirmed', revision=state['revision'], task_id=task['task_id'],
            context_hash=task['review_contexts'][eid], event_id=eid, quote='架空', kind='headline', confirmed=False))
        db.close()
