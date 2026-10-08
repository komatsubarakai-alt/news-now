import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from event_tracker import merge_incoming, lookup_event
from merge_safety import (UpdateHeld, validate_identity, validate_saved_update,
                          review, save_reviews, source_context)
from news_ai_grouping import build_groups
from news_group_merge import merge_groups
from test_tracking import NOW, FakeAI, article, event, report
from tracking_v1 import apply_report


def proof(a, b, case='江別豊幌住宅強盗', entity='被害者山田'):
    return {'relation': 'same_event', 'confidence': 'high', 'identity_matches': [
        {'existing_url': a['url'], 'incoming_url': b['url'], 'anchors': [
            {'kind': kind, 'value': value, 'existing_quote': a['title'], 'incoming_quote': b['title']}
            for kind, value in [('case', case), ('entity', entity)]]}]}


def group(a, **extra):
    return {'event_name': '江別豊幌住宅強盗', 'category': '事件・事故', 'tracking_value': 'high',
            'articles': [a], **extra}


class MergeSafetyTests(unittest.TestCase):
    def setUp(self):
        data = json.loads((Path(__file__).parent/'fixtures/mice_baseball.json').read_text())
        self.mice = next(g for g in data['groups'] if 'MICE' in g['event_name'])
        self.baseball = next(g for g in data['groups'] if 'MICE' not in g['event_name'])

    def test_actual_mice_baseball_pair_rejected_even_ai_high(self):
        a, b = self.mice['articles'][0], self.baseball['articles'][0]
        decision = proof(a, b, '札幌', '北海道')
        groups, held = build_groups([{'article_a': a, 'article_b': b, 'ai_result': json.dumps(decision)}])
        self.assertEqual(len(groups), 2)
        self.assertTrue(held)
        self.assertEqual(sum(len(g['articles']) for g in groups), 2)

    def test_actual_mice_baseball_group_merge_rejected(self):
        groups, held = merge_groups([self.mice, self.baseball], lambda a,b: {
            'relation': 'same_event', 'confidence': 'high', 'event_name': self.mice['event_name']})
        self.assertEqual(len(groups), 2)
        self.assertTrue(held)

    def test_actual_mice_baseball_event_update_atomic(self):
        e = {**event(), 'event_name': self.mice['event_name'], 'category': '地域・インフラ',
             'articles': copy.deepcopy(self.mice['articles'])}
        before = copy.deepcopy(e); b = self.baseball['articles'][0]
        reviews = []
        pending = merge_incoming([e], [group(b)], FakeAI(report(b)), NOW, reviews)
        self.assertEqual(e, before)
        self.assertEqual(len(pending), 1)
        self.assertIn('event_article_contradiction', reviews[0]['reason'])

    def test_actual_sports_event_cannot_inherit_mice_name(self):
        events = []; b = self.baseball['articles'][0]
        pending = merge_incoming(events, [group(b, event_name=self.mice['event_name'], category='地域・インフラ')],
                                 FakeAI(report(b, relation='new_event', event_id='')), NOW)
        self.assertEqual(events, [])
        self.assertEqual(len(pending), 1)

    def test_new_event_mixed_group_rejected(self):
        a, b = self.mice['articles'][0], self.baseball['articles'][0]
        events = []
        pending = merge_incoming(events, [group(a, articles=[a,b], event_name=self.mice['event_name'], category='地域・インフラ')],
                                 FakeAI(report(a, relation='new_event', event_id='')), NOW)
        self.assertEqual(events, [])
        self.assertEqual(len(pending), 1)

    def test_latest_four_article_mixed_mice_event_is_held(self):
        saved=json.loads((Path(__file__).parent/'fixtures/mice_baseball.json').read_text())['latest_event']
        mice=next(a for a in saved['articles'] if 'MICE' in a['title'])
        events=[]
        answer=report(mice,relation='new_event',event_id='',meaningful_change=False,
                      event_identity={'value':'MICE','url':mice['url'],'quote':mice['title']})
        pending=merge_incoming(events,[saved],FakeAI(answer),NOW)
        self.assertEqual(events,[])
        self.assertEqual(len(pending),1)

    def test_region_and_publisher_cannot_be_identity(self):
        a = article('札幌のニュース - 北海道新聞デジタル'); b = article('札幌のニュース - 北海道新聞デジタル', 'https://example.test/b')
        with self.assertRaises(UpdateHeld):
            validate_identity([a], [b], proof(a,b,'札幌','北海道新聞デジタル'))

    def test_fabricated_quote_is_held(self):
        a = article(); b = article('容疑者を起訴', 'https://example.test/b')
        d = proof(a,b); d['identity_matches'][0]['anchors'][0]['incoming_quote'] = '架空の根拠'
        with self.assertRaises(UpdateHeld): validate_identity([a],[b],d)

    def test_only_one_of_two_articles_proved_rejects_batch(self):
        a = article(); b = article('容疑者を起訴', 'https://example.test/b')
        c = article('別件', 'https://example.test/c')
        with self.assertRaises(UpdateHeld): validate_identity([a],[b,c],proof(a,b))

    def test_different_case_same_person_held(self):
        a = article(); b = {'title': '被害者山田 札幌西区の別の暴行事件', 'url':'https://example.test/b'}
        with self.assertRaises(UpdateHeld): validate_identity([a],[b],proof(a,b))

    def test_article_transitive_bridge_does_not_merge_three(self):
        a, b, c = [article('続報', 'https://example.test/'+x) for x in 'abc']
        results = [{'article_a':x,'article_b':y,'ai_result':proof(x,y)} for x,y in [(a,b),(b,c)]]
        groups, held = build_groups(results)
        self.assertEqual(sorted(len(g['articles']) for g in groups), [1,2])
        self.assertTrue(any(i['reason']=='transitive_pair_unproved' for i in held))

    def test_group_transitive_bridge_does_not_merge_three(self):
        a,b,c = [article('続報', 'https://example.test/'+x) for x in 'abc']
        def judge(x,y):
            if x['articles'][0]['url'].endswith('/a') and y['articles'][0]['url'].endswith('/c'):
                return {'relation':'different','confidence':'high'}
            return proof(x['articles'][0],y['articles'][0])
        groups, held = merge_groups([group(a),group(b),group(c)],judge)
        self.assertEqual(sorted(len(g['articles']) for g in groups),[1,2])
        self.assertTrue(held)

    def test_case_arrest_indictment_trial_all_stages_merge(self):
        e=event(); e['articles']=[article('事件発生')]; e['current_stage']='発生'
        for index, stage in enumerate(['逮捕','送検','起訴','公判','判決']):
            a = article('容疑者の'+stage, 'https://example.test/'+str(index), (NOW+timedelta(days=index)).isoformat())
            d=report(a,current_stage=stage)
            d['identity_matches']=proof(e['articles'][0],a)['identity_matches']
            pending=merge_incoming([e],[group(a)],FakeAI(d),NOW+timedelta(days=index))
            self.assertEqual(pending,[],stage)
            self.assertEqual(e['current_stage'],stage)
        self.assertEqual(e['progress_count'],5)
        self.assertEqual(len(e['articles']),6)

    def test_good_mice_plan_followup_merges(self):
        a={'title':'札幌MICE計画 中島公園駅直結の施設概要を公表','url':'https://example.test/a','published':NOW.isoformat()}
        b={'title':'札幌MICE計画 中島公園駅直結の施設を着工','url':'https://example.test/b','published':NOW.isoformat()}
        e={**event(),'event_name':'札幌MICE計画','category':'地域・インフラ','articles':[a]}
        d={**report(b,current_stage='着工'),'identity_matches':proof(a,b,'札幌MICE','中島公園駅')['identity_matches']}
        self.assertEqual(merge_incoming([e],[group(b,category='地域・インフラ')],FakeAI(d),NOW),[])
        self.assertEqual(e['current_stage'],'着工')

    def test_category_change_only_holds_bad_event(self):
        first=event(); second=copy.deepcopy(first);second['event_id']='e2'
        a=article('容疑者を起訴','https://example.test/a',NOW.isoformat()); b=article('容疑者を起訴','https://example.test/b',NOW.isoformat())
        before=copy.deepcopy(first)
        pending=merge_incoming([first,second],[group(a),group(b)],
                              FakeAI(report(a,category='災害'),report(b,event_id='e2')),NOW)
        self.assertEqual(first,before)
        self.assertEqual(second['current_stage'],'起訴')
        self.assertEqual(len(pending),1)

    def test_no_progress_quote_no_mutation(self):
        e=event(); before=copy.deepcopy(e); a=article('容疑者を起訴','https://example.test/a',NOW.isoformat())
        with self.assertRaises(UpdateHeld): apply_report(e,[a],report(a,progress_evidence=[]),NOW)
        self.assertEqual(e,before)

    def test_claimed_indictment_from_arrest_title_held(self):
        e=event(); before=copy.deepcopy(e); a=article('容疑者を逮捕','https://example.test/a',NOW.isoformat())
        with self.assertRaises(UpdateHeld): apply_report(e,[a],report(a,current_stage='起訴'),NOW)
        self.assertEqual(e,before)

    def test_presave_category_and_content_contradictions_held(self):
        before=event(); after=copy.deepcopy(before);after['category']='災害'
        with self.assertRaises(UpdateHeld): validate_saved_update(before,after)
        before['event_name']='札幌MICE計画'; after=copy.deepcopy(before);after['summary']='高校野球で甲子園出場'
        with self.assertRaises(UpdateHeld): validate_saved_update(before,after)

    def test_backfill_wrong_selection_held_without_mutation(self):
        e={**event(),'event_name':self.mice['event_name'],'articles':self.mice['articles']}
        old=copy.deepcopy(self.baseball['articles'][0]);old['published']='2020-01-01T00:00:00Z'
        before=copy.deepcopy(e)
        ai=FakeAI({'confidence':'high','matches':[{'url':old['url'],'confidence':'high'}], 'identity_matches':[]})
        with self.assertRaises(UpdateHeld): lookup_event(e,ai,'test',NOW,True,lambda q:[old])
        self.assertEqual(e,before)

    def test_explicit_negative_pair_blocks_group_merge_without_ai_call(self):
        a,b=article(),article('続報','https://example.test/b')
        def judge(x,y):
            self.fail('既に別案件と判定したペアを再統合してはいけない')
        groups,held=merge_groups([group(a),group(b)],judge,{frozenset((a['url'],b['url']))})
        self.assertEqual(len(groups),2)
        self.assertEqual(held[0]['reason'],'explicit_pair_conflict')

    def test_reviews_persist_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'review.json'); i=review('event_attachment','uncertain',group(article()))
            save_reviews([i],path);save_reviews([i],path)
            self.assertEqual(len(json.loads(Path(path).read_text())),1)

    def test_available_description_is_used_for_identity(self):
        a={'title':'事件の続報','url':'https://example.test/a','description':'江別豊幌住宅強盗 被害者山田'}
        b={'title':'起訴の続報','url':'https://example.test/b','description':'江別豊幌住宅強盗 被害者山田'}
        d=proof(a,b)
        for anchor in d['identity_matches'][0]['anchors']:
            anchor['existing_quote']=a['description'];anchor['incoming_quote']=b['description']
        validate_identity([a],[b],d)
        self.assertEqual(source_context(a)['description'],a['description'])
