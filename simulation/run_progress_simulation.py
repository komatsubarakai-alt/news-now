"""Offline simulation: no HTTP, no production writes, .invalid sources only."""
import copy
import hashlib
import json
import sys
import tempfile
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from event_tracker import merge_incoming
from publication_safety import write_public
from tracking_v1 import atomic_save
from merge_safety import article_content

NOW = datetime(2026, 11, 1, 1, tzinfo=timezone.utc)
class FakeAI:
    def __init__(self, report): self.report = report; self.calls = 0
    def ask(self, prompt): self.calls += 1; return copy.deepcopy(self.report)

SPECS = [
 ('criminal', 'b565866f-bfb4-4888-b054-97e69d71a3f2', ('倉庫侵入・工具窃盗', 'リサイクルショップ'),
  [('起訴','札幌市の倉庫侵入・工具窃盗、リサイクルショップの通報で逮捕された男を起訴','初公判の予定が公表されるかを確認'),
   ('初公判','札幌市の倉庫侵入・工具窃盗、リサイクルショップの通報で逮捕・起訴された男の初公判','判決と控訴の有無を確認'),
   ('判決','札幌市の倉庫侵入・工具窃盗、リサイクルショップの通報で逮捕された男に判決','控訴と判決確定の有無を確認')]),
 ('policy','5d4bafaf-011f-4e26-9014-22a57b3fef2b',('新MICE施設','中島公園駅'),
  [('正式決定','札幌市が中島公園駅に接続する新MICE施設の基本計画を正式決定','事業費と着工に向けた手続きの続報を確認')]),
 ('accident','e98c5d08-4d42-4ae4-8b60-294778a05c75',('藻岩浄水場','電動ゲート'),
  [('原因判明','藻岩浄水場の電動ゲート倒壊事故、原因が判明','正式な調査結果と再発防止策の公表を確認'),
   ('調査結果公表','藻岩浄水場の電動ゲート倒壊事故、調査結果を公表','再発防止策の実施状況を確認')])]

def proof(e,a,stage=None,meaningful=True,anchors=None):
    anchors=anchors or next(s[2] for s in SPECS if s[1]==e['event_id'])
    prior=next(x for x in e['articles'] if all(v in x['title'] for v in anchors))
    return {'relation':'same_event','event_id':e['event_id'],'confidence':'high','meaningful_change':meaningful,
      'current_stage':stage or e['current_stage'],'summary':a['title'],'latest':a['title'],
      'next_watch':'続報と正式な資料を確認','evidence_urls':[a['url']],
      'progress_evidence':[{'url':a['url'],'quote':a['title']}],
      'identity_matches':[{'existing_url':prior['url'],'incoming_url':a['url'],
        'anchors':[{'kind':'case' if i==0 else 'detail','value':v,'existing_quote':article_content(prior).split('\n',1)[0],'incoming_quote':a['title']} for i,v in enumerate(anchors)]}]}

def test_article(title,url='https://simulation.invalid/new',at=NOW):
    return {'title':title,'url':url,'published':at.isoformat(),'description':'テスト専用の架空続報。本番ニュースではありません。','simulation_only':True}

def run_step(events,eid,a,r,now):
    e=next(e for e in events if e['event_id']==eid)
    g={'event_name':e['event_name'],'category':e['category'],'tracking_value':e['tracking_value'],'articles':[a]}
    ai=FakeAI(r);reviews=[]
    with patch('event_tracker.load_corrections',return_value={}):
        pending=merge_incoming(events,[g],ai,now,reviews,{})
    return pending,reviews,ai.calls


def run(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    protected=[ROOT/p for p in ('events.json','public_events.json','tracking_pending.json','data_corrections.json','ai_budget.json')]
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    base=json.loads((ROOT/'simulation/fixtures/real_event_snapshot.json').read_text()); before=copy.deepcopy(base);events=copy.deepcopy(base);results=[]
    for key,eid,anchors,steps in SPECS:
        for i,(stage,title,next_watch) in enumerate(steps):
            now=NOW+timedelta(days=i);a=test_article(title,f'https://simulation.invalid/{key}/{i}',now)
            e=next(e for e in events if e['event_id']==eid);count=e['progress_count'];r=proof(e,a,stage);r['next_watch']=next_watch
            pending,reviews,calls=run_step(events,eid,a,r,now+timedelta(minutes=10))
            e=next(e for e in events if e['event_id']==eid)
            assert not pending and not reviews
            assert e['progress_count']==count+1 and e['current_stage']==stage and e['latest']==title and e['next_watch']==next_watch
            assert e['progress_history'][-1]['evidence_urls']==[a['url']]
            assert e['last_progress_source_at']==a['published']
            results.append({'case':key,'stage':stage,'progress_count':e['progress_count'],'ai_calls':calls,'outcome':'pass'})
    with tempfile.TemporaryDirectory() as temp:
        prev=Path.cwd()
        try:
            os.chdir(temp)
            for name,data in [('before',before),('after',events)]:
                atomic_save('events.json',data)
                public=write_public(data,json.loads((ROOT/'data_corrections.json').read_text()))
                folder=output/name;folder.mkdir(exist_ok=True)
                atomic_save(folder/'public_events.json',public)
                (folder/'index.html').write_bytes((ROOT/'index.html').read_bytes())
                (folder/'tracking_health.json').write_text('{}')
                assert {s[1] for s in SPECS}.issubset({e['event_id'] for e in public})
        finally:os.chdir(prev)
    assert hashes=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    atomic_save(output/'results.json',{'simulation_only':True,'ai_mode':'deterministic mocked responses; no API calls', 'source_commit':'ff25fb80910c3621a096b8c2e1e9cea3263376e2','production_files_unchanged':hashes,'cases':results})
    print(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__':run(Path(sys.argv[1]).resolve())
