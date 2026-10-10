import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import ai_cost_control as cost
from tracking_sources import AI
from event_tracker import merge_incoming, enrich
from test_tracking import NOW, FakeAI, article, event


class CostControlTests(unittest.TestCase):
    def setUp(self):
        self.before = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'test-secret',
            'AI_DAILY_USD_LIMIT': '0.20', 'AI_MONTHLY_USD_LIMIT': '5.00',
            'AI_DAILY_CALL_LIMIT': '40', 'AI_RUN_CALL_LIMIT': '24',
            'AI_MAX_OUTPUT_TOKENS': '1200', 'GITHUB_RUN_ID': '1',
            'AI_RESUME_AFTER_TOPUP': 'false'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.chdir(self.before)
        self.tmp.cleanup()

    def payload(self, usage=True):
        p = {'output': [{'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
        if usage:
            p['usage'] = {'input_tokens': 100, 'output_tokens': 20}
        return p

    def network(self, payload=None):
        return patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(json.dumps(payload or self.payload()).encode()))

    def test_cache_is_shared_across_clients_and_new_process_instances(self):
        with self.network() as calls:
            self.assertEqual(AI(limit=1).ask('same'), {'ok': True})
            self.assertEqual(AI(limit=0).ask('same'), {'ok': True})
            cost.response('same', allow_network=False)
            self.assertEqual(calls.call_count, 1)
            cost.response('changed')
            self.assertEqual(calls.call_count, 2)
        state = cost.read('ai_budget.json', {})
        self.assertEqual(next(iter(state['days'].values()))['reserved_micro_usd'], 330)
        self.assertNotIn('test-secret', Path('ai_response_cache.json').read_text())

    def test_unknown_usage_keeps_reservation_and_day_limit_prevents_network(self):
        with patch.dict(os.environ, {'AI_DAILY_USD_LIMIT': '0.008'}), self.network(self.payload(False)) as calls:
            cost.response('first')
            with self.assertRaisesRegex(cost.BudgetExceeded, 'daily'):
                cost.response('second')
            self.assertEqual(calls.call_count, 1)

    def test_run_and_day_call_limits_are_shared_across_stages(self):
        with patch.dict(os.environ, {'AI_RUN_CALL_LIMIT': '1', 'AI_DAILY_CALL_LIMIT': '2'}), self.network() as calls:
            AI().ask('one')
            with self.assertRaises(cost.BudgetExceeded):
                cost.response('two')
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '2'}):
                cost.response('two')
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '3'}):
                with self.assertRaises(cost.BudgetExceeded):
                    cost.response('three')
            self.assertEqual(calls.call_count, 2)

    def test_monthly_budget_survives_day_rollover_jst(self):
        at = datetime(2026, 10, 10, 14, 59, tzinfo=timezone.utc)
        with patch.dict(os.environ, {'AI_MONTHLY_USD_LIMIT': '0.008'}), self.network(self.payload(False)) as calls:
            with patch.object(cost, 'now', return_value=at):
                cost.response('one')
            with patch.object(cost, 'now', return_value=at + timedelta(minutes=2)):
                with self.assertRaisesRegex(cost.BudgetExceeded, 'monthly'):
                    cost.response('two')
            self.assertEqual(calls.call_count, 1)

    def test_billing_failure_stops_all_following_network_calls_and_future_runs(self):
        error = HTTPError('https://api.openai.com/v1/responses', 429, 'quota', {},
                          io.BytesIO(b'{"error":{"code":"insufficient_quota"}}'))
        with patch('urllib.request.urlopen', side_effect=error) as calls:
            with self.assertRaises(HTTPError):
                AI().ask('one')
            with self.assertRaisesRegex(cost.BudgetExceeded, 'billing_blocked'):
                cost.response('two')
            with patch.dict(os.environ, {'GITHUB_RUN_ID': 'new'}):
                self.assertFalse(cost.should_run())
                with self.assertRaises(cost.BudgetExceeded):
                    AI().ask('three')
            self.assertEqual(calls.call_count, 1)
        with patch.dict(os.environ, {'AI_RESUME_AFTER_TOPUP': 'true'}):
            self.assertTrue(cost.should_run())
        self.assertEqual(next(iter(cost.read('ai_budget.json', {})['days'].values()))['calls'], 1)

    def test_rate_limit_stops_current_run_but_not_next_run(self):
        error = HTTPError('https://api.openai.com/v1/responses', 429, 'rate', {},
                          io.BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}'))
        with patch('urllib.request.urlopen', side_effect=error) as calls:
            with self.assertRaises(HTTPError):
                cost.response('one')
            with self.assertRaises(cost.BudgetExceeded):
                cost.response('two')
            self.assertEqual(calls.call_count, 1)
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '2'}), self.network() as calls:
            cost.response('two')
            self.assertEqual(calls.call_count, 1)

    def test_six_hour_interval_does_not_clear_spend(self):
        at = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        with patch.object(cost, 'now', return_value=at):
            self.assertTrue(cost.should_run())
        with patch.object(cost, 'now', return_value=at + timedelta(hours=5, minutes=59)):
            self.assertFalse(cost.should_run())
        with patch.object(cost, 'now', return_value=at + timedelta(hours=6)):
            self.assertTrue(cost.should_run())

    def test_invalid_cache_and_accounting_fail_closed(self):
        Path('ai_budget.json').write_text('{bad')
        with self.network() as calls:
            with self.assertRaises(json.JSONDecodeError):
                cost.response('one')
            self.assertEqual(calls.call_count, 0)

    def test_unpriced_model_is_not_sent(self):
        with self.network() as calls:
            with self.assertRaises(cost.BudgetExceeded):
                cost.response('one', model='gpt-unknown')
            self.assertEqual(calls.call_count, 0)

    def test_invalid_or_truncated_responses_are_not_cached(self):
        incomplete = self.payload(); incomplete['status'] = 'incomplete'
        with self.network(incomplete):
            with self.assertRaises(ValueError):
                cost.response('one')
        self.assertFalse(Path('ai_response_cache.json').exists())

    def test_deferred_articles_preserved_without_evidence_attempts(self):
        groups = [{'category': '事件・事故', 'tracking_value': 'high',
                   'articles': [article(url='https://example.test/' + str(i))]} for i in range(3)]
        triage = {}
        pending = merge_incoming([], groups, FakeAI(cost.BudgetExceeded('daily_budget_deferred')), NOW, triage=triage)
        self.assertEqual(pending, groups)
        self.assertTrue(all(r['decision_attempts'] == 0 for r in triage.values()))
        e = event(); e['v1_enriched'] = True
        enrich([e], FakeAI(cost.BudgetExceeded('daily_budget_deferred')), NOW)
        self.assertEqual(e['backfill'].get('attempts', 0), 0)

    def test_hourly_collection_retains_articles_between_ai_runs(self):
        import runpy
        from unittest.mock import patch
        root = Path(__file__).resolve().parents[1]
        for suffix in ('old', 'new'):
            Path('sapporo_news.xml').write_text('<rss><channel><item><title>案件 ' + suffix + '</title><link>https://example.test/' + suffix + '</link><pubDate>Fri, 10 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>')
            with patch('sys.argv', ['source_filter.py']):
                runpy.run_path(str(root / 'source_filter.py'), run_name='__main__')
        self.assertEqual(len(cost.read('news_inbox.json', [])), 2)
        self.assertEqual(len(cost.read('news_filtered.json', [])), 2)

    def test_pair_cache_allows_next_uncached_pair_to_progress(self):
        import runpy
        root = Path(__file__).resolve().parents[1]
        pairs = [{'article_a': article(url='https://example.test/a' + str(i)),
                  'article_b': article(url='https://example.test/b' + str(i))} for i in range(2)]
        Path('ai_candidates.json').write_text(json.dumps(pairs))
        with patch.dict(os.environ, {'PAIR_CALL_LIMIT': '1'}), self.network() as calls:
            runpy.run_path(str(root / 'news_pairing.py'), run_name='__main__')
            self.assertEqual(calls.call_count, 1)
            runpy.run_path(str(root / 'news_pairing.py'), run_name='__main__')
            self.assertEqual(calls.call_count, 2)

    def test_status_keeps_previous_summary_when_budget_is_zero(self):
        import runpy
        root = Path(__file__).resolve().parents[1]
        old = {'event_name': 'old', 'articles': [article(url='https://example.test/old')], 'summary': 'saved'}
        new = {'event_name': 'new', 'articles': [article(url='https://example.test/new')]}
        Path('news_status.json').write_text(json.dumps([old]))
        Path('news_groups_deduplicated.json').write_text(json.dumps([new]))
        with patch.dict(os.environ, {'AI_DAILY_USD_LIMIT': '0'}), self.network() as calls:
            runpy.run_path(str(root / 'news_status.py'), run_name='__main__')
            self.assertEqual(calls.call_count, 0)
        self.assertEqual(cost.read('news_status.json', []), [old])
        self.assertEqual(cost.read('status_pending.json', []), [new])
