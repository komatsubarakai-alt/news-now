import copy
import io
import json
import os
import runpy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from queue_policy import obvious_out_of_scope, triage_articles, prioritize_groups, retain_group, pause_reason, article_fingerprint
from pending_triage import group_key, fingerprint
from queue_monitor import build_health
from test_tracking import NOW

def article(title="地域の発表", url="https://example.test/a", published="2026-10-07T10:00:00+00:00"):
    return {"title": title, "url": url, "published": published}
from test_merge_safety import group


class QueuePolicyTests(unittest.TestCase):
    def test_clear_sports_and_food_are_free_triage(self):
        self.assertEqual(obvious_out_of_scope(article('コンサドーレ きょうアウェー磐田戦')), 'sports_result_or_fixture')
        self.assertEqual(obvious_out_of_scope(article('高校野球 秋季全道２回戦 適時三塁打')), 'sports_result_or_fixture')
        self.assertEqual(obvious_out_of_scope(article('札幌スイーツ 新メニュー')), 'entertainment_or_food_announcement')

    def test_safety_and_policy_veto_sports_filter(self):
        for headline in ['ファイターズ選手を逮捕', '音楽フェスで火災', 'サッカーチーム破産',
                         'スポーツ施設の条例改正', 'グルメ店で食中毒 死亡', 'ライブ会場の建設計画',
                         'ファイターズ球場で事故', 'コンサート会場の閉鎖']:
            self.assertIsNone(obvious_out_of_scope(article(headline)), headline)

    def test_ambiguous_news_is_not_removed(self):
        for title in ['札幌で重要な動き', 'スポーツ企業の発表', '高校生の地域活動', '地域イベントに関する調査']:
            self.assertIsNone(obvious_out_of_scope(article(title)))

    def test_archive_preserves_sources_and_updated_sports_reopens(self):
        a = article('高校野球 ２回戦勝利', 'https://example.test/new')
        state = {}
        self.assertEqual(triage_articles([a], [], [], state, NOW), [])
        self.assertEqual(state[a['url']]['article'], a)
        changed = copy.deepcopy(a); changed['title'] = '高校野球の選手を逮捕'
        self.assertEqual(triage_articles([changed], [], [], state, NOW), [changed])
        self.assertEqual(state[a['url']]['source_history'][0]['article'], a)

    def test_saved_or_classified_unchanged_source_does_not_use_upstream_ai(self):
        a = article('容疑者を起訴', 'https://example.test/new')
        state = {}
        self.assertEqual(triage_articles([a], [{'articles': [a]}], [], state, NOW), [])
        self.assertEqual(state[a['url']]['status'], 'already_saved')
        status = group(a); status['category'] = '事件・事故'
        self.assertEqual(triage_articles([a], [], [status], state, NOW), [])
        self.assertEqual(state[a['url']]['status'], 'classified_waiting')
        changed = copy.deepcopy(a); changed['description'] = '新たな根拠'
        self.assertEqual(triage_articles([changed], [], [status], state, NOW), [changed])

    def test_priority_has_fairness_slot_for_oldest(self):
        ordinary = group(article('地域の発表', 'https://example.test/old'))
        urgent = [group(article('容疑者を起訴', 'https://example.test/' + str(i))) for i in range(5)]
        state = {group_key(ordinary): {'first_seen_at': (NOW - timedelta(hours=12)).isoformat()}}
        ordered = prioritize_groups([ordinary] + urgent, state, NOW)
        self.assertEqual(ordered[0], urgent[0])
        self.assertEqual(ordered[3], ordinary)
        self.assertCountEqual([group_key(g) for g in ordered], [group_key(g) for g in [ordinary] + urgent])

    def test_new_sources_rank_above_same_evidence_retry(self):
        old = group(article('地域の発表', 'https://example.test/old'))
        new = group(article('地域の発表', 'https://example.test/new'))
        state = {group_key(old): {'status': 'needs_evidence', 'source_fingerprint': fingerprint(old)}}
        self.assertEqual(prioritize_groups([old, new], state, NOW), [new, old])

    def test_repeated_format_failures_pause_but_api_failures_do_not(self):
        g = group(article())
        state = {}
        for _ in range(4):
            retain_group(state, g, 'retry_api', 'HTTPError', NOW)
        self.assertEqual(state[group_key(g)]['decision_attempts'], 0)
        for _ in range(3):
            retain_group(state, g, 'retry_format', 'JSONDecodeError', NOW, decision=True)
        self.assertEqual(pause_reason(g, state), 'manual_review')
        changed = copy.deepcopy(g); changed['articles'][0]['description'] = 'new'
        self.assertIsNone(pause_reason(changed, state))
        retain_group(state, changed, 'retry_format', 'JSONDecodeError', NOW, decision=True)
        self.assertEqual(state[group_key(g)]['decision_attempts'], 1)
        self.assertEqual(state[group_key(g)]['source_history'][0]['group'], g)


