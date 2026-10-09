import copy
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from event_tracker import merge_incoming
from merge_safety import review, save_reviews
from pending_triage import current_groups, group_key, summary
from tracking_sources import AI
from test_tracking import NOW, FakeAI, article, event, report
from test_merge_safety import group


class PendingTriageTests(unittest.TestCase):
    def test_skipped_sources_do_not_starve_new_news(self):
        e = event()
        saved = [group(copy.deepcopy(e['articles'][0])) for _ in range(20)]
        outside = [group(article(url='https://example.test/sport'), category='対象外')]
        new = group(article('容疑者を起訴', 'https://example.test/new'))
        state = {}
        ai = FakeAI(report(new['articles'][0]))
        pending = merge_incoming([e], saved + outside + [new], ai, NOW, triage=state)
        self.assertEqual(pending, [])
        self.assertIn(new['articles'][0]['url'], {a['url'] for a in e['articles']})
        self.assertEqual(state[group_key(outside[0])]['group'], outside[0])
        self.assertEqual(state[group_key(outside[0])]['status'], 'out_of_scope')

    def test_evidence_failures_pause_after_three_and_preserve_sources(self):
        g = group(article('容疑者を起訴', 'https://example.test/new'))
        state = {}
        answer = {'confidence': 'low', 'relation': 'uncertain'}
        for i in range(3):
            pending = merge_incoming([event()], [g], FakeAI(answer), NOW + timedelta(hours=i), triage=state)
        self.assertEqual(pending, [])
        item = state[group_key(g)]
        self.assertEqual(item['status'], 'manual_review')
        self.assertEqual(item['decision_attempts'], 3)
        self.assertEqual(item['group'], g)
        with patch('event_tracker.analyze', side_effect=AssertionError('must not retry')):
            merge_incoming([event()], [g], FakeAI(answer), NOW, triage=state)
        changed = copy.deepcopy(g)
        changed['articles'][0]['description'] = '追加された案件固有の根拠'
        pending = merge_incoming([event()], [changed], FakeAI(answer), NOW, triage=state)
        self.assertEqual(pending, [changed])
        self.assertEqual(state[group_key(g)]['decision_attempts'], 1)

    def test_api_failures_do_not_consume_evidence_attempts(self):
        from urllib.error import HTTPError
        g = group(article(url='https://example.test/new'))
        state = {}
        for i in range(4):
            merge_incoming([event()], [g], FakeAI(HTTPError('https://example.test', 429, 'quota', {}, None)), NOW, triage=state)
        self.assertEqual(state[group_key(g)]['status'], 'retry_api')
        self.assertEqual(state[group_key(g)]['decision_attempts'], 0)

    def test_failure_diagnostics_include_answer_and_target(self):
        g = group(article('容疑者を起訴', 'https://example.test/new'))
        answer = report(g['articles'][0])
        answer['identity_matches'] = []
        reviews = []
        merge_incoming([event()], [g], FakeAI(answer), NOW, reviews)
        d = reviews[0]['diagnostics']
        self.assertEqual(d['target_event_id'], event()['event_id'])
        self.assertEqual(d['ai_report'], answer)
        self.assertEqual(d['input_urls'], [g['articles'][0]['url']])
        self.assertEqual(reviews[0]['observed_at'], NOW.isoformat())

    def test_legacy_review_preserved_and_new_observation_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'review.json')
            legacy = {'review_id': 'legacy', 'reason': 'old', 'payload': {}, 'stage': 'backfill'}
            Path(path).write_text(json.dumps([legacy]))
            first = review('event_attachment', 'uncertain', {'group': group(article())}, at=NOW,
                           diagnostics={'ai_report': {'confidence': 'low'}})
            second = review('event_attachment', 'uncertain', first['payload'], at=NOW + timedelta(hours=1),
                            diagnostics={'ai_report': {'confidence': 'medium'}})
            save_reviews([first], path)
            save_reviews([second], path)
            data = json.loads(Path(path).read_text())
            self.assertEqual(data[0], legacy)
            self.assertEqual(len(data), 2)
            self.assertEqual(data[1]['observation_count'], 2)
            self.assertEqual(data[1]['first_seen_at'], NOW.isoformat())
            self.assertEqual(data[1]['diagnostics']['ai_report']['confidence'], 'medium')
            self.assertEqual(data[1]['observation_history'][0]['diagnostics']['ai_report']['confidence'], 'low')

    def test_updated_content_supersedes_pending_and_reopens_manual_review(self):
        original = group(article('容疑者を起訴', 'https://example.test/new'))
        state = {}
        for _ in range(3):
            merge_incoming([event()], [original], FakeAI({'confidence': 'low'}), NOW, triage=state)
        updated = copy.deepcopy(original)
        updated['articles'][0]['description'] = '追加の案件固有情報'
        queue = current_groups([original], [updated])
        self.assertEqual(queue, [updated])
        pending = merge_incoming([event()], queue, FakeAI({'confidence': 'low'}), NOW, triage=state)
        self.assertEqual(pending, [updated])
        self.assertEqual(state[group_key(original)]['decision_attempts'], 1)
        self.assertEqual(state[group_key(original)]['source_history'][0]['group'], original)

    def test_changed_url_and_editorial_hold_keep_original_sources(self):
        original = group(article('容疑者を起訴', 'https://example.test/new'))
        state = {}
        for _ in range(3):
            merge_incoming([event()], [original], FakeAI({'confidence': 'low'}), NOW, triage=state)
        updated = copy.deepcopy(original)
        updated['articles'][0]['url'] = 'https://example.test/revised'
        registry = {'held_articles': [{'status': 'held', 'article': original['articles'][0]}]}
        with patch('event_tracker.load_corrections', return_value=registry), patch(
                'event_tracker.analyze', side_effect=AssertionError('editorial hold must not call AI')):
            pending = merge_incoming([event()], [updated], FakeAI({}), NOW, triage=state)
        self.assertEqual(pending, [])
        self.assertEqual(state[group_key(updated)]['status'], 'editorial_hold')
        self.assertEqual(state[group_key(original)]['group'], original)

    def test_format_error_is_separate_from_api_and_evidence(self):
        g = group(article(url='https://example.test/new'))
        state = {}
        pending = merge_incoming([event()], [g], FakeAI(json.JSONDecodeError('bad', '{', 0)), NOW, triage=state)
        self.assertEqual(pending, [g])
        self.assertEqual(state[group_key(g)]['status'], 'retry_format')
        self.assertEqual(state[group_key(g)]['decision_attempts'], 0)

    def test_invalid_json_answer_keeps_excerpt_without_request_credentials(self):
        payload = {'output': [{'content': [{'type': 'output_text', 'text': '{bad json'}]}]}
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-secret'}), patch('urllib.request.urlopen',
                return_value=io.BytesIO(json.dumps(payload).encode())):
            with self.assertRaises(json.JSONDecodeError) as raised:
                AI().ask('test')
        self.assertEqual(raised.exception.ai_response_excerpt, '{bad json')
        self.assertNotIn('test-secret', raised.exception.ai_response_excerpt)

    def test_metrics_separate_waiting_and_archived(self):
        state = {}
        outside = group(article(url='https://example.test/out'), category='対象外')
        uncertain = group(article(url='https://example.test/new'))
        pending = merge_incoming([], [outside, uncertain], FakeAI({'confidence': 'low'}), NOW, triage=state)
        metrics = summary(state, pending)
        self.assertEqual(metrics['queue_counts'], {'needs_evidence': 1})
        self.assertEqual(metrics['retained_counts']['out_of_scope'], 1)


if __name__ == '__main__':
    unittest.main()
