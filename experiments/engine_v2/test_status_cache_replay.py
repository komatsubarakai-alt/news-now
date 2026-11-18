"""Offline classification-only regression tests, entirely fictional inputs."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from datetime import datetime, timezone
from engine import connect, dumps
from failed_response import digest
from pending_triage import group_key, fingerprint
import status_cache_replay as c
import queue_diagnostics as d

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


def group(n=0):
    return {'event_name': '架空施設の制度変更' + str(n), 'articles': [
        {'url': 'https://example.test/' + str(n), 'title': '架空施設の制度変更' + str(n),
         'published': '2026-01-01T00:00:00Z', 'description': '架空の説明'}]}


def payload(category='政策・制度', value='medium'):
    return {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': json.dumps(
        {'category': category, 'tracking_value': value, 'summary': '架空・未検証の分類'}, ensure_ascii=False)}]}]}


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(); self.g = group()
        self.net = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')); self.net.start()
    def tearDown(self): self.net.stop(); self.db.close()
    def capture(self, g=None, p=None, **kw): return c.capture(self.db, g or self.g, p or payload(), **kw)
    def run_cache(self, groups=None, **kw): return c.run(self.db, groups or [self.g], NOW, **kw)

    def test_valid_cache_only_creates_classification_reference(self):
        self.capture(); result = self.run_cache()
        self.assertEqual(result['cache_validated'], 1); self.assertEqual(result['processing_used'], 0)
        self.assertEqual(result['downstream'], 1); self.assertEqual(result['total_waiting'], 1)
        for table in ['events', 'changes', 'progress', 'decisions', 'budget']:
            self.assertEqual(self.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertFalse(result['can_publish'])

    def test_empty_source_cannot_be_classified(self):
        invalid = {'event_name': '架空', 'articles': []}; self.capture(invalid)
        self.assertEqual(self.run_cache([invalid])['cache_validated'], 0)

    def test_unsafe_source_url_cannot_be_classified(self):
        invalid = copy.deepcopy(self.g); invalid['articles'][0]['url'] = 'javascript:alert(1)'
        self.capture(invalid)
        self.assertEqual(self.run_cache([invalid])['cache_validated'], 0)

    def test_no_publication_or_adoption_side_effect(self):
        import publication
        publication.initialize(self.db)
        tables = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0].startswith(('pub_', 'sync_'))]
        before = {name: list(self.db.execute('SELECT * FROM ' + name)) for name in tables}
        self.capture(); self.run_cache()
        self.assertEqual(before, {name: list(self.db.execute('SELECT * FROM ' + name)) for name in tables})

    def test_known_cache_and_mock_same_frame_comparison(self):
        groups = [group(i) for i in range(6)]
        answers = {digest(c.request(g)): payload() for g in groups[:4]}
        self.capture(groups[4]); self.capture(groups[5], payload('対象外', 'low'))
        before = c.run(self.db, groups, NOW, cache_first=False, mock=c.MockResponses(answers))
        self.assertEqual((before['processing_used'], before['new_results'], before['status_remaining'],
                          before['downstream'], before['total_waiting']), (4, 4, 2, 4, 6))
        after = c.run(self.db, groups, NOW, mock=c.MockResponses(answers))
        self.assertEqual((after['cache_validated'], after['status_remaining'], after['downstream'], after['total_waiting']), (2, 0, 5, 5))

    def test_independent_baseline_and_proposal_use_same_four_mock_requests(self):
        groups = [group(i) for i in range(6)]; answers = {digest(c.request(g)): payload() for g in groups[:4]}
        results = []
        for first in [False, True]:
            db = connect(); c.capture(db, groups[4], payload()); c.capture(db, groups[5], payload('対象外', 'low'))
            results.append(c.run(db, groups, NOW, cache_first=first, mock=c.MockResponses(answers))); db.close()
        self.assertEqual([r['mock_requests'] for r in results], [4, 4])
        self.assertEqual([r['new_results'] for r in results], [4, 6])

    def test_raw_legacy_request_hit_is_not_article_or_condition_proof(self):
        raw = {digest(c.request(self.g)): payload()}
        result = self.run_cache(raw_legacy_cache=raw)
        self.assertEqual(result['legacy_unknown'], 1); self.assertEqual(result['cache_validated'], 0)
        self.assertEqual(result['status_remaining'], 1)
        self.assertEqual(result['wait_counts'], {'unknown': 1})

    def test_no_legacy_capture_upgrade(self):
        with self.assertRaises(ValueError): self.capture(origin='saved_capture_manifest')

    def test_title_revision_invalidates(self):
        self.capture(); changed = copy.deepcopy(self.g); changed['articles'][0]['title'] += ' 訂正'
        self.assertEqual(self.run_cache([changed])['cache_validated'], 0)

    def test_same_url_body_revision_invalidates(self):
        self.capture(); changed = copy.deepcopy(self.g); changed['articles'][0]['body'] = '新しい本文'
        self.assertEqual(self.run_cache([changed])['cache_validated'], 0)

    def test_description_change_invalidates_even_with_same_prompt(self):
        self.capture(); changed = copy.deepcopy(self.g); changed['articles'][0]['description'] = '本文相当の新情報'
        self.assertEqual(c.request(changed), c.request(self.g))
        self.assertEqual(self.run_cache([changed])['cache_validated'], 0)

    def test_different_url_invalidates_even_with_same_prompt(self):
        self.capture(); changed = copy.deepcopy(self.g); changed['articles'][0]['url'] += '/other'
        self.assertEqual(self.run_cache([changed])['cache_validated'], 0)

    def test_publication_datetime_invalidates(self):
        self.capture(); changed = copy.deepcopy(self.g); changed['articles'][0]['published'] = '2026-01-02T00:00:00Z'
        self.assertEqual(self.run_cache([changed])['cache_validated'], 0)

    def test_registry_change_invalidates(self):
        self.capture(); self.assertEqual(self.run_cache(registry={'held_articles': ['changed']})['cache_validated'], 0)

    def test_operator_triage_change_invalidates(self):
        self.capture(); state = {group_key(self.g): {'operator_review': {'action': 'hold'}}}
        self.assertEqual(self.run_cache(triage=state)['cache_validated'], 0)

    def test_observation_clock_does_not_invalidate_success(self):
        state = {group_key(self.g): {'last_seen_at': 'old', 'cached_rejection_skips': 0}}
        self.capture(triage=state); self.run_cache(triage=state)
        state[group_key(self.g)]['last_seen_at'] = 'new'
        state[group_key(self.g)]['cached_rejection_skips'] = 1
        result = self.run_cache(triage=state)
        self.assertEqual(result['new_results'], 0); self.assertEqual(result['status_remaining'], 0)

    def test_observation_clock_does_not_reopen_rejection(self):
        state = {group_key(self.g): {'last_seen_at': 'old'}}
        self.capture(p=payload('invalid'), triage=state); self.run_cache(triage=state)
        state[group_key(self.g)]['last_seen_at'] = 'new'
        result = self.run_cache(triage=state)
        self.assertEqual(result['cache_checked'], 0)
        self.assertEqual(result['wait_counts'], {'rejected_unchanged': 1})

    def test_event_change_invalidates(self):
        self.capture()
        with self.db: self.db.execute('INSERT INTO events VALUES(?,?)', ('fiction', dumps({'event_id': 'fiction'})))
        self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_validator_change_invalidates(self):
        self.capture()
        with patch.object(c, 'validator_hash', return_value='changed'):
            self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_bad_classification_is_rejected_not_retried_or_counted(self):
        self.capture(p=payload('invalid')); first = self.run_cache(); before = self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0]
        second = self.run_cache(mock=c.MockResponses({digest(c.request(self.g)): payload()}))
        self.assertEqual(first['rejected'], 1); self.assertEqual(second['cache_checked'], 0)
        self.assertEqual(second['processing_used'], 0); self.assertEqual(second['wait_counts'], {'rejected_unchanged': 1})
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0], before)

    def test_changed_answer_reopens_rejection(self):
        self.capture(p=payload('invalid')); self.run_cache()
        self.capture(p=payload()); self.assertEqual(self.run_cache()['cache_validated'], 1)

    def test_changed_source_reopens_with_new_capture(self):
        self.capture(p=payload('invalid')); self.run_cache(); changed = copy.deepcopy(self.g)
        changed['articles'][0]['description'] += '改訂'; self.capture(changed)
        self.assertEqual(self.run_cache([changed])['cache_validated'], 1)

    def test_changed_validation_context_can_be_recaptured(self):
        self.capture(); state = {group_key(self.g): {'reason': 'changed'}}
        self.capture(triage=state); self.assertEqual(self.run_cache(triage=state)['cache_validated'], 1)

    def test_manual_pause_not_reopened_by_cache(self):
        state = {group_key(self.g): {'status': 'manual_review', 'source_fingerprint': fingerprint(self.g)}}
        self.capture(triage=state); self.assertEqual(self.run_cache(triage=state)['cache_validated'], 0)

    def test_payload_tampering_rejected(self):
        self.capture()
        with self.db: self.db.execute('UPDATE status_exact_cache SET payload=?', (dumps(payload('対象外', 'low')),))
        self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_binding_metadata_tampering_rejected(self):
        self.capture()
        with self.db: self.db.execute("UPDATE status_exact_cache SET article_hash='unrelated'")
        self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_same_answer_new_response_id_does_not_repeat_rejection(self):
        p = payload('invalid'); p['id'] = 'mock-first'
        self.capture(p=p); self.run_cache(); before = self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0]
        p['id'] = 'mock-second'; p['usage'] = {'mock': 3}; self.capture(p=p)
        result = self.run_cache()
        self.assertEqual(result['cache_checked'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0], before)

    def test_same_answer_new_response_id_does_not_duplicate_success(self):
        p = payload(); self.capture(p=p); self.run_cache(); p['id'] = 'mock-new-id'; self.capture(p=p)
        result = self.run_cache(); self.assertEqual(result['new_results'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0], 1)

    def test_fixed_corrupt_payload_can_be_validated(self):
        self.capture()
        with self.db: self.db.execute('UPDATE status_exact_cache SET payload=?', (dumps(payload('対象外', 'low')),))
        self.run_cache(); self.capture()
        self.assertEqual(self.run_cache()['cache_validated'], 1)

    def test_incomplete_cache_is_not_adopted(self):
        p = payload(); p['status'] = 'incomplete'; self.capture(p=p)
        self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_unconfirmed_completion_is_not_adopted(self):
        p = payload(); del p['status']; self.capture(p=p)
        self.assertEqual(self.run_cache()['cache_validated'], 0)

    def test_invalid_json_cache_is_not_adopted(self):
        p = payload(); p['output'][0]['content'][0]['text'] = 'private malformed answer'
        self.capture(p=p); self.assertEqual(self.run_cache()['cache_validated'], 0)
        self.assertNotIn('private malformed', str(list(self.db.execute('SELECT * FROM queue_diagnostics'))))

    def test_success_is_not_revalidated_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'shadow.sqlite'); db = connect(path); c.capture(db, self.g, payload())
            c.run(db, [self.g], NOW); db.close(); db = connect(path)
            result = c.run(db, [self.g], NOW); self.assertEqual(result['cache_checked'], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0], 1); db.close()

    def test_transaction_failure_has_no_result_or_success_receipt(self):
        self.capture()
        with self.assertRaises(RuntimeError): self.run_cache(fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_cache_attempts').fetchone()[0], 0)
        self.assertEqual(self.run_cache()['cache_validated'], 1)

    def test_fresh_fixture_success_survives_without_fixture_on_resume(self):
        first = self.run_cache(mock=c.MockResponses({digest(c.request(self.g)): payload()}))
        self.assertEqual(first['new_results'], 1)
        after = self.run_cache()
        self.assertEqual(after['status_remaining'], 0); self.assertEqual(after['new_results'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0], 1)

    def test_fresh_fixture_save_failure_retries_without_another_mock_request(self):
        with self.assertRaises(RuntimeError):
            self.run_cache(mock=c.MockResponses({digest(c.request(self.g)): payload()}), fail_save=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM status_shadow_results').fetchone()[0], 0)
        after = self.run_cache()
        self.assertEqual(after['cache_validated'], 1); self.assertEqual(after['mock_requests'], 0)

    def test_fresh_invalid_answer_not_rechecked_after_restart(self):
        self.run_cache(mock=c.MockResponses({digest(c.request(self.g)): payload('invalid')}))
        before = self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0]
        after = self.run_cache()
        self.assertEqual(after['cache_checked'], 0); self.assertEqual(after['processing_used'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM queue_diagnostics').fetchone()[0], before)

    def test_cache_limit_and_cursor_resume_without_dropping(self):
        groups = [group(i) for i in range(3)]
        for g in groups: self.capture(g)
        results = [self.run_cache(groups, cache_limit=1) for _ in range(3)]
        self.assertEqual([r['cache_validated'] for r in results], [1, 1, 1])
        self.assertEqual(results[-1]['status_remaining'], 0)

    def test_scan_limit_resumes_before_late_cache(self):
        groups = [group(i) for i in range(3)]; self.capture(groups[-1])
        self.assertEqual(self.run_cache(groups, scan_limit=1)['cache_validated'], 0)
        self.assertEqual(self.run_cache(groups, scan_limit=1)['cache_validated'], 0)
        self.assertEqual(self.run_cache(groups, scan_limit=1)['cache_validated'], 1)

    def test_duplicate_entry_refused_not_double_processed(self):
        self.capture()
        with self.assertRaises(ValueError): self.run_cache([self.g, self.g])

    def test_no_arbitrary_callable_transport(self):
        with self.assertRaises(ValueError): self.run_cache(mock=lambda: 'network')

    def test_limits_cannot_be_raised(self):
        for kw in [{'cache_limit': 17}, {'processing_limit': 5}, {'scan_limit': 2001}]:
            with self.assertRaises(ValueError): self.run_cache(**kw)

    def test_cache_works_when_communication_amount_is_exhausted(self):
        self.capture(); day, month = d.periods(NOW)
        ledger = {'days': {day: {'reserved_micro_usd': 200000, 'calls': 40}},
                  'months': {month: {'reserved_micro_usd': 5000000}}, 'run': {'calls': 24}}
        before = copy.deepcopy(ledger)
        result = self.run_cache(observed_ledger=ledger)
        self.assertEqual(result['cache_validated'], 1); self.assertEqual(ledger, before)

    def test_current_budget_limits_are_not_legacy_attribution(self):
        day, _ = d.periods(NOW); ledger = {'days': {day: {'calls': 40}}}
        result = self.run_cache(observed_ledger=ledger)
        self.assertEqual(result['wait_counts'], {'calls_daily': 1})
        self.assertEqual(d.legacy_cause('budget_deferred'), 'unknown')

