import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from event_tracker import enrich, lookup_event, merge_incoming
from tracking_sources import AI, BudgetExceeded
from merge_safety import UpdateHeld
from tracking_v1 import (apply_report, atomic_save, completion_verified, date_time, deduplicate,
                         merge_schedules, migrate, schedule_bounds, scheduled_search_due, update_lifecycle)

NOW = datetime(2026, 10, 8, 11, tzinfo=timezone.utc)


def article(title='札幌の事件 容疑者を逮捕', url='https://example.com/a', published='2026-10-07T10:00:00+00:00'):
    return {'title': '江別豊幌住宅強盗 被害者山田 ' + title, 'url': url, 'published': published}


def event():
    return migrate({'event_id': 'e1', 'event_name': '江別豊幌住宅強盗', 'category': '事件・事故', 'tracking_value': 'high',
                    'current_stage': '逮捕', 'summary': '容疑者を逮捕', 'updated_at': '2026-10-07T12:00:00+00:00',
                    'articles': [article()]}, NOW)


def report(a, **values):
    prior = article()
    anchors = [{'kind': kind, 'value': value, 'existing_quote': prior['title'], 'incoming_quote': a['title']}
               for kind, value in [('case', '江別豊幌住宅強盗'), ('entity', '被害者山田')]]
    return {'identity_matches': [{'existing_url': prior['url'], 'incoming_url': a['url'], 'anchors': anchors}],
            'progress_evidence': [{'url': a['url'], 'quote': a['title']}],
            'event_identity': {'value': '江別豊幌住宅強盗', 'url': a['url'], 'quote': a['title']},
            'relation': 'same_event', 'event_id': 'e1', 'confidence': 'high', 'evidence_urls': [a['url']],
            'meaningful_change': True, 'current_stage': '起訴', **values}


