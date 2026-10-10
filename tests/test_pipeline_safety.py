import contextlib
import io
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from audit_merges import audit
from event_tracker import enrich, merge_incoming
from test_tracking import NOW, FakeAI, article, event, report


class PipelineSafetyTests(unittest.TestCase):
    def test_backfill_transaction_preserves_done_state(self):
        e=event();e['v1_enriched']=True
        old=article('事件発生','https://example.test/old','2020-01-01T00:00:00Z')
        answer={**report(old,meaningful_change=False),'matches':[{'url':old['url'],'confidence':'high'}]}
        enrich([e],FakeAI({'query':'江別豊幌住宅強盗'},answer),NOW,finder=lambda q:[old])
        self.assertTrue(e['backfill']['done'])
        self.assertEqual(e['article_count'],2)
        self.assertEqual(e['current_stage'],'逮捕')

    def test_ambiguous_search_retries_and_records_source(self):
        e=event();e['v1_enriched']=True;reviews=[]
        old=article('事件発生','https://example.test/old','2020-01-01T00:00:00Z')
        enrich([e],FakeAI({'query':'案件'},{'matches':[]}),NOW,finder=lambda q:[old],reviews=reviews)
        self.assertEqual(e['backfill']['attempts'],1)
        self.assertNotIn('done',e['backfill'])
        self.assertEqual(reviews[0]['payload']['articles'],[old])

    def test_status_budget_retries_without_losing_articles(self):
        script=Path(__file__).resolve().parents[1]/'news_status.py'
        groups=[{'event_name':str(i),'articles':[article(url='https://example.test/'+str(i))]} for i in range(17)]
        payload={'output':[{'content':[{'text':json.dumps({'category':'事件・事故','tracking_value':'high'})}]}]}
        def response(*args,**kwargs):
            return io.BytesIO(json.dumps(payload).encode())
        before=os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                Path('news_groups_deduplicated.json').write_text(json.dumps(groups))
                with patch.dict(os.environ,{'OPENAI_API_KEY':'test', 'STATUS_GROUP_LIMIT':'16', 'AI_RUN_CALL_LIMIT':'24', 'AI_DAILY_CALL_LIMIT':'40', 'AI_DAILY_USD_LIMIT':'0.20'}), patch('urllib.request.urlopen',side_effect=response) as network, contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(script),run_name='__main__')
                    self.assertEqual(network.call_count,16)
                self.assertEqual(len(json.loads(Path('news_status.json').read_text())),16)
                self.assertEqual(json.loads(Path('status_pending.json').read_text()),groups[16:])
                Path('news_groups_deduplicated.json').write_text('[]')
                with patch.dict(os.environ,{'OPENAI_API_KEY':'test', 'STATUS_GROUP_LIMIT':'16', 'AI_RUN_CALL_LIMIT':'24', 'AI_DAILY_CALL_LIMIT':'40', 'AI_DAILY_USD_LIMIT':'0.20'}),patch('urllib.request.urlopen',side_effect=response) as network, contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(str(script),run_name='__main__')
                    self.assertEqual(network.call_count,1)
                self.assertEqual(json.loads(Path('status_pending.json').read_text()),[])
            finally:
                os.chdir(before)

    def test_uncertain_queue_rotates_to_avoid_starvation(self):
        groups=[{'category':'事件・事故','tracking_value':'high','articles':[article(url='https://example.test/'+str(i))]} for i in range(17)]
        ai=FakeAI(*[{'relation':'uncertain','confidence':'low'} for _ in range(16)])
        pending=merge_incoming([],groups,ai,NOW)
        self.assertEqual(pending[0],groups[16])
        self.assertEqual(len(pending),17)

    def test_legacy_writers_are_locked_before_any_data_access(self):
        root=Path(__file__).resolve().parents[1]
        for name in ['events_update.py','events_cleanup.py']:
            with patch('builtins.open', side_effect=AssertionError('既存データに触れてはいけない')):
                with self.assertRaisesRegex(RuntimeError,'旧データ更新'):
                    runpy.run_path(str(root/name),run_name='__main__')

    def test_audit_does_not_modify_saved_data(self):
        e=event();e['event_name']='札幌MICE計画';e['articles']=[article('高校野球 甲子園')]
        encoded=json.dumps(e,sort_keys=True)
        self.assertTrue(audit([e]))
        self.assertEqual(encoded,json.dumps(e,sort_keys=True))
