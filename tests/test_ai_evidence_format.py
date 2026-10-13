"""Prompt-derived mock answers must pass the unchanged source-bound validators."""
import copy
import hashlib
import io
import json
import os
import runpy
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from event_tracker import analyze, lookup_event, merge_incoming
from merge_safety import (UpdateHeld, article_content, source_context,
                          validate_identity)
from news_ai_grouping import build_groups
from news_group_merge import ask_ai, merge_groups
from tracking_v1 import completion_verified, merge_schedules
from test_tracking import NOW, article, event

ROOT = Path(__file__).resolve().parents[1]


def source(stage='容疑者を起訴', url='https://example.test/new', published=None):
    return {**article(stage, url, published or NOW.isoformat()),
            'title': article(stage)['title'] + ' - 報道社'}


def identity(old, new):
    return [{'existing_url': old['url'], 'incoming_url': new['url'], 'anchors': [
        {'kind': kind, 'value': value, 'existing_quote': old['title'], 'incoming_quote': new['title']}
        for kind, value in [('case', '江別豊幌住宅強盗'), ('entity', '被害者山田')]]}]


def example(prompt):
    marker = '形式例の空文字・空配列・空オブジェクトは、根拠がないときの値。確認できた場合だけ入力から値を埋め、根拠やURLを作らない。\n'
    return json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]


def tracking_data(prompt):
    saved, incoming = prompt.split('\n保存済み出来事:\n', 1)[1].split('\n今回の記事:\n', 1)
    return json.loads(saved), json.loads(incoming)


class MockAI:
    def __init__(self, responder):
        self.responder = responder
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.responder(prompt)


def tracked_answer(prompt):
    saved, incoming = tracking_data(prompt)
    result = example(prompt)
    old, new = saved[0]['articles'][0], incoming[0]
    result.update(relation='same_event', event_id=saved[0]['event_id'], confidence='high',
                  meaningful_change=True, current_stage='起訴', evidence_urls=[new['url']],
                  identity_matches=identity(old, new),
                  progress_evidence=[{'url': new['url'], 'quote': new['title']}])
    return result


class SourceContextTests(unittest.TestCase):
    def test_title_is_validator_text_without_mutating_original(self):
        a = source(); original = copy.deepcopy(a)
        context = source_context(a)
        self.assertEqual(context['title'], article_content(a).split('\n', 1)[0])
        self.assertNotIn(' - 報道社', context['title'])
        self.assertEqual(a, original)

    def test_description_is_exact_sanitized_substring_and_repeat_is_omitted(self):
        a = source(); title = source_context(a)['title']
        a['description'] = '<b>被害者山田</b> &amp; 別の固有情報'
        self.assertIn(source_context(a)['description'], article_content(a))
        a['description'] = f'<a>{title}</a> <font>報道社</font>'
        self.assertEqual(source_context(a)['description'], '')

    def test_long_description_prefix_preserves_exact_whitespace(self):
        a = source(); a['description'] = ' 固有事実  ' * 400
        text = source_context(a)['description']
        self.assertEqual(len(text), 1800)
        self.assertIn(text, article_content(a))


