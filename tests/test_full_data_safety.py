import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from merge_safety import UpdateHeld, validate_identity, validate_saved_update
from publication_safety import public_events, protect_existing, excluded, publication_issues
from news_group_merge import merge_groups
from tracking_sources import BudgetExceeded
from event_tracker import merge_incoming
from test_tracking import NOW, FakeAI, article, event, report
from test_merge_safety import proof, group

ROOT=Path(__file__).resolve().parents[1]

class FullDataSafetyTests(unittest.TestCase):
    def test_separate_accidents_even_with_common_exact_phrases(self):
        a={'title':'厚岸町の国道で車転落 男性意識不明','url':'https://example.test/a'}
        b={'title':'新潟県柏崎市で車転落 女性意識不明','url':'https://example.test/b'}
        with self.assertRaises(UpdateHeld): validate_identity([a],[b],proof(a,b,'車転落','意識不明'))

    def test_same_region_generic_case_is_not_identity(self):
        a={'title':'札幌市北区で車転落 意識不明','url':'https://example.test/a'}
        b={'title':'札幌市北区で別の車転落 意識不明','url':'https://example.test/b'}
        with self.assertRaises(UpdateHeld): validate_identity([a],[b],proof(a,b,'車転落','意識不明'))

    def test_existing_corruption_detected_even_without_new_article(self):
        e=event(); e['event_name']='札幌MICE計画';e['summary']='高校野球の全道大会';
        with self.assertRaises(UpdateHeld): validate_saved_update(copy.deepcopy(e),e)

    def test_count_mismatch_is_held(self):
        e=event();e['article_count']=9
        with self.assertRaises(UpdateHeld):validate_saved_update(event(),e)

    def test_held_article_never_public_or_attached(self):
        a=article('容疑者を起訴',url='https://example.test/held'); e=event(); old=copy.deepcopy(e)
        registry={'held_articles':[{'status':'held','article':a}]}
        triage={}
        with patch('event_tracker.load_corrections',return_value=registry):
            pending=merge_incoming([e],[group(a)],FakeAI(report(a)),NOW,triage=triage)
        self.assertEqual(e,old);self.assertEqual(pending,[])
        retained=next(iter(triage.values()))
        self.assertEqual(retained['status'],'editorial_hold')
        self.assertEqual(retained['group']['articles'],[a])
        self.assertTrue(excluded({**a,'url':'https://example.test/repost'},registry))

    def test_held_event_frozen_and_not_public(self):
        e=event();e['publication_status']='held';before=copy.deepcopy(e);e['summary']='新規上書き'
        with self.assertRaises(UpdateHeld):validate_saved_update(before,e)
        self.assertEqual(public_events([e]),[])

    def test_individual_bad_event_does_not_remove_normal_news(self):
        bad=event();bad['summary']='高校野球';bad['event_name']='札幌MICE計画'
        good=event();good['event_id']='good'
        self.assertEqual([e['event_id'] for e in public_events([bad,good])],['good'])

    def test_blocked_reattachment_quarantines_without_deletion(self):
        e=event();before=copy.deepcopy(e)
        protect_existing([e],{'event_rules':[{'event_id':'e1','blocked_urls':[e['articles'][0]['url']]}]})
        self.assertEqual(e['articles'],before['articles']);self.assertEqual(e['publication_status'],'held')

    def test_multi_accident_new_event_is_held(self):
        a=article('厚岸町 車転落 札幌北区 単独事故',url='https://example.test/multi')
        events=[]
        pending=merge_incoming(events,[group(a)],FakeAI(report(a,relation='new_event',event_id='')),NOW)
        self.assertEqual(events,[]);self.assertEqual(len(pending),1)

    def test_group_budget_stops_without_thousands_of_duplicate_reviews(self):
        groups=[group(article(url='https://example.test/'+str(i))) for i in range(25)]
        calls=[]
        def judge(a,b):
            calls.append(1);raise BudgetExceeded()
        result,held=merge_groups(groups,judge)
        self.assertEqual(len(calls),1);self.assertEqual(len(held),1);self.assertEqual(len(result),25)

    def test_all_original_sources_preserved_in_events_or_hold(self):
        original=json.loads((ROOT/'events_before_full_audit.json').read_text())
        now=json.loads((ROOT/'events.json').read_text())
        reg=json.loads((ROOT/'data_corrections.json').read_text())
        old={a['url'] for e in original for a in e['articles']}
        saved={a['url'] for e in now for a in e['articles']}|{d['article']['url'] for d in reg['held_articles']}
        self.assertTrue(old<=saved)

    def test_corrected_public_data_passes_gate_and_mice_clean(self):
        es=json.loads((ROOT/'public_events.json').read_text())
        self.assertTrue(es)
        for e in es:self.assertEqual(publication_issues(e),[])
        mice=[e for e in es if e['event_id']=='5d4bafaf-011f-4e26-9014-22a57b3fef2b']
        self.assertEqual(len(mice),1)
        self.assertFalse(any('高校野球' in a['title'] for e in mice for a in e['articles']))
        accident=[e for e in es if e['event_id'] in {'e72f1865-5261-49b1-ba55-ae5e7ac47a00','719c979d-347a-5455-a5da-55aefe678081'}]
        self.assertEqual(len(accident),2)
        self.assertNotEqual(accident[0]['event_id'],accident[1]['event_id'])
