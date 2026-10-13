import copy
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import ai_cost_control as cost
from merge_safety import source_context
from pending_triage import group_key, summary
from queue_policy import triage_articles, retain_group, article_fingerprint
from test_tracking import NOW, article, event
from tracking_v1 import event_text
from schedule_evidence import apply_verified_dates
from tracking_v1 import migrate, scheduled_search_due


class PriorityQueueTests(unittest.TestCase):
    def test_legacy_collection_history_survives_changed_source(self):
        a = article()
        state = {a['url']: {'article': a, 'status': 'queued', 'reason': 'old',
                           'last_seen_at': NOW.isoformat(), 'source_fingerprint': article_fingerprint(a)}}
        changed = dict(a, title=a['title'] + ' 更新')
        self.assertEqual(triage_articles([changed], [], [], state, NOW), [changed])
        self.assertEqual(state[a['url']]['source_history'][0]['article'], a)

    def test_legacy_status_history_survives_changed_group(self):
        group = {'articles': [article()]}
        state = {}
        retain_group(state, group, 'retry_api', 'HTTPError', NOW)
        del state[group_key(group)]['source_history']
        changed = copy.deepcopy(group)
        changed['articles'][0]['description'] = '更新'
        retain_group(state, changed, 'queued', 'updated', NOW)
        self.assertEqual(state[group_key(group)]['source_history'][0]['group'], group)

    def test_summary_accounts_for_unobserved_and_duplicate_pending(self):
        group = {'articles': [article()]}
        other = {'articles': [article(url='https://example.test/unknown')]}
        state = {group_key(group): {'status': 'already_saved'}}
        metrics = summary(state, [group, group, other])
        self.assertEqual(sum(metrics['queue_counts'].values()), 3)
        self.assertEqual(metrics['queue_counts']['unobserved'], 1)
        self.assertEqual(metrics['processing_wait_count'], 1)
        self.assertEqual(metrics['archived_in_pending_count'], 2)

    def test_rss_heading_repeat_omitted_but_extra_facts_kept(self):
        a = article()
        a.update(title='固有事件 容疑者起訴 - 報道社',
                 description='<a href="https://example.test/a">固有事件 容疑者起訴</a> <font>報道社</font>')
        self.assertEqual(source_context(a)['description'], '')
        a['description'] += ' 新たな被害者を確認'
        self.assertIn('新たな被害者を確認', source_context(a)['description'])

    def test_prompt_compacts_reprints_without_changing_saved_sources(self):
        e = event()
        e['articles'] += [dict(e['articles'][0], url='https://example.test/reprint')]
        original = copy.deepcopy(e)
        self.assertEqual(len(event_text(e)['articles']), 1)
        self.assertEqual(e, original)

    def test_verified_dates_do_not_increment_progress_or_complete(self):
        import json
        snapshot = Path(__file__).resolve().parents[1] / 'verified_schedule_dates.json'
        records = json.loads(snapshot.read_text())
        es = [{'event_id': r['event_id'], 'articles': [article()],
               'progress_count': 0, 'progress_history': [], 'lifecycle': 'active',
               'schedules': [{'schedule_id': r['schedule_id'], 'date_text': r['expected_date_text'],
                   'precision': 'unknown', 'status': 'scheduled', 'source_url': 'https://example.test/original'}]}
              for r in records]
        apply_verified_dates(es, NOW, snapshot)
        apply_verified_dates(es, NOW, snapshot)
        for e, r in zip(es, records):
            self.assertEqual(e['schedules'][0]['due_start'], r['due_start'])
            self.assertEqual(e['progress_count'], 0)
            self.assertEqual(e['lifecycle'], 'active')
            self.assertEqual(len(e['schedules'][0]['history']), 1)
        self.assertFalse(scheduled_search_due(es[0], NOW))
        self.assertTrue(scheduled_search_due(es[0], NOW + timedelta(days=2)))

    def test_revised_or_cancelled_schedule_not_overwritten(self):
        import json
        snapshot = Path(__file__).resolve().parents[1] / 'verified_schedule_dates.json'
        record = json.loads(snapshot.read_text())[0]
        for status, text in [('cancelled', record['expected_date_text']), ('scheduled', '新たな設置予定')]:
            e = {'event_id': record['event_id'], 'schedules': [{'schedule_id': record['schedule_id'],
                'date_text': text, 'precision': 'unknown', 'status': status}]}
            old = copy.deepcopy(e)
            apply_verified_dates([e], NOW, snapshot)
            self.assertEqual(e, old)

    def test_fiscal_year_migration_preserves_quote_and_progress(self):
        e = event()
        e['schedules'] = [{'date_text': '２０３１年度着工へ', 'precision': 'unknown', 'status': 'scheduled'}]
        migrate(e, NOW)
        self.assertEqual(e['schedules'][0]['due_start'], '2031-04-01')
        self.assertEqual(e['schedules'][0]['due_end'], '2032-03-31')
        self.assertEqual(e['schedules'][0]['date_text'], '２０３１年度着工へ')
        self.assertEqual(e['progress_count'], 0)


