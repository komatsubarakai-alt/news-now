import copy
import json
import os
import runpy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from event_tracker import analysis_prompt, merge_incoming
from failed_response import cached_answer, digest, rejection_signature, safe_signature
from pending_triage import group_key, summary
from tracking_sources import AI, BudgetExceeded
from test_tracking import NOW, article, event, report
from test_merge_safety import group


def payload(answer):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps(answer)}]}]}


class RejectedResponseTests(unittest.TestCase):
    def setUp(self):
        self.before = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.root = Path(__file__).resolve().parents[1]
        self.env = patch.dict(os.environ, {'AI_MAX_OUTPUT_TOKENS': '1200', 'TRACKING_MODEL': 'gpt-5.4-mini'})
        self.env.start()
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.g = group(article('容疑者を起訴', 'https://example.test/new'))
        self.events = [event()]
        self.state = {}
        self.reviews = []
        self.bad = {'confidence': 'low', 'relation': 'uncertain'}

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        os.chdir(self.before)
        self.tmp.cleanup()

    def put(self, name, value):
        Path(name).write_text(json.dumps(value), encoding='utf-8')

    def seed(self, prompt, answer):
        path = Path('ai_response_cache.json')
        cache = json.loads(path.read_text()) if path.exists() else {}
        key = digest({'model': 'gpt-5.4-mini', 'input': prompt, 'max_output_tokens': 1200})
        cache[key] = payload(answer)
        self.put(str(path), cache)
        return key

    def tracking(self, g=None, answer=None, now=NOW):
        g = g or self.g
        self.seed(analysis_prompt(self.events, g['articles'], incoming_event_name=g['event_name']),
                  self.bad if answer is None else answer)
        return merge_incoming(self.events, [g], AI(limit=0), now, self.reviews, self.state)

    def test_tracking_repeats_preserve_failures_sources_and_diagnostics(self):
        original = copy.deepcopy(self.events)
        self.assertEqual(self.tracking(), [self.g])
        item = self.state[group_key(self.g)]
        failure = item['last_failure_at']
        for hour in range(1, 6):
            with patch.object(AI, 'ask', side_effect=AssertionError('must skip')):
                self.assertEqual(self.tracking(now=NOW + timedelta(hours=hour)), [self.g])
        self.assertEqual(item['decision_attempts'], 1)
        self.assertEqual(item['last_failure_at'], failure)
        self.assertEqual(item['cached_rejection_skips'], 5)
        self.assertEqual(item['group'], self.g)
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.events, original)
        self.assertEqual(summary(self.state, [self.g])['cached_rejection_wait_count'], 1)
        self.assertFalse(Path('ai_budget.json').exists())

    def test_article_revisions_reopen_and_keep_original(self):
        for field, value in [('title', '別事件の容疑者を起訴'), ('description', '新たな根拠'),
                             ('published', '2026-10-09T10:00:00+00:00')]:
            with self.subTest(field=field):
                self.state = {}; self.reviews = []
                self.tracking()
                changed = copy.deepcopy(self.g); changed['articles'][0][field] = value
                self.assertEqual(self.tracking(changed), [changed])
                item = self.state[group_key(changed)]
                self.assertEqual(item['decision_attempts'], 1)
                self.assertEqual(item['source_history'][0]['group'], self.g)
                self.assertEqual(len(item['rejected_responses']), 2)
                self.assertEqual(len(self.reviews), 2)

    def test_target_evidence_and_registry_changes_reopen(self):
        self.tracking()
        self.events[0]['articles'][0]['description'] = '追加された既存事件の根拠'
        self.tracking()
        self.assertEqual(self.state[group_key(self.g)]['decision_attempts'], 2)
        with patch('event_tracker.load_corrections', return_value={'revision': 'new'}):
            self.tracking()
        self.assertEqual(self.state[group_key(self.g)]['decision_attempts'], 3)

    def test_changed_validation_reopens_without_relaxing_guards(self):
        self.tracking()
        with patch('failed_response.validation_revision', return_value='changed'):
            self.tracking()
        self.assertEqual(self.state[group_key(self.g)]['decision_attempts'], 2)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(self.events[0]['articles']), 1)

    def test_changed_answer_must_pass_existing_validation(self):
        self.tracking()
        unsupported = report(self.g['articles'][0], identity_matches=[])
        self.tracking(answer=unsupported)
        self.assertEqual(self.state[group_key(self.g)]['decision_attempts'], 2)
        self.assertEqual(len(self.events[0]['articles']), 1)
        self.assertEqual(self.tracking(answer=report(self.g['articles'][0])), [])
        self.assertEqual(len(self.events[0]['articles']), 2)

    def test_response_metadata_does_not_reopen_same_answer(self):
        self.tracking()
        cache = json.loads(Path('ai_response_cache.json').read_text())
        for value in cache.values():
            value.update(id='different-response', usage={'input_tokens': 99}, created_at=123)
        self.put('ai_response_cache.json', cache)
        with patch.object(AI, 'ask', side_effect=AssertionError('same answer')):
            merge_incoming(self.events, [self.g], AI(limit=0), NOW, self.reviews, self.state)
        self.assertEqual(self.state[group_key(self.g)]['decision_attempts'], 1)

    def test_missing_cache_and_budget_failure_not_memoized(self):
        self.tracking()
        Path('ai_response_cache.json').unlink()
        merge_incoming(self.events, [self.g], AI(limit=0), NOW, self.reviews, self.state)
        item = self.state[group_key(self.g)]
        self.assertEqual(item['decision_attempts'], 1)
        self.assertEqual(item['status'], 'queued')
        self.assertFalse(item.get('cached_rejection_waiting'))
        self.assertEqual(len(item['rejected_responses']), 1)

    def test_manual_review_is_not_automatically_reopened(self):
        self.tracking()
        self.state[group_key(self.g)]['status'] = 'manual_review'
        with patch.object(AI, 'ask', side_effect=AssertionError('manual hold')):
            self.assertEqual(self.tracking(), [])
        self.assertEqual(self.state[group_key(self.g)]['status'], 'manual_review')

    def test_transport_failure_does_not_create_rejection_or_consume_failure_count(self):
        ai = AI(limit=0)
        with patch.object(ai, 'ask', side_effect=TimeoutError('temporary')):
            self.assertEqual(merge_incoming(self.events, [self.g], ai, NOW, self.reviews, self.state), [self.g])
        item = self.state[group_key(self.g)]
        self.assertEqual(item['decision_attempts'], 0)
        self.assertNotIn('rejected_responses', item)
        self.tracking()
        self.assertEqual(item['decision_attempts'], 1)

    def test_other_url_is_not_the_same_article(self):
        self.tracking()
        changed = copy.deepcopy(self.g)
        changed['articles'][0]['url'] = 'https://example.test/other'
        self.tracking(changed)
        self.assertEqual(len(self.state), 2)
        self.assertEqual(self.state[group_key(self.g)]['group'], self.g)
        self.assertEqual(self.state[group_key(changed)]['decision_attempts'], 1)

    def test_many_skips_do_not_starve_tracking_slot(self):
        bad_groups = [group(article('容疑者を起訴', 'https://example.test/bad' + str(i))) for i in range(20)]
        for g in bad_groups:
            self.tracking(g)
        good = group(article('容疑者を起訴', 'https://example.test/good'))
        self.seed(analysis_prompt(self.events, good['articles'], incoming_event_name=good['event_name']), report(good['articles'][0]))
        ai = AI(limit=0)
        with patch.object(ai, 'ask', wraps=ai.ask) as calls:
            pending = merge_incoming(self.events, bad_groups + [good], ai, NOW, self.reviews, self.state)
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(pending, bad_groups)
        self.assertIn(good['articles'][0], self.events[0]['articles'])

    def status_run(self, answer=None):
        import ai_cost_control
        real_response = ai_cost_control.response
        def fake(prompt, *args, **kwargs):
            self.seed(prompt, self.bad if answer is None else answer)
            return real_response(prompt, allow_network=False)
        with patch('ai_cost_control.response', side_effect=fake) as calls:
            result = runpy.run_path(str(self.root / 'news_status.py'), run_name='__main__')
        return result, calls.call_count

    def test_status_repeats_and_changed_article(self):
        self.put('news_groups_deduplicated.json', [self.g])
        self.status_run()
        for _ in range(4):
            result, calls = self.status_run()
            self.assertEqual(calls, 0)
            self.assertEqual(result['attempted'], 0)
        item = json.loads(Path('status_triage.json').read_text())['items'][group_key(self.g)]
        self.assertEqual(item['decision_attempts'], 1)
        self.assertEqual(item['cached_rejection_skips'], 4)
        self.assertEqual(json.loads(Path('status_pending.json').read_text()), [self.g])
        changed = copy.deepcopy(self.g); changed['articles'][0]['description'] = '新根拠'
        self.put('news_groups_deduplicated.json', [changed])
        _, calls = self.status_run()
        self.assertEqual(calls, 1)
        item = json.loads(Path('status_triage.json').read_text())['items'][group_key(self.g)]
        self.assertEqual(item['source_history'][0]['group'], self.g)
        self.assertEqual(item['decision_attempts'], 1)

    def test_status_skip_preserves_limited_slot_for_fresh_article(self):
        self.put('news_groups_deduplicated.json', [self.g])
        self.status_run()
        good = group(article('地域の新発表', 'https://example.test/good'))
        self.put('news_groups_deduplicated.json', [self.g, good])
        with patch.dict(os.environ, {'STATUS_GROUP_LIMIT': '1'}):
            result, calls = self.status_run({'category': '政策・制度', 'tracking_value': 'high'})
        self.assertEqual(calls, 1)
        self.assertEqual(result['attempted'], 1)
        self.assertEqual(json.loads(Path('status_pending.json').read_text()), [self.g])
        self.assertEqual(json.loads(Path('news_status.json').read_text())[0]['articles'], good['articles'])

    def test_model_output_conditions_and_missing_signature_fail_closed(self):
        prompt = 'test'
        self.seed(prompt, self.bad)
        first = cached_answer(prompt)
        with patch.dict(os.environ, {'AI_MAX_OUTPUT_TOKENS': '1000'}):
            self.assertIsNone(cached_answer(prompt))
        self.assertIsNone(cached_answer(prompt, 'other-model'))
        self.assertIsNone(rejection_signature(first, 'tracking', {}, answer={'other': True}))
        self.assertIsNone(safe_signature(lambda p: (_ for _ in ()).throw(ValueError('bad cache')), prompt, 'tracking', {}, self.bad))

    def test_revision_reverting_content_does_not_reuse_old_rejection(self):
        self.tracking()
        changed = copy.deepcopy(self.g); changed['articles'][0]['description'] = '新根拠'
        self.tracking(changed)
        self.tracking()
        item = self.state[group_key(self.g)]
        self.assertEqual(len(item['source_history']), 2)
        self.assertEqual(len(item['rejected_responses']), 3)
        self.assertFalse(item.get('cached_rejection_waiting'))


if __name__ == '__main__':
    unittest.main()