class TrackingFormatTests(unittest.TestCase):
    def setUp(self):
        self.e = event()
        self.e['articles'][0]['title'] += ' - 報道社'
        self.incoming = source()

    def test_complete_tracking_example_and_correct_quote_advances(self):
        before = copy.deepcopy(self.incoming)
        ai = MockAI(tracked_answer)
        with patch('event_tracker.load_corrections', return_value={}):
            pending = merge_incoming([self.e], [{'event_name': self.e['event_name'],
                'category': self.e['category'], 'tracking_value': 'high', 'articles': [self.incoming]}], ai, NOW)
        self.assertEqual(pending, [])
        self.assertEqual(self.e['progress_count'], 1)
        self.assertEqual(self.e['current_stage'], '起訴')
        schema = example(ai.prompts[0])
        self.assertTrue({'identity_matches', 'event_identity', 'progress_evidence', 'multi_event',
                         'evidence_urls', 'completion_quote', 'schedules'}.issubset(schema))
        self.assertEqual(self.incoming, before)
        self.assertIn(' - 報道社', self.e['articles'][-1]['title'])

    def test_missing_identity_or_progress_still_holds_without_mutation(self):
        for field in ['identity_matches', 'progress_evidence']:
            with self.subTest(field=field):
                e = copy.deepcopy(self.e); before = copy.deepcopy(e)
                def answer(prompt):
                    value = tracked_answer(prompt); value.pop(field); return value
                with patch('event_tracker.load_corrections', return_value={}):
                    pending = merge_incoming([e], [{'category': e['category'], 'tracking_value': 'high',
                        'event_name': e['event_name'], 'articles': [self.incoming]}], MockAI(answer), NOW)
                self.assertEqual(e, before); self.assertEqual(len(pending), 1)

    def test_invalid_kind_quote_url_or_independent_anchor_still_fails(self):
        ai = MockAI(tracked_answer)
        good = analyze(ai, [self.e], [self.incoming])
        cases = []
        bad = copy.deepcopy(good); bad['identity_matches'][0]['anchors'][1]['kind'] = 'entity/detail'; cases.append(bad)
        bad = copy.deepcopy(good); bad['identity_matches'][0]['anchors'][1]['incoming_quote'] += ' - 報道社'; cases.append(bad)
        bad = copy.deepcopy(good); bad['identity_matches'][0]['existing_url'] = 'https://invented.invalid/a'; cases.append(bad)
        bad = copy.deepcopy(good); bad['identity_matches'][0]['anchors'] = bad['identity_matches'][0]['anchors'][:1]; cases.append(bad)
        bad = copy.deepcopy(good); bad['identity_matches'][0]['anchors'][1]['value'] = '双方にはない言い換え'; cases.append(bad)
        for report in cases:
            with self.subTest(report=report):
                with self.assertRaises(UpdateHeld):
                    validate_identity(self.e['articles'], [self.incoming], report)

    def test_new_event_name_is_visible_and_must_be_proved(self):
        name = '江別豊幌住宅強盗'
        def answer(prompt):
            _, incoming = tracking_data(prompt); a = incoming[0]
            supplied_name = json.loads(prompt.split('\n今回の出来事名（データ）:\n', 1)[1].split('\n', 1)[0])
            self.assertEqual(supplied_name, name)
            result = example(prompt)
            result.update(relation='new_event', confidence='high', current_stage='起訴',
                          evidence_urls=[a['url']], event_identity={'value': supplied_name,
                          'url': a['url'], 'quote': a['title']})
            return result
        events = []; ai = MockAI(answer)
        with patch('event_tracker.load_corrections', return_value={}):
            pending = merge_incoming(events, [{'event_name': name, 'category': '事件・事故',
                'tracking_value': 'high', 'articles': [self.incoming]}], ai, NOW)
        self.assertEqual(pending, []); self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['progress_count'], 0)

    def test_uncertain_response_cannot_add_or_advance(self):
        before = copy.deepcopy(self.e)
        with patch('event_tracker.load_corrections', return_value={}):
            pending = merge_incoming([self.e], [{'event_name': self.e['event_name'],
                'category': self.e['category'], 'tracking_value': 'high', 'articles': [self.incoming]}],
                MockAI(example), NOW)
        self.assertEqual(self.e, before); self.assertEqual(len(pending), 1)

    def test_description_only_completion_or_schedule_is_not_accepted(self):
        a = source(); a['description'] = '全面復旧完了。2026年12月17日に開業予定'
        report = {'completed': True, 'confidence': 'high', 'completion_quote': '全面復旧完了',
                  'evidence_urls': [a['url']]}
        self.assertFalse(completion_verified(report, [a]))
        e = copy.deepcopy(self.e)
        merge_schedules(e, [{'label': '開業', 'quote': '2026年12月17日に開業予定',
                            'source_url': a['url'], 'status': 'scheduled'}], [a])
        self.assertEqual(e['schedules'], [])


