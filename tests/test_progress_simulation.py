import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from simulation.run_progress_simulation import NOW, SPECS, FakeAI, proof, run_step, test_article, run
from tracking_v1 import apply_report
from merge_safety import UpdateHeld, article_content
from pending_triage import preflight

ROOT=Path(__file__).resolve().parents[1]
class ProgressSimulationTests(unittest.TestCase):
    def setUp(self):
        self.events=json.loads((ROOT/'simulation/fixtures/real_event_snapshot.json').read_text())
        self.e=copy.deepcopy(next(e for e in self.events if e['event_id']==SPECS[0][1]))
        self.title=SPECS[0][3][0][1]
    def apply(self,a,meaningful=True,stage='起訴'):
        apply_report(self.e,[a],proof(self.e,a,stage,meaningful),NOW+timedelta(minutes=10))
    def test_real_event_simulations_and_production_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:run(tmp)
    def test_rereport_reprint_typo_no_new_fact(self):
        for label in ['他媒体再報道','転載','誤字訂正','記事追加のみ']:
            with self.subTest(label=label):
                original=copy.deepcopy(self.e)
                a=test_article(article_content(self.e['articles'][0]).split('\n')[0],url='https://simulation.invalid/'+label)
                self.apply(a,False)
                for field in ['current_stage','summary','latest','next_watch','last_progress_at','last_progress_source_at','progress_count','progress_history']:
                    self.assertEqual(self.e[field],original[field])
                self.assertIn(a['url'],[a['url'] for a in self.e['articles']])
    def test_unrelated_article_transactionally_held(self):
        before=copy.deepcopy(self.events)
        a=test_article('札幌市の高校野球・全道大会の試合結果')
        r=proof(self.e,a);pending,reviews,_=run_step(self.events,self.e['event_id'],a,r,NOW)
        self.assertEqual(self.events,before);self.assertTrue(pending);self.assertTrue(reviews)
    def test_before_registration_backfill_not_progress(self):
        at=NOW-timedelta(hours=2)
        self.e['created_at']=(NOW-timedelta(hours=1)).isoformat()
        self.e['last_progress_source_at']=(NOW-timedelta(days=1)).isoformat()
        a=test_article(self.title,at=at);old=self.e['current_stage'];self.apply(a)
        self.assertEqual(self.e['progress_count'],0);self.assertEqual(self.e['current_stage'],old)
        self.assertIn(a['url'],[x['url'] for x in self.e['articles']])
    def test_existing_exact_fact_cannot_count_even_if_ai_says_true(self):
        title=article_content(self.e['articles'][0]).split('\n')[0]
        self.apply(test_article(title),True,stage='逮捕')
        self.assertEqual(self.e['progress_count'],0)
    def test_only_supporting_evidence_sets_source_watermark(self):
        a=test_article(self.title);b=test_article(self.title+'についての解説',url='https://simulation.invalid/commentary',at=NOW+timedelta(days=3))
        r=proof(self.e,a);r['identity_matches']+=proof(self.e,b)['identity_matches']
        apply_report(self.e,[a,b],r,NOW+timedelta(days=4))
        self.assertEqual(self.e['last_progress_source_at'],a['published'])
        c=test_article(SPECS[0][3][1][1],url='https://simulation.invalid/trial',at=NOW+timedelta(days=1))
        apply_report(self.e,[c],proof(self.e,c,'初公判'),NOW+timedelta(days=5))
        self.assertEqual(self.e['progress_count'],2)
    def test_known_evidence_cannot_support_new_rereport(self):
        a=test_article(self.title);self.apply(a)
        b=test_article(self.title+'の再報道',url='https://simulation.invalid/repeat',at=NOW+timedelta(days=1))
        r=proof(self.e,b);r.update(evidence_urls=[a['url']],progress_evidence=[{'url':a['url'],'quote':a['title']}])
        apply_report(self.e,[a,b],r,NOW+timedelta(days=2))
        self.assertEqual(self.e['progress_count'],1)
    def test_history_contains_quote_source_and_record_times(self):
        a=test_article(self.title);self.apply(a);h=self.e['progress_history'][-1]
        self.assertEqual(h['source_at'],a['published']);self.assertEqual(h['progress_evidence'],[{'url':a['url'],'quote':a['title']}])
        self.assertNotEqual(h['at'],h['source_at'])
    def test_same_url_updated_source_reaches_tracking_once(self):
        a=test_article(self.title,url=self.e['articles'][0]['url']);r=proof(self.e,a)
        es=[self.e];p,rv,c=run_step(es,self.e['event_id'],a,r,NOW+timedelta(minutes=10))
        self.assertFalse(p);self.assertFalse(rv);self.assertEqual(c,1);self.assertEqual(self.e['progress_count'],1)
        self.assertEqual(len(self.e['articles']),1);self.assertTrue(self.e['source_revisions'])
        p,rv,c=run_step(es,self.e['event_id'],a,r,NOW+timedelta(minutes=20))
        self.assertEqual(c,0);self.assertEqual(self.e['progress_count'],1)
    def test_same_url_unrelated_revision_requires_identity_proof(self):
        before=copy.deepcopy(self.e);a=test_article('札幌市の別の暴行事件',url=self.e['articles'][0]['url']);r=proof(self.e,a)
        es=[self.e];p,rv,c=run_step(es,self.e['event_id'],a,r,NOW)
        self.assertEqual(es,[before]);self.assertTrue(p)
    def test_same_url_revision_cannot_create_new_event(self):
        before=copy.deepcopy(self.e);a=test_article(self.title,url=self.e['articles'][0]['url']);r=proof(self.e,a)
        r.update(relation='new_event',event_id='')
        es=[self.e];p,rv,c=run_step(es,self.e['event_id'],a,r,NOW)
        self.assertEqual(es,[before]);self.assertTrue(p)
    def test_same_url_typo_revision_retains_original_without_progress(self):
        a=test_article(article_content(self.e['articles'][0]).split('\n')[0]+'（表記訂正）',url=self.e['articles'][0]['url'])
        self.apply(a,False,stage='逮捕')
        self.assertEqual(self.e['progress_count'],0);self.assertEqual(len(self.e['source_revisions']),1)
        self.assertEqual(self.e['articles'][0]['title'],a['title'])
    def test_same_url_editorial_hold_preserved(self):
        a=test_article(self.title,url=self.e['articles'][0]['url'])
        registry={'held_articles':[{'status':'held','article':a}]}
        known={x['url']:x for x in self.e['articles']}
        self.assertEqual(preflight({'category':self.e['category'],'articles':[a]},known,registry,{}),'editorial_hold')
    def test_missing_or_old_source_date_never_progress(self):
        for stamp in ['', '2020-01-01T00:00:00+00:00']:
            a=test_article(self.title);a['published']=stamp;self.apply(a)
            self.assertEqual(self.e['progress_count'],0)
