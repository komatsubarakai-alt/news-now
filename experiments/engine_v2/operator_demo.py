"""Explicit fictional fixture, never real news or operator evidence."""
import copy
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from engine import dumps, import_snapshot
from tracking_v1 import migrate
from event_tracker import TRACKING_RESPONSE
import revision_sync as sync


def populate(db):
    at=datetime(2026,10,8,11,tzinfo=timezone.utc)
    base={'title':'江別豊幌住宅強盗 被害者山田 容疑者を逮捕','url':'https://example.test/fictional-old','published':'2026-10-07T10:00:00+00:00','source':'架空媒体'}
    incoming={**base,'title':'江別豊幌住宅強盗 被害者山田 容疑者を起訴','url':'https://example.test/fictional-new','published':at.isoformat()}
    e=migrate({'event_id':'demo-e1','event_name':'【架空・操作練習】江別豊幌住宅強盗','category':'事件・事故',
       'tracking_value':'high','current_stage':'逮捕','summary':'容疑者を逮捕','updated_at':'2026-10-07T12:00:00+00:00','articles':[base]},at)
    group={'event_name':'江別豊幌住宅強盗','category':'事件・事故','tracking_value':'high','articles':[incoming]}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'events.json').write_text(dumps([e]));(root/'tracking_pending.json').write_text(dumps([group]));import_snapshot(db,root)
    proof=[]
    for article in (base,incoming):
        sync.seed(db,article)
        proof.append(sync.add_evidence(db,'demo-e1',article['url'],'title',article['title']))
    answer=copy.deepcopy(TRACKING_RESPONSE)
    answer.update({'relation':'same_event','event_id':'demo-e1','confidence':'high','meaningful_change':True,'current_stage':'起訴',
        'evidence_urls':[incoming['url']], 'progress_evidence':[{'url':incoming['url'],'quote':incoming['title']}],
        'event_identity':{'value':'江別豊幌住宅強盗','url':incoming['url'],'quote':incoming['title']},
        'identity_matches':[{'existing_url':base['url'],'incoming_url':incoming['url'],'anchors':[
          {'kind':kind,'value':value,'existing_quote':base['title'],'incoming_quote':incoming['title']}
          for kind,value in [('case','江別豊幌住宅強盗'),('entity','被害者山田')]]}]})
    jid=sync.make_job(db,'demo-e1',group,proof,at)
    sync.call_mock(db,jid,lambda prompt:answer,at)
    result=sync.adopt(db,'demo-guarded-adoption',jid,at)
    if result['status']!='accepted': raise ValueError('fictional fixture did not pass safety guards: '+dumps(result))
