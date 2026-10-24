"""Ten explicitly fictional review scenarios. No real labels, fetches or AI calls."""
import copy
from datetime import datetime,timezone
from engine import dumps,events
from failed_response import digest
from article_store import version_id
from operator_demo import populate as original_demo
import revision_sync as sync
import operator_comparison as comparison

CASES=[
 ('01','同じ事件・進展なし','江別豊幌住宅強盗 被害者山田 逮捕の経緯を改めて報道','none'),
 ('02','逮捕から起訴へ','江別豊幌住宅強盗 被害者山田 容疑者を起訴','new'),
 ('03','別の事件','別市の別住宅強盗 被害者佐藤 容疑者を逮捕','different'),
 ('04','同じ事件か不明','住宅での事件について新たな発表','hold'),
 ('05','進展の引用不足','江別豊幌住宅強盗 被害者山田 手続きの新情報','missing'),
 ('06','すでに記録済みの起訴','江別豊幌住宅強盗 被害者山田 容疑者を起訴 続報','duplicate'),
 ('07','過去の報道の訂正の可能性','江別豊幌住宅強盗 被害者山田 逮捕報道を訂正','correction'),
 ('08','本文を取得できていない','江別豊幌住宅強盗 被害者山田 捜査の続報','body_missing'),
 ('09','記事改訂後の再確認','江別豊幌住宅強盗 被害者山田 逮捕の報道を更新','revision'),
 ('10','1記事と複数の事件','江別豊幌住宅強盗 被害者山田 と 別市の別住宅強盗 被害者佐藤 の比較','shared'),
]


def populate(db):
    original_demo(db)
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS ui_demo_meta(scope TEXT NOT NULL)')
        db.execute('INSERT INTO ui_demo_meta VALUES(?)',('synthetic_ten_cases',))
    at=datetime(2026,10,8,11,tzinfo=timezone.utc)
    base=copy.deepcopy(events(db)[0]);base['event_id']='demo-e2';base['event_name']='【架空】比較用の住宅強盗事件';base['current_stage']='逮捕'
    base['progress_history']=[];base['progress_count']=0;base['articles']=[dict(base['articles'][0],url='https://example.test/other-case')]
    with db:db.execute('INSERT INTO events VALUES(?,?)',('demo-e2',dumps(base)))
    sync.seed(db,base['articles'][0]);sync.add_evidence(db,'demo-e2',base['articles'][0]['url'],'title',base['articles'][0]['title'])
    third=copy.deepcopy(base);third['event_id']='demo-e3';third['event_name']='【架空】別市の住宅強盗事件';third['articles']=[dict(base['articles'][0],url='https://example.test/third-case',title='別市の別住宅強盗 被害者佐藤 容疑者を逮捕')]
    with db:db.execute('INSERT INTO events VALUES(?,?)',('demo-e3',dumps(third)))
    sync.seed(db,third['articles'][0])
    for number,label,title,kind in CASES:
        article=dict(title=title,url='https://example.test/demo-'+number,published='2026-10-08T11:00:00+00:00',source='架空のニュース媒体')
        body='【架空の検証用本文】'+title+'。出来事の発生日時は、この資料では未確認です。'
        sync.seed(db,article,body_text=None if kind=='body_missing' else body)
        group=dict(event_name='【架空】'+label,articles=[article],category='事件・事故',tracking_value='high')
        target='demo-e2' if kind in ('new','none','revision','shared') else 'demo-e1'
        scoped_title=title.split(' と ')[0] if kind=='shared' else title
        if kind not in ('hold','missing'):
            sync.add_evidence(db,target,article['url'],'title',scoped_title)
            if kind!='body_missing':sync.add_evidence(db,target,article['url'],'body',scoped_title)
        if kind=='revision':
            proof=sync.add_evidence(db,target,article['url'],'body',title)
            sync.record_reference(db,'demo-review-before-edit',target,'operator','same_event',{'scope':'fictional_reference_only'},[proof],at)
            sync.change_source(db,'demo-article-edited',article,sync.head(db,article['url'])['state_id'],at,body_text=body+' 記事の本文が改訂されました。')
        if kind=='shared':
            for eid in ('demo-e2','demo-e3'):
                event=next(e for e in events(db) if e['event_id']==eid);event['articles'].append(copy.deepcopy(article))
                with db:db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(event),eid))
                scoped=title.split(' と ')[0] if eid=='demo-e2' else title.split(' と ')[1].removesuffix(' の比較')
                ev=sync.add_evidence(db,eid,article['url'],'body',scoped)
                sync.record_reference(db,'demo-shared-'+eid,eid,'operator','same_event',{'scope':'fictional_reference_only'},[ev],at)
            sync.change_source(db,'demo-shared-body-edited',article,sync.head(db,article['url'])['state_id'],at,body_text=body+' 比較記事の本文が改訂されました。')
            for eid in ('demo-e2','demo-e3'):
                scoped=title.split(' と ')[0] if eid=='demo-e2' else title.split(' と ')[1].removesuffix(' の比較')
                sync.add_evidence(db,eid,article['url'],'body',scoped)
        payload=dict(status='manual_review',reason='【架空の操作確認】'+label,group=group)
        with db:
            for stage in ('tracking_triage','tracking_pending.json'):
                db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',(stage,'demo-case-'+number,dumps(payload) if stage=='tracking_triage' else dumps(group)))
    # Author-provided annotations for these fictional fixtures only. No news extraction.
    comparison.initialize(db)
    with db:
        for eid,url,quote,proof in db.execute('SELECT event_id,url,quote,evidence_id FROM sync_evidence WHERE quote IS NOT NULL').fetchall():
            for field,values in {'case_name':['江別豊幌住宅強盗','別市の別住宅強盗'],'place':['江別豊幌','別市'],'entities':['被害者山田','被害者佐藤'],'action':['住宅強盗']}.items():
                for value in values:
                    if value in quote and sync.verify_quote(db,proof,current=True)['status']=='exact_literal_match':comparison.add_note(db,eid,url,field,value,proof)
    # A saved simulated response demonstrates reuse; it is never adopted or billed.
    url='https://example.test/demo-02'
    proof,quote=db.execute("SELECT evidence_id,quote FROM sync_evidence WHERE event_id='demo-e2' AND url=? AND field='body' AND quote IS NOT NULL",(url,)).fetchone()
    sync.record_reference(db,'demo-saved-ai-draft','demo-e2','ai','same_event',
        {'reason':'【架空の保存済み模擬AI】事件名と人物の記述を比較する提案です。未確認です。','current_stage':'起訴','progress_evidence':[{'url':url,'quote':quote}]},[proof],at)