class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.before = os.getcwd(); self.tmp = tempfile.TemporaryDirectory(); os.chdir(self.tmp.name)
        self.root = Path(__file__).resolve().parents[1]
    def tearDown(self):
        os.chdir(self.before); self.tmp.cleanup()
    def put(self, path, value):
        Path(path).write_text(json.dumps(value))

    def test_status_pauses_invalid_output_after_three_without_losing_sources(self):
        g = group(article('容疑者を起訴', 'https://example.test/new'))
        self.put('news_groups_deduplicated.json', [g])
        with patch('ai_cost_control.response', side_effect=ValueError('invalid result')) as calls:
            for _ in range(4):
                runpy.run_path(str(self.root / 'news_status.py'), run_name='__main__')
            self.assertEqual(calls.call_count, 3)
        state = json.loads(Path('status_triage.json').read_text())['items'][group_key(g)]
        self.assertEqual(state['status'], 'manual_review')
        self.assertEqual(state['group'], g)
        self.assertEqual(json.loads(Path('status_pending.json').read_text()), [])

    def test_monitor_runs_while_ai_paused_and_deduplicates_stages(self):
        a = article('容疑者を起訴', 'https://example.test/new')
        g = group(a)
        state = {a['url']: {'article': a, 'status': 'queued', 'first_seen_at': (NOW - timedelta(hours=60)).isoformat()}}
        self.put('news_inbox.json', [a]); self.put('collection_triage.json', {'items': state})
        self.put('status_pending.json', [g]); self.put('tracking_pending.json', [g])
        self.put('ai_budget.json', {'billing_blocked': {'code': 'insufficient_quota'}})
        with patch('urllib.request.urlopen', side_effect=AssertionError('monitor must be free')):
            health = build_health(NOW)
        self.assertEqual(health['waiting_article_count'], 1)
        self.assertEqual(health['oldest_wait_hours'], 60)
        self.assertEqual(health['ai_pause_reason'], 'insufficient_quota')
        self.assertIn('oldest_wait_at_least_48_hours', health['alerts'])
        self.assertEqual(health['stages']['tracking']['group_count'], 1)

    def test_monitor_separates_archived_and_manual_review(self):
        sport = article('高校野球 ２回戦', 'https://example.test/sport')
        paused = group(article('地域発表', 'https://example.test/paused'))
        self.put('news_inbox.json', [sport]); self.put('collection_triage.json', {'items': {sport['url']: {'status': 'out_of_scope'}}})
        self.put('status_pending.json', [paused])
        state = {group_key(paused): {'status': 'manual_review', 'source_fingerprint': fingerprint(paused)}}
        self.put('status_triage.json', {'items': state})
        health = build_health(NOW)
        self.assertEqual(health['waiting_article_count'], 0)
        self.assertEqual(health['manual_review_counts']['status_generation'], 1)

    def test_monitor_detects_consecutive_growth(self):
        for i in range(4):
            self.put('news_inbox.json', [article(url='https://example.test/' + str(j)) for j in range(i+1)])
            health = build_health(NOW + timedelta(hours=i))
        self.assertIn('waiting_increased_three_observations', health['alerts'])
