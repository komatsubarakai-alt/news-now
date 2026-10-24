"""Fictional fixtures only; no saved operational records in this file."""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from datetime import datetime, timezone
from engine import connect
from ai_cost_control import periods
import queue_diagnostics as q

NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


class DiagnosticTests(unittest.TestCase):
    def setUp(self): self.db = connect()
    def tearDown(self): self.db.close()

    def ledger(self):
        day, month = periods(NOW)
        return {'days': {day: {'calls': 0, 'reserved_micro_usd': 0}},
                'months': {month: {'reserved_micro_usd': 0}}, 'run': {'calls': 0}}

    def test_ambiguous_legacy_is_unknown(self):
        for reason in ['budget_deferred', 'call_budget_deferred', 'stage_budget_deferred', None, 'ValueError']:
            self.assertEqual(q.legacy_cause(reason), 'unknown')

    def test_explicit_evidence_and_rejection_are_distinct(self):
        self.assertEqual(q.legacy_cause('article_identity_unproved:private-url'), 'evidence')
        self.assertEqual(q.legacy_cause('same_rejected_answer'), 'rejected_unchanged')

    def test_amount_day(self):
        ledger = self.ledger(); day, _ = periods(NOW)
        ledger['days'][day]['reserved_micro_usd'] = 200000
        self.assertEqual(q.budget_causes(ledger, NOW, input_bytes=100), ['amount_daily'])

    def test_amount_month(self):
        ledger = self.ledger(); _, month = periods(NOW)
        ledger['months'][month]['reserved_micro_usd'] = 5000000
        self.assertEqual(q.budget_causes(ledger, NOW, input_bytes=100), ['amount_monthly'])

    def test_day_calls(self):
        ledger = self.ledger(); day, _ = periods(NOW); ledger['days'][day]['calls'] = 40
        self.assertEqual(q.budget_causes(ledger, NOW, input_bytes=100), ['calls_daily'])

    def test_run_calls(self):
        ledger = self.ledger(); ledger['run']['calls'] = 24
        self.assertEqual(q.budget_causes(ledger, NOW, input_bytes=100), ['calls_run'])

    def test_processing_slots_not_money_or_calls(self):
        self.assertEqual(q.budget_causes(self.ledger(), NOW, input_bytes=100, processing_used=4), ['processing_slots'])

    def test_all_limits_retained_and_ledger_unchanged(self):
        ledger = self.ledger(); day, month = periods(NOW)
        ledger['days'][day] = {'calls': 40, 'reserved_micro_usd': 200000}
        ledger['months'][month]['reserved_micro_usd'] = 5000000
        ledger['run']['calls'] = 24; before = copy.deepcopy(ledger)
        self.assertEqual(q.budget_causes(ledger, NOW, input_bytes=100, processing_used=4),
                         ['amount_daily', 'amount_monthly', 'calls_daily', 'calls_run', 'processing_slots'])
        self.assertEqual(ledger, before)

    def test_secret_and_personal_text_never_logged(self):
        secret = 'sk-fictional-secret Alice https://example.test/private PROMPT RESPONSE'
        row = q.failure(self.db, secret, ValueError(secret), secret, {'status': secret, 'output': secret}, NOW)
        encoded = json.dumps(list(self.db.execute('SELECT * FROM queue_diagnostics')))
        for part in ['sk-fictional', 'Alice', 'example.test', 'PROMPT', 'RESPONSE']:
            self.assertNotIn(part, encoded)
        self.assertEqual(row['safe_error'], 'redacted_unclassified_error')
        self.assertEqual(row['completion'], 'unknown'); self.assertIsNone(row['request_hash'])

    def test_safe_error_stage_hash_completion(self):
        row = q.failure(self.db, 'schedule_analysis', ValueError('incomplete_response'), 'a' * 64,
                        {'status': 'incomplete', 'output': 'private'}, NOW)
        self.assertEqual(row['safe_error'], 'incomplete_response')
        self.assertEqual(row['stage'], 'schedule_analysis')
        self.assertEqual(row['request_hash'], 'a' * 64); self.assertEqual(row['completion'], 'incomplete')

    def test_json_parse_error_does_not_log_document(self):
        try: json.loads('private personal response')
        except ValueError as exc: row = q.failure(self.db, 'schedule_analysis', exc, 'b' * 64, None, NOW)
        self.assertEqual(row['safe_error'], 'invalid_json')

    def test_custom_exception_class_not_logged(self):
        exc = type('PrivateName', (ValueError,), {})('private')
        self.assertEqual(q.failure(self.db, 'persistence', exc, None, None, NOW)['exception_type'], 'Exception')

    def schedule_fixture(self):
        # Standard fictional fixtures, never an actual expired-plan record.
        sys.path.insert(0, str(ROOT / 'tests'))
        from test_tracking import event, article, report, NOW as AT
        e = event(); a = article('容疑者を起訴', url='https://example.test/schedule', published=AT.isoformat())
        return e, a, report(a), AT

    def response(self, answer, status='completed'):
        return {'status': status, 'output': [{'content': [{'type': 'output_text', 'text': json.dumps(answer)}]}]}

    def test_schedule_selection_failure_stage(self):
        e, a, answer, at = self.schedule_fixture()
        row = q.schedule_replay(self.db, e, [a], {'matches': 'not a list'}, self.response(answer), at)
        self.assertEqual(row['stage'], 'schedule_selection')
        self.assertEqual(row['safe_error'], 'matches must be a list')
        self.assertEqual(row['completion'], 'unknown')

    def test_schedule_incomplete_analysis_records_no_answer(self):
        e, a, answer, at = self.schedule_fixture(); before = copy.deepcopy(e)
        selected = {'matches': [{'url': a['url'], 'confidence': 'high'}]}
        row = q.schedule_replay(self.db, e, [a], selected, self.response(answer, 'incomplete'), at)
        self.assertEqual(row['stage'], 'schedule_analysis')
        self.assertEqual(row['safe_error'], 'incomplete_response')
        self.assertEqual(row['completion'], 'incomplete'); self.assertEqual(e, before)
        self.assertNotIn(a['title'], str(list(self.db.execute('SELECT * FROM queue_diagnostics'))))

    def test_schedule_non_object_analysis_records_safe_error(self):
        e, a, _, at = self.schedule_fixture()
        selected = {'matches': [{'url': a['url'], 'confidence': 'high'}]}
        row = q.schedule_replay(self.db, e, [a], selected, self.response([]), at)
        self.assertEqual(row['stage'], 'schedule_analysis')
        self.assertEqual(row['safe_error'], 'JSON object required')

    def test_schedule_evidence_validation_stage(self):
        e, a, answer, at = self.schedule_fixture()
        answer['confidence'] = 'low'
        row = q.schedule_replay(self.db, e, [a], {'matches': [{'url': a['url'], 'confidence': 'high'}]}, self.response(answer), at)
        self.assertEqual(row['stage'], 'schedule_validation'); self.assertEqual(row['cause'], 'evidence')

    def test_schedule_success_does_not_save_or_adopt(self):
        e, a, answer, at = self.schedule_fixture(); before = copy.deepcopy(e)
        row = q.schedule_replay(self.db, e, [a], {'matches': [{'url': a['url'], 'confidence': 'high'}]}, self.response(answer), at)
        self.assertEqual(row['status'], 'validated_reference_only'); self.assertFalse(row['can_publish'])
        self.assertEqual(e, before); self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_schedule_unknown_completion_not_validated(self):
        e, a, answer, at = self.schedule_fixture(); p = self.response(answer); p.pop('status')
        row = q.schedule_replay(self.db, e, [a], {'matches': [{'url': a['url'], 'confidence': 'high'}]}, p, at)
        self.assertEqual(row['safe_error'], 'cache_incomplete'); self.assertEqual(row['completion'], 'unknown')