class OtherPromptPathsTests(unittest.TestCase):
    def setUp(self):
        self.old = source('容疑者を逮捕', 'https://example.test/old')
        self.new = source()

    def test_pair_prompt_example_produces_valid_group_from_sanitized_titles(self):
        saved_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path('ai_candidates.json').write_text(json.dumps([{'article_a': self.old, 'article_b': self.new}]))
                def fake_response(prompt, **kwargs):
                    data = json.loads(prompt.rsplit('\n', 1)[-1]); result = example(prompt)
                    self.assertIn('identity_matches', result)
                    self.assertNotIn(' - 報道社', prompt)
                    result.update(relation='same_event', confidence='high', event_name='江別豊幌住宅強盗',
                                  identity_matches=identity(data['existing'], data['incoming']))
                    return {'output': [{'content': [{'text': json.dumps(result)}]}]}
                with patch('ai_cost_control.response', side_effect=fake_response) as calls, patch('ai_cost_control.cached', return_value=False), redirect_stdout(io.StringIO()):
                    runpy.run_path(str(ROOT / 'news_pairing.py'), run_name='__main__')
                self.assertEqual(calls.call_count, 1)
                groups, held = build_groups(json.loads(Path('ai_results.json').read_text()))
                self.assertEqual(len(groups), 1); self.assertEqual(held, [])
                self.assertCountEqual(groups[0]['articles'], [self.old, self.new])
            finally:
                os.chdir(saved_cwd)

    def test_group_prompt_uses_same_quote_contract(self):
        a = {'event_name': '江別豊幌住宅強盗', 'articles': [self.old]}
        b = {'event_name': '江別豊幌住宅強盗', 'articles': [self.new]}
        def answer(prompt):
            data = json.loads(prompt.rsplit('\n', 1)[-1]); result = example(prompt)
            result.update(relation='same_event', confidence='high', event_name=a['event_name'],
                          identity_matches=identity(data['A']['articles'][0], data['B']['articles'][0]))
            return result
        groups, held = merge_groups([a, b], lambda left, right: ask_ai(MockAI(answer), left, right))
        self.assertEqual(len(groups), 1); self.assertEqual(held, [])
        self.assertIn(' - 報道社', groups[0]['articles'][0]['title'])

    def test_backfill_prompt_sanitizes_candidates_and_retains_original_sources(self):
        e = event(); e['articles'][0]['title'] += ' - 報道社'
        older = source('事件発生', 'https://example.test/history', '2020-01-01T00:00:00Z')
        original = copy.deepcopy(older)
        def answer(prompt):
            data = json.loads(prompt.rsplit('\n', 1)[-1]); result = example(prompt)
            a = data['candidates'][0]; old = data['event']['articles'][0]
            self.assertNotIn(' - 報道社', a['title'])
            self.assertIn('evidence_urls', result)
            result.update(confidence='high', matches=[{'url': a['url'], 'confidence': 'high'}],
                          evidence_urls=[a['url']], identity_matches=identity(old, a))
            return result
        with patch('event_tracker.load_corrections', return_value={}):
            lookup_event(e, MockAI(answer), 'mock query', NOW, historical=True, finder=lambda q: [older])
        self.assertEqual(e['article_count'], 2); self.assertEqual(e['progress_count'], 0)
        self.assertEqual(older, original)
        self.assertEqual(e['articles'][0]['title'], older['title'])

    def test_real_sources_use_validator_text_and_json_unchanged(self):
        hashes = lambda: {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in ROOT.rglob('*.json')}
        before = hashes(); events = json.loads((ROOT / 'events.json').read_text())
        pending = json.loads((ROOT / 'tracking_pending.json').read_text())
        for e in events + pending:
            for a in e['articles']:
                context = source_context(a)
                self.assertIn(context['title'], article_content(a))
                self.assertIn(context['description'], article_content(a))
        self.assertEqual(hashes(), before)
