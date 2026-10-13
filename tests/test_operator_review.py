import copy
import json
import tempfile
import unittest
import subprocess
import shutil
from pathlib import Path
from unittest.mock import patch

from event_tracker import merge_incoming
from operator_review import (build_snapshot, digest, target_digest, validate_decisions,
                             import_decisions, prepare_reviews, render_page)
from pending_triage import observe, group_key, fingerprint, preflight, summary
from test_tracking import NOW, article, event, report, FakeAI


class OperatorReviewTests(unittest.TestCase):
    def setUp(self):
        self.events = [event()]
        self.a = article('容疑者を起訴', 'https://example.com/new', NOW.isoformat())
        self.group = {'event_name': '江別豊幌住宅強盗', 'category': '事件・事故',
                      'tracking_value': 'high', 'articles': [self.a]}
        self.state = {}
        observe(self.state, self.group, 'manual_review', 'article_identity_unproved', NOW)
        self.key = group_key(self.group)

    def document(self, action='same_event'):
        row = {'review_key': self.key, 'source_fingerprint': fingerprint(self.group),
               'group_hash': digest(self.group), 'action': action,
               'event_id': 'e1' if action == 'same_event' else None,
               'target_hash': target_digest(self.events[0]) if action == 'same_event' else None,
               'note': '元記事の事件名と被害者を確認'}
        return {'schema_version': 1, 'repository': 'komatsubarakai-alt/news-now', 'decisions': [row]}

    def prepare(self, action='same_event', groups=None):
        doc = self.document(action)
        doc['decisions'] = validate_decisions(doc, self.events, self.state, {})
        result = prepare_reviews(self.events, groups or [], self.state, doc, {}, NOW)
        return doc, result

    def test_route_to_existing_preserves_progress_evidence_and_repeat_is_idempotent(self):
        doc, groups = self.prepare()
        self.assertEqual(groups, [self.group])
        with patch('event_tracker.load_corrections', return_value={}):
            self.assertEqual(merge_incoming(self.events, groups, FakeAI(report(self.a)), NOW, triage=self.state), [])
        self.assertEqual(self.events[0]['progress_count'], 1)
        self.assertEqual(prepare_reviews(self.events, [], self.state, doc, {}, NOW), [])
        self.assertEqual(self.state[self.key]['operator_review']['decision_id'], doc['decisions'][0]['decision_id'])

    def test_operator_choice_does_not_bypass_identity(self):
        _, groups = self.prepare()
        r = report(self.a, identity_matches=[])
        original = copy.deepcopy(self.events)
        with patch('event_tracker.load_corrections', return_value={}):
            merge_incoming(self.events, groups, FakeAI(r), NOW, triage=self.state)
        self.assertEqual(self.events, original)
        self.assertEqual(self.state[self.key]['status'], 'needs_evidence')

    def test_report_cannot_route_to_other_event_or_create_new_one(self):
        for r in [report(self.a, event_id='other'), report(self.a, relation='new_event')]:
            self.setUp()
            _, groups = self.prepare()
            with patch('event_tracker.load_corrections', return_value={}):
                merge_incoming(self.events, groups, FakeAI(r), NOW, triage=self.state)
            self.assertEqual(len(self.events), 1)
            self.assertEqual(self.events[0]['progress_count'], 0)
            self.assertEqual(self.state[self.key]['reason'], 'operator_selection_not_supported')

    def test_exclude_retains_group_and_audit_without_api(self):
        original = copy.deepcopy(self.events)
        doc, groups = self.prepare('exclude')
        self.assertEqual(groups, [])
        self.assertEqual(preflight(self.group, {}, {}, self.state), 'operator_excluded')
        with patch('event_tracker.load_corrections', return_value={}):
            self.assertEqual(merge_incoming(self.events, [self.group], FakeAI(), NOW, triage=self.state), [])
        self.assertEqual(self.state[self.key]['group'], self.group)
        self.assertEqual(self.state[self.key]['operator_review']['before']['reason'], 'article_identity_unproved')
        self.assertEqual(self.state[self.key]['operator_review']['before']['status'], 'manual_review')
        self.assertEqual(self.events, original)
        self.assertEqual(summary(self.state, [self.group])['processing_wait_count'], 0)
        changed = copy.deepcopy(self.group)
        changed['articles'][0]['title'] += ' 新事実'
        self.assertIsNone(preflight(changed, {}, {}, self.state))
        self.assertEqual(prepare_reviews(self.events, [], self.state, doc, {}, NOW), [])

    def test_retries_do_not_reset_attempt_count_or_circumvent_manual_hold(self):
        doc, groups = self.prepare()
        with patch('event_tracker.load_corrections', return_value={}):
            for _ in range(3):
                merge_incoming(self.events, groups, FakeAI(report(self.a, confidence='low')), NOW, triage=self.state)
                groups = prepare_reviews(self.events, [], self.state, doc, {}, NOW)
        self.assertEqual(self.state[self.key]['status'], 'manual_review')
        self.assertEqual(self.state[self.key]['decision_attempts'], 3)
        self.assertEqual(groups, [])

    def test_source_changes_after_snapshot_are_rejected(self):
        doc = self.document()
        self.state[self.key]['group']['articles'][0]['title'] += ' 更新'
        with self.assertRaisesRegex(ValueError, '記事が更新'):
            validate_decisions(doc, self.events, self.state, {})

    def test_new_incoming_revision_is_never_overwritten_by_old_review(self):
        doc = self.document()
        doc['decisions'] = validate_decisions(doc, self.events, self.state, {})
        changed = copy.deepcopy(self.group)
        changed['articles'][0]['title'] += ' 最新情報'
        groups = prepare_reviews(self.events, [changed], self.state, doc, {}, NOW)
        self.assertEqual(groups, [changed])
        self.assertEqual(self.state[self.key]['status'], 'manual_review')
        self.assertIn('operator_review_rejection', self.state[self.key])

    def test_target_changes_or_editorial_hold_are_rejected(self):
        doc = self.document()
        self.events[0]['articles'][0]['title'] += ' 訂正'
        with self.assertRaisesRegex(ValueError, '既存イベントが更新'):
            validate_decisions(doc, self.events, self.state, {})
        self.events[0]['publication_status'] = 'held'
        with self.assertRaisesRegex(ValueError, '公開可能'):
            validate_decisions(doc, self.events, self.state, {})
        self.setUp()
        registry = {'held_articles': [{'status': 'held', 'article': self.a}]}
        with self.assertRaisesRegex(ValueError, 'editorial_hold'):
            validate_decisions(self.document(), self.events, self.state, registry)

    def test_duplicate_wrong_repo_and_incomplete_input_are_rejected(self):
        docs = []
        doc = self.document(); doc['decisions'] *= 2; docs.append(doc)
        doc = self.document(); doc['repository'] = 'elsewhere'; docs.append(doc)
        doc = self.document(); doc['decisions'][0]['note'] = ' '; docs.append(doc)
        doc = self.document('new_event'); doc['decisions'][0]['event_id'] = 'e1'; docs.append(doc)
        for doc in docs:
            with self.assertRaises(ValueError):
                validate_decisions(doc, self.events, self.state, {})

    def test_import_dry_run_and_atomic_batch_preserve_all_production_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payloads = {'events.json': self.events, 'tracking_triage.json': {'items': self.state},
                        'tracking_pending.json': [self.group], 'public_events.json': self.events,
                        'data_corrections.json': {}}
            for name, value in payloads.items():
                (root / name).write_text(json.dumps(value))
            before = {name: (root / name).read_bytes() for name in payloads}
            result = import_decisions(root, self.document(), now=NOW)
            self.assertFalse(result['written'])
            self.assertFalse((root / 'operator_review_decisions.json').exists())
            import_decisions(root, self.document(), apply=True, now=NOW)
            written = (root / 'operator_review_decisions.json').read_bytes()
            import_decisions(root, self.document(), apply=True, now=NOW)
            self.assertEqual(written, (root / 'operator_review_decisions.json').read_bytes())
            doc = self.document(); bad = copy.deepcopy(doc['decisions'][0]); bad['review_key'] = 'unknown'; doc['decisions'].append(bad)
            with self.assertRaises(ValueError):
                import_decisions(root, doc, apply=True, now=NOW)
            self.assertEqual(written, (root / 'operator_review_decisions.json').read_bytes())
            for name in payloads:
                self.assertEqual(before[name], (root / name).read_bytes())

    def test_real_snapshot_read_only_and_no_script_injection(self):
        root = Path(__file__).resolve().parents[1]
        names = ['events.json', 'tracking_triage.json', 'tracking_pending.json', 'public_events.json']
        before = {name: (root / name).read_bytes() for name in names}
        snapshot = build_snapshot(root, NOW)
        self.assertGreater(len(snapshot['reviews']), 0)
        snapshot['reviews'][0]['event_name'] = '</script><img src=x onerror=alert(1)>'
        html = render_page(snapshot)
        self.assertNotIn('</script><img', html)
        self.assertIn('\\u003c/script', html)
        for name in names:
            self.assertEqual(before[name], (root / name).read_bytes())

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the page DOM test')
    def test_page_javascript_operations_against_real_snapshot(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'review.html'
            page.write_text(render_page(build_snapshot(root, NOW)), encoding='utf-8')
            result = subprocess.run(['node', str(root / 'tests/operator_review_dom.cjs'), str(page)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_new_event_uses_existing_validation_and_does_not_count_initial_progress(self):
        self.events = []
        _, groups = self.prepare('new_event')
        with patch('event_tracker.load_corrections', return_value={}):
            merge_incoming(self.events, groups, FakeAI(report(self.a, relation='new_event', event_id='')), NOW, triage=self.state)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['progress_count'], 0)
        self.assertEqual(self.events[0]['article_count'], 1)
