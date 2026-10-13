"""Queue ownership never substitutes for event identity or modifies source snapshots."""
import copy
import hashlib
import io
import json
import os
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from ai_cost_control import BudgetExceeded
from pending_triage import fingerprint, group_key
from queue_policy import (article_fingerprint, downstream_article_index, retain_group,
                          tracking_classification_index, triage_articles)
from test_tracking import NOW

ROOT = Path(__file__).resolve().parents[1]


def article():
    return {'url': 'https://example.test/a', 'title': '豊幌住宅強盗で容疑者を起訴',
            'description': '被害者と事件の根拠', 'published': '2026-10-07T10:00:00+00:00'}


def group(*articles, classified=False):
    result = {'event_name': '豊幌住宅強盗', 'articles': list(articles)}
    if classified:
        result.update(category='事件・事故', tracking_value='high', current_stage='起訴',
                      summary='確認済みの分類', latest='起訴', next_watch='公判')
    return result


class CrossStagePolicyTests(unittest.TestCase):
    def triage(self, a, state=None, **queues):
        return triage_articles([a], [], [], state if state is not None else {}, NOW, **queues)

    def test_status_pending_owns_exact_revision_and_keeps_reference(self):
        a = article(); g = group(a); state = {}; before = copy.deepcopy(g)
        self.assertEqual(self.triage(a, state, status_pending=[g]), [])
        record = state[a['url']]
        self.assertEqual(record['article'], a)
        self.assertEqual(record['status'], 'downstream_waiting')
        self.assertEqual(record['downstream_waiting'], [{'stage': 'status_generation',
            'group_key': group_key(g), 'source_fingerprint': fingerprint(g)}])
        self.assertEqual(g, before)

    def test_tracking_pending_owns_only_valid_classification(self):
        a = article()
        for category, value in [('事件・事故', 'high'), ('政策・制度', 'medium'), ('対象外', 'low')]:
            with self.subTest(category=category, value=value):
                g = group(a, classified=True); g.update(category=category, tracking_value=value)
                self.assertEqual(self.triage(a, tracking_pending=[g]), [])
        for changes in [{'category': 'unknown'}, {'tracking_value': 'unknown'},
                        {'category': []}, {'tracking_value': []}, {'event_name': ''}]:
            with self.subTest(changes=changes):
                g = group(a, classified=True); g.update(changes)
                self.assertEqual(self.triage(a, tracking_pending=[g]), [a])
        self.assertEqual(self.triage(a, tracking_pending=[group(a)]), [a])

    def test_each_identity_field_change_reopens_and_preserves_old_link(self):
        a = article(); g = group(a)
        for field, changed in [('url', 'https://example.test/b'), ('title', '北区の別事件で逮捕'),
                               ('description', '新しい根拠'), ('published', '2026-10-08T10:00:00+00:00')]:
            with self.subTest(field=field):
                state = {}; self.triage(a, state, status_pending=[g])
                revised = {**a, field: changed}
                self.assertEqual(self.triage(revised, state, status_pending=[g]), [revised])
                record = state[revised['url']]
                self.assertNotIn('downstream_waiting', record)
                if field != 'url':
                    self.assertEqual(record['source_history'][0]['article'], a)
                    self.assertEqual(record['source_history'][0]['downstream_waiting'][0]['group_key'], group_key(g))
                else:
                    self.assertEqual(state[a['url']]['article'], a)

    def test_missing_downstream_queue_releases_article_without_sticky_hold(self):
        a = article(); state = {}
        self.triage(a, state, status_pending=[group(a)])
        self.assertEqual(self.triage(a, state), [a])
        self.assertNotIn('downstream_waiting', state[a['url']])
        self.assertEqual(state[a['url']]['downstream_history'][0]['references'][0]['stage'], 'status_generation')

    def test_repeated_same_destination_does_not_duplicate_history(self):
        a = article(); state = {}; g = group(a)
        for _ in range(3):
            self.triage(a, state, status_pending=[g])
        self.assertNotIn('downstream_history', state[a['url']])
        classified = group(a, classified=True); references = [{'stage': 'tracking', 'group_key': group_key(g)}]
        state = {}
        for _ in range(3):
            retain_group(state, classified, 'tracking_waiting', 'unchanged_tracking_source', NOW,
                         downstream_waiting=references)
        self.assertNotIn('downstream_history', state[group_key(classified)])

    def test_multiple_destinations_preserved_without_duplicate_links(self):
        a = article(); first = group(a); second = group(a, classified=True)
        links = downstream_article_index([first, first], [second])[article_fingerprint(a)]
        self.assertEqual([r['stage'] for r in links], ['status_generation', 'tracking'])

    def test_group_match_requires_whole_revision_name_and_unambiguous_labels(self):
        a = article(); b = {**a, 'url': 'https://example.test/b'}
        g = group(a, b, classified=True)
        index = tracking_classification_index([g])
        self.assertNotIn((g['event_name'], fingerprint(group(a))), index)
        self.assertNotIn(('別事件', fingerprint(g)), index)
        reordered = group(b, a)
        self.assertIn((g['event_name'], fingerprint(reordered)), index)
        contradictory = {**g, 'category': '災害'}
        self.assertEqual(tracking_classification_index([g, contradictory]), {})

    def test_malformed_sources_do_not_suppress_collection(self):
        a = article()
        for bad in [None, {'articles': None}, group({**a, 'description': None}),
                    group({**a, 'url': 'javascript:alert(1)'}), group({**a, 'title': ''})]:
            with self.subTest(group=bad):
                self.assertEqual(self.triage(a, status_pending=[bad]), [a])

    def test_status_revision_history_keeps_prior_destination(self):
        a = article(); g = group(a); state = {}
        record = retain_group(state, g, 'tracking_waiting', 'unchanged_tracking_source', NOW)
        record['downstream_waiting'] = [{'stage': 'tracking', 'group_key': group_key(g)}]
        changed = group({**a, 'published': '2026-10-09T10:00:00+00:00'})
        updated = retain_group(state, changed, 'queued', 'budget_deferred', NOW + timedelta(hours=1))
        self.assertNotIn('downstream_waiting', updated)
        self.assertEqual(updated['source_history'][0]['group'], g)
        self.assertEqual(updated['source_history'][0]['downstream_waiting'][0]['stage'], 'tracking')


class CrossStageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.before = os.getcwd(); self.tmp = tempfile.TemporaryDirectory(); os.chdir(self.tmp.name)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Real network prohibited'))
        self.network.start()

    def tearDown(self):
        self.network.stop(); os.chdir(self.before); self.tmp.cleanup()

    def put(self, name, data):
        Path(name).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def run_status(self, incoming, waiting):
        self.put('news_groups_deduplicated.json', incoming)
        self.put('status_pending.json', incoming)
        self.put('tracking_pending.json', waiting)
        tracking_before = Path('tracking_pending.json').read_bytes()
        with patch('ai_cost_control.response', side_effect=BudgetExceeded('daily_budget_deferred')) as calls, redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'news_status.py'), run_name='__main__')
        self.assertEqual(Path('tracking_pending.json').read_bytes(), tracking_before)
        return calls, json.loads(Path('status_pending.json').read_text())

    def test_tracking_owned_group_avoids_ai_and_archives_source_with_link(self):
        a = article(); incoming = group(a); waiting = group(a, classified=True)
        calls, pending = self.run_status([incoming], [waiting])
        calls.assert_not_called(); self.assertEqual(pending, [])
        record = json.loads(Path('status_triage.json').read_text())['items'][group_key(incoming)]
        self.assertEqual(record['group'], incoming)
        self.assertEqual(record['status'], 'tracking_waiting')
        self.assertEqual(record['downstream_waiting'][0]['source_fingerprint'], fingerprint(waiting))

    def test_every_changed_source_field_requires_status_processing(self):
        a = article()
        for field, value in [('url', 'https://example.test/b'), ('title', '別事件で逮捕'),
                             ('description', '追加事実'), ('published', '2026-10-08T10:00:00+00:00')]:
            with self.subTest(field=field):
                changed = group({**a, field: value})
                calls, pending = self.run_status([changed], [group(a, classified=True)])
                self.assertEqual(calls.call_count, 1); self.assertEqual(pending, [changed])

    def test_invalid_or_conflicting_classification_is_not_reused(self):
        a = article(); incoming = group(a)
        for waiting in [[group(a)], [dict(group(a, classified=True), category='unknown')],
                        [dict(group(a, classified=True), tracking_value='unknown')],
                        [group(a, classified=True), dict(group(a, classified=True), category='災害')]]:
            with self.subTest(waiting=waiting):
                calls, pending = self.run_status([incoming], waiting)
                self.assertEqual(calls.call_count, 1); self.assertEqual(pending, [incoming])

    def test_partial_group_and_different_name_are_not_reused(self):
        a = article(); b = {**a, 'url': 'https://example.test/b'}
        for incoming in [group(a), dict(group(a, b), event_name='別事件')]:
            with self.subTest(group=incoming):
                calls, pending = self.run_status([incoming], [group(a, b, classified=True)])
                self.assertEqual(calls.call_count, 1); self.assertEqual(pending, [incoming])

    def test_duplicate_does_not_consume_status_slot_before_fresh_group(self):
        a = article(); fresh = group({**a, 'url': 'https://example.test/new'})
        self.put('news_groups_deduplicated.json', [group(a), fresh])
        self.put('tracking_pending.json', [group(a, classified=True)])
        with patch.dict(os.environ, {'STATUS_GROUP_LIMIT': '1'}), patch('ai_cost_control.response', side_effect=BudgetExceeded('daily_budget_deferred')) as calls, redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'news_status.py'), run_name='__main__')
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(json.loads(Path('status_pending.json').read_text()), [fresh])

    def test_source_filter_keeps_inbox_pending_and_changed_source_history(self):
        from source_filter import main
        a = article(); self.put('news_inbox.json', [a]); self.put('status_pending.json', [group(a)])
        Path('feed.xml').write_text('<rss><channel/></rss>')
        original_pending = Path('status_pending.json').read_bytes()
        with patch('sys.argv', ['source_filter.py', '--rss', 'feed.xml']), redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(json.loads(Path('news_filtered.json').read_text()), [])
        self.assertEqual(json.loads(Path('news_inbox.json').read_text()), [a])
        self.assertEqual(Path('status_pending.json').read_bytes(), original_pending)
        changed = {**a, 'published': '2026-10-08T10:00:00+00:00'}
        self.put('news_inbox.json', [changed])
        with patch('sys.argv', ['source_filter.py', '--rss', 'feed.xml']), redirect_stdout(io.StringIO()):
            main()
        self.assertEqual(json.loads(Path('news_filtered.json').read_text())[0]['published'], changed['published'])
        record = json.loads(Path('collection_triage.json').read_text())['items'][a['url']]
        self.assertEqual(record['source_history'][0]['article']['published'], a['published'])

    def test_read_only_snapshot_hashes_and_real_duplicate_candidates(self):
        hashes = lambda: {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in ROOT.rglob('*.json')}
        before = hashes()
        read = lambda n: json.loads((ROOT / n).read_text())
        sources = read('news_filtered.json'); pending = read('status_pending.json')
        tracking = read('tracking_pending.json'); state = copy.deepcopy(read('collection_triage.json')['items'])
        kept = triage_articles(sources, read('events.json'), read('news_status.json'), state, NOW,
                               status_pending=pending, tracking_pending=tracking)
        parked = [a for a in sources if state[a['url']]['status'] == 'downstream_waiting']
        index = tracking_classification_index(tracking)
        duplicate_groups = [g for g in pending if (g.get('event_name'), fingerprint(g)) in index]
        self.assertEqual(len(kept) + len(parked), len(sources))
        for a in parked:
            self.assertIn(article_fingerprint(a), downstream_article_index(pending, tracking))
        self.assertEqual(hashes(), before)
        # Work on temporary copies, leaving all downloaded JSON read-only.
        self.put('news_groups_deduplicated.json', [])
        self.put('status_pending.json', pending); self.put('tracking_pending.json', tracking)
        self.put('status_triage.json', read('status_triage.json'))
        self.put('events.json', read('events.json')); self.put('news_status.json', read('news_status.json'))
        with patch.dict(os.environ, {'STATUS_GROUP_LIMIT': '0'}), patch('ai_cost_control.response', side_effect=AssertionError('No API allowed')) as calls, redirect_stdout(io.StringIO()):
            runpy.run_path(str(ROOT / 'news_status.py'), run_name='__main__')
        calls.assert_not_called()
        remaining = json.loads(Path('status_pending.json').read_text())
        self.assertEqual(len(remaining), len(pending) - len(duplicate_groups))
        self.assertEqual(json.loads(Path('tracking_pending.json').read_text()), tracking)
        self.assertEqual(hashes(), before)