class ProtectedBudgetTests(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.env = patch.dict(os.environ, {'AI_DAILY_USD_LIMIT': '0.20',
            'AI_MONTHLY_USD_LIMIT': '5.00', 'AI_DAILY_CALL_LIMIT': '40',
            'AI_RUN_CALL_LIMIT': '24', 'GITHUB_RUN_ID': 'priority-test',
            'AI_TRACKING_DAILY_RESERVED_USD': '0.10',
            'AI_TRACKING_DAILY_RESERVED_CALLS': '16',
            'AI_TRACKING_RUN_RESERVED_CALLS': '4', 'AI_PROCESSING_STAGE': 'upstream'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def seed(self, spend=95000, calls=1, run_calls=1):
        day, month = cost.periods(cost.now())
        cost.atomic('ai_budget.json', {'days': {day: {'reserved_micro_usd': spend, 'calls': calls}},
            'months': {month: {'reserved_micro_usd': spend, 'calls': calls}},
            'run': {'id': 'priority-test', 'calls': run_calls}})
        return day, month

    def test_upstream_cannot_take_tracking_money_but_tracking_can(self):
        day, _ = self.seed()
        with self.assertRaisesRegex(cost.BudgetExceeded, 'tracking_reserved_budget'):
            cost.reserve('next', cost.MODEL, 1200)
        with patch.dict(os.environ, {'AI_PROCESSING_STAGE': 'tracking'}):
            reservation = cost.reserve('next', cost.MODEL, 1200)
        self.assertEqual(cost.read('ai_budget.json', {})['days'][day]['tracking_calls'], 1)
        cost.settle(reservation, {'usage': {'input_tokens': 100, 'output_tokens': 20}})
        self.assertEqual(cost.read('ai_budget.json', {})['days'][day]['tracking_micro_usd'], 165)
        self.assertEqual(cost.read('ai_budget.json', {})['days'][day]['reserved_micro_usd'], 95165)

    def test_day_calls_are_protected(self):
        self.seed(spend=0, calls=24)
        with self.assertRaisesRegex(cost.BudgetExceeded, 'tracking_reserved_calls'):
            cost.reserve('next', cost.MODEL, 1200)
        with patch.dict(os.environ, {'AI_PROCESSING_STAGE': 'tracking'}):
            cost.reserve('next', cost.MODEL, 1200)

    def test_run_calls_are_protected(self):
        self.seed(spend=0, calls=20, run_calls=20)
        with self.assertRaisesRegex(cost.BudgetExceeded, 'tracking_reserved_calls'):
            cost.reserve('next', cost.MODEL, 1200)

    def test_tracking_cannot_exceed_existing_total_budget(self):
        self.seed(spend=198000)
        with patch.dict(os.environ, {'AI_PROCESSING_STAGE': 'tracking'}):
            with self.assertRaisesRegex(cost.BudgetExceeded, 'daily_budget'):
                cost.reserve('next', cost.MODEL, 1200)

    def test_legacy_accounting_not_reset(self):
        day, month = self.seed(spend=123456, calls=30)
        with self.assertRaises(cost.BudgetExceeded):
            cost.reserve('next', cost.MODEL, 1200)
        state = cost.read('ai_budget.json', {})
        self.assertEqual(state['days'][day]['reserved_micro_usd'], 123456)
        self.assertEqual(state['months'][month]['calls'], 30)