class FakeAI:
    def __init__(self, *answers):
        self.answers = list(answers)
    def ask(self, prompt):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class TrackingTests(unittest.TestCase):
    def test_same_reporting_does_not_advance(self):
        e = event(); a = article(url='https://example.com/b')
        apply_report(e, [a], report(a, meaningful_change=False), NOW)
        self.assertEqual(e['article_count'], 2)
        self.assertEqual(e['progress_count'], 0)
        self.assertEqual(e['current_stage'], '逮捕')
        self.assertEqual(e['updated_at'], '2026-10-07T12:00:00+00:00')

    def test_indictment_advances_once(self):
        e = event(); a = article('札幌の事件 容疑者を起訴', 'https://example.com/b', NOW.isoformat())
        apply_report(e, [a], report(a), NOW)
        apply_report(e, [a], report(a), NOW)
        self.assertEqual(e['progress_count'], 1)
        self.assertEqual(len(e['progress_history']), 1)
        self.assertEqual(e['current_stage'], '起訴')

    def test_backfill_never_rolls_back(self):
        e = event(); a = article('事件発生', 'https://example.com/old', '2020-01-01T00:00:00+00:00')
        apply_report(e, [a], report(a, current_stage='発生'), NOW)
        self.assertEqual(e['current_stage'], '逮捕')
        self.assertEqual(e['articles'][0]['url'], a['url'])
        self.assertEqual(e['progress_count'], 0)

    def test_untrusted_evidence_cannot_advance(self):
        e = event(); a = article(url='https://example.com/b', published=NOW.isoformat())
        with self.assertRaises(UpdateHeld):
            apply_report(e, [a], report(a, evidence_urls=['https://invented.invalid']), NOW)
        self.assertEqual(e['progress_count'], 0)

    def test_low_confidence_cannot_advance(self):
        e = event(); a = article(url='https://example.com/b', published=NOW.isoformat())
        with self.assertRaises(UpdateHeld):
            apply_report(e, [a], report(a, confidence='medium'), NOW)
        self.assertEqual(e['progress_count'], 0)

    def test_verdict_alone_not_completion(self):
        a = article('懲役10年の判決')
        self.assertFalse(completion_verified(report(a, completed=True, completion_quote=a['title']), [a]))

    def test_final_verdict_archived(self):
        e = event(); a = article('事件の判決が確定', 'https://example.com/final', NOW.isoformat())
        apply_report(e, [a], report(a, completed=True, completion_quote=a['title'], current_stage='判決'), NOW)
        self.assertEqual(e['lifecycle'], 'completed')
        self.assertEqual(e['article_count'], 2)

    def test_future_completion_rejected(self):
        for title in ['復旧完了の予定', '判決確定へ', '復旧は未完了', '復旧完了していない', '判決が確定する見込み']:
            a = article(title)
            self.assertFalse(completion_verified(report(a, completed=True, completion_quote=title), [a]), title)

    def test_partial_completion_quote_cannot_hide_future_tense(self):
        a = article('判決が確定する見込み')
        self.assertFalse(completion_verified(report(a, completed=True, completion_quote='判決が確定'), [a]))

    def test_fabricated_quote_rejected(self):
        a = article('判決が出た')
        self.assertFalse(completion_verified(report(a, completed=True, completion_quote='判決が確定'), [a]))

    def test_archived_event_reopens_on_progress(self):
        e = event(); e['lifecycle'] = 'completed'; e['completed_at'] = NOW.isoformat()
        a = article('再審開始を決定', 'https://example.com/reopen', NOW.isoformat())
        apply_report(e, [a], report(a, current_stage='再審'), NOW)
        self.assertEqual(e['lifecycle'], 'active')
        self.assertNotIn('completed_at', e)
        self.assertEqual(e['lifecycle_history'][0]['from'], 'completed')

    def test_reprints_do_not_reopen_archive(self):
        e = event(); e['lifecycle'] = 'completed'
        a = article(url='https://example.com/reprint', published=NOW.isoformat())
        apply_report(e, [a], report(a, meaningful_change=False), NOW)
        self.assertEqual(e['lifecycle'], 'completed')

    def test_dormancy_and_reactivation(self):
        e = event(); e['last_progress_at'] = (NOW-timedelta(days=181)).isoformat()
        update_lifecycle([e], NOW)
        self.assertEqual(e['lifecycle'], 'dormant')
        a = article('容疑者を起訴', url='https://example.com/new', published=NOW.isoformat())
        apply_report(e, [a], report(a), NOW)
        self.assertEqual(e['lifecycle'], 'active')

    def test_upcoming_schedule_prevents_dormancy(self):
        e = event(); e['last_progress_at'] = (NOW-timedelta(days=181)).isoformat()
        e['schedules'] = [{'status': 'scheduled', 'due_end': '2026-11-01'}]
        update_lifecycle([e], NOW)
        self.assertEqual(e['lifecycle'], 'active')

    def test_explicit_schedule_and_month_precision(self):
        self.assertEqual(schedule_bounds('2026年11月15日に初公判', '')[0], '2026-11-15')
        self.assertEqual(schedule_bounds('2026年11月から制度開始', ''), ('2026-11-01', '2026-11-30', 'month'))
        self.assertEqual(schedule_bounds('2033年開業予定', '')[2], 'year')
        self.assertEqual(schedule_bounds('来春開業', '')[2], 'unknown')
        self.assertEqual(schedule_bounds('11月に初公判', '')[2], 'unknown')
        self.assertEqual(schedule_bounds('2031年度着工', '')[2], 'unknown')

    def test_invalid_schedule_dates(self):
        self.assertEqual(schedule_bounds('2026年13月40日', '')[2], 'unknown')

    def test_current_notice_is_not_a_future_schedule(self):
        e = event(); a = article('札幌や旭川…１１地点注意報')
        merge_schedules(e, [{'label': 'インフル注意報', 'quote': a['title'], 'source_url': a['url']}], [a])
        self.assertEqual(e['schedules'], [])
        e['schedules'] = [{'label': 'インフル注意報', 'date_text': a['title'], 'status': 'scheduled'}]
        migrate(e, NOW)
        self.assertEqual(e['schedules'], [])

    def test_unresolved_future_plan_is_kept(self):
        e = event(); a = article('２０３１年度着工へ')
        merge_schedules(e, [{'label': '着工', 'quote': a['title'], 'source_url': a['url']}], [a])
        self.assertEqual(len(e['schedules']), 1)
        self.assertEqual(e['schedules'][0]['precision'], 'unknown')

    def test_schedules_require_actual_source_quote(self):
        e = event(); a = article('2026年11月15日 初公判予定')
        merge_schedules(e, [{'label': '初公判', 'quote': a['title'], 'source_url': a['url']}], [a])
        merge_schedules(e, [{'label': '架空予定', 'quote': '2027年1月1日', 'source_url': a['url']}], [a])
        self.assertEqual(len(e['schedules']), 1)
        self.assertEqual(e['schedules'][0]['due_start'], '2026-11-15')

    def test_near_deadline_search_is_bounded_and_never_auto_completes(self):
        e = event(); e['schedules'] = [{'status': 'scheduled', 'due_start': '2026-10-10', 'due_end': '2026-10-10'}]
        self.assertTrue(scheduled_search_due(e, NOW))
        e['last_schedule_search_at'] = NOW.isoformat()
        self.assertFalse(scheduled_search_due(e, NOW))
        self.assertTrue(scheduled_search_due(e, NOW+timedelta(days=4)))
        self.assertEqual(e['schedules'][0]['status'], 'scheduled')

    def test_search_backfill_accepts_only_high_confidence_older_candidates(self):
        e = event(); old = article('事件発生', 'https://example.com/old', '2025-01-01T00:00:00Z')
        other = article('別の事件', 'https://example.com/other', '2025-01-02T00:00:00Z')
        ai = FakeAI({**report(old, meaningful_change=False), 'matches': [{'url': old['url'], 'confidence': 'high'}, {'url': other['url'], 'confidence': 'low'}, {'url': 'https://invented.invalid', 'confidence': 'high'}]})
        lookup_event(e, ai, 'test', NOW, True, finder=lambda q: [old, other])
        self.assertEqual(e['article_count'], 2)
        self.assertEqual(e['current_stage'], '逮捕')

    def test_api_failure_keeps_pending_news(self):
        e = event(); a = article(url='https://example.com/new')
        group = {'category': '事件・事故', 'tracking_value': 'high', 'articles': [a]}
        pending = merge_incoming([e], [group], FakeAI(RuntimeError('429')), NOW)
        self.assertEqual(pending, [group])
        self.assertEqual(e['article_count'], 1)

    def test_uncertain_match_is_pending_not_duplicate_event(self):
        e = event(); a = article(url='https://example.com/new')
        groups = [{'category': '事件・事故', 'tracking_value': 'high', 'articles': [a]}]
        events = [e]
        self.assertEqual(merge_incoming(events, groups, FakeAI(report(a, confidence='low')), NOW), groups)
        self.assertEqual(len(events), 1)

    def test_new_event_is_preserved(self):
        a = article(url='https://example.com/new')
        group = {'event_name': '江別豊幌住宅強盗', 'category': '事件・事故', 'tracking_value': 'high', 'articles': [a]}
        events = []
        pending = merge_incoming(events, [group], FakeAI(report(a, relation='new_event', event_id='', current_stage='逮捕')), NOW)
        self.assertEqual(pending, [])
        self.assertEqual(events[0]['article_count'], 1)
        self.assertEqual(events[0]['progress_count'], 0)

    def test_url_validation(self):
        self.assertEqual(deduplicate([article(url='javascript:alert(1)'), article(), article()]), [article()])

    def test_rfc_date_parse_and_atomic_save(self):
        self.assertIsNotNone(date_time('Tue, 06 Oct 2026 09:02:14 GMT'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'events.json'
            atomic_save(path, [event()])
            self.assertEqual(json.loads(path.read_text())[0]['event_id'], 'e1')

    def test_budget_does_not_make_network_calls(self):
        with self.assertRaises(BudgetExceeded):
            AI(limit=0).ask('test')


if __name__ == '__main__':
    unittest.main()
