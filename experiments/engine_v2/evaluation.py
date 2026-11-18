"""Curated stored-news pairs. Labels come from quoted source facts, never AI votes."""
import hashlib
import json
import sys
from pathlib import Path
from collections import Counter

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(Path(__file__).parent)]
from merge_safety import source_context, normalized
from ai_cost_control import json_answer
from event_tracker import TRACKING_RESPONSE

# Stable IDs, reference article index, incoming source event/index.
SAME=[
('MICE基本計画の費用報道','5d4bafaf-011f-4e26-9014-22a57b3fef2b',7,9,[('case','MICE'),('detail','597億円')], '同日、札幌MICE基本計画と597億円の事業費が一致'),
('副首都担当部の設置','de81a3e4-e37c-4423-ac06-9a5d26e7be7e',1,2,[('case','副首都整備担当部'),('detail','16日')], '同じ担当部の設置と16日付の予定が一致。公表日の違いでは除外しない'),
('厚別区大谷地の火災','c14a0e3e-653c-4b41-8000-b4d0597275ae',0,1,[('case','厚別区大谷地'),('detail','消防車など21台')], '同日・同一の固有見出し、所在地と消防車台数が一致'),
('アイヌ展示指針の会合','c2d01fb9-77ed-48ef-bc0c-8eab7285f4e9',4,5,[('case','アイヌ民族パネル展'),('detail','ガイドライン策定')], '同日の専門部会、特定パネル展とガイドライン策定が一致'),
('東西線車両不具合','78bcef99-1ca6-47fa-ae41-fd5c219b1d5b',0,1,[('case','東西線'),('detail','約22分')], '同日の回送列車不具合と最大約22分遅れの見出しが一致'),
('インフル注意報','09ca7666-428d-40df-bdc9-7d75a21acbe1',4,5,[('case','インフル注意報'),('detail','統計開始以来2番目')], '同日の同内容見出し。注意報と統計史上の位置づけが一致'),
('東区共同住宅火災','fab8ff4a-59a8-4e3f-b7d9-312007f89467',0,1,[('case','札幌市東区の3階建て共同住宅'),('detail','70代')], '同日の同内容見出し。3階建て共同住宅と70代の負傷者が一致'),
('ゲーム機窃盗','eea45021-9422-4ee0-b7cb-a709a57c2eb6',0,1,[('case','会員制大型量販店'),('detail','ゲーム機2台')], '同日の固有見出し。量販店、ゲーム機2台、27歳容疑者が一致'),
('知床沈没控訴審','cd3f5f7e-7ba9-4e72-886f-550866a8433d',0,1,[('case','知床沈没'),('detail','控訴審は12月初公判')], '同日の固有見出し。知床沈没事件と控訴審初公判が一致'),
]
DIFFERENT=[
('MICEと高校野球','5d4bafaf-011f-4e26-9014-22a57b3fef2b',7,'5ea068c9-841e-4f4d-a03e-f773ad6106f9',0,'会議施設の建設計画と高校野球の大会敗退は別の出来事'),
('厚別区と東区の別火災','c14a0e3e-653c-4b41-8000-b4d0597275ae',0,'fab8ff4a-59a8-4e3f-b7d9-312007f89467',0,'厚別区大谷地の住宅と東区の3階建て共同住宅。所在地・建物・報道日が異なる'),
('女性宅侵入と少女への暴行','b4674f16-08ac-4666-9038-b4718418f8ff',0,'fbecb958-e0f1-470b-a84c-37f73542972b',0,'20代女性宅への侵入と公園での10代前半少女への暴行。被害者・場所・行為が異なる'),
('給食と住宅強盗','91f93057-10f0-4a7b-b557-87023c0335da',1,'4002f5d8-6c3f-4678-afee-87597a7cb104',0,'学校給食の相談と住宅強盗容疑の再逮捕は別の出来事'),
('共同住宅火災と破産','fab8ff4a-59a8-4e3f-b7d9-312007f89467',0,'e7de3c44-9ece-4d4f-86e7-b18cfa1cd98d',0,'共同住宅の火災と特定生花店の破産手続きは別の出来事'),
('妻への暴行と車損壊','a344ef1c-cae5-424d-805f-ad8a69c27939',0,'16f6c9c4-65cc-4ffd-949d-6db78b0a707e',0,'29歳警察官による妻への暴行疑いと39歳男による車ガラス損壊。対象・行為・容疑者属性が異なる'),
('ゲーム機窃盗と工具窃盗','eea45021-9422-4ee0-b7cb-a709a57c2eb6',0,'b565866f-bfb4-4888-b054-97e69d71a3f2',0,'量販店のゲーム機と倉庫の工具。対象物・場所・容疑者の年齢が異なる'),
('MICE施設と斗南病院跡','5d4bafaf-011f-4e26-9014-22a57b3fef2b',7,'978be146-7669-4e02-8b03-c79ea6ac34f1',0,'中島公園駅接続のMICE施設と斗南病院跡の国有地入札。対象の土地・事業が異なる'),
('ゲート事故と足場転落','e98c5d08-4d42-4ae4-8b60-294778a05c75',1,'fb55e9b6-3f90-47ed-bf76-cdddb6a6076b',2,'浄水場ゲート倒壊・職員3人と足場解体・20代作業員の転落。施設・事故形態が異なる'),
]
MANUAL=[
('アレフ施設の検査','c3cff280-0e7c-4e41-a21c-f3a36fc40db6',0,'c3cff280-0e7c-4e41-a21c-f3a36fc40db6',1,'同じ団体・施設でも反復検査の可能性。見出しだけで個別の検査日を確定しない'),
('時期の異なる大雨','5476859f-fa90-4ad0-a0ff-f12c0115af0c',0,'f5c1d0e9-a503-5f99-b2d0-994c96ce14ec',0,'異なる警報段階・報道日。同一災害の継続か別の気象事象かは要確認'),
('複数強盗と再逮捕','5c4bb2fd-3d87-491c-be56-825ca2674374',0,'4002f5d8-6c3f-4678-afee-87597a7cb104',0,'複数の強盗事件を列挙。人物一致だけで一つの事件に統合しない'),
('一記事に二事故','e72f1865-5261-49b1-ba55-ae5e7ac47a00',0,'719c979d-347a-5455-a5da-55aefe678081',0,'同じURLの記事が厚岸の転落と札幌北区の事故を含む。記事全体の同一性を案件同一性にしない'),
('ゲート事故の速報と詳細','e98c5d08-4d42-4ae4-8b60-294778a05c75',0,'e98c5d08-4d42-4ae4-8b60-294778a05c75',1,'重量・人数は一致するが速報の施設名が不明。直接の続報かを本文等で確認'),
('給食相談の長期報道','91f93057-10f0-4a7b-b557-87023c0335da',0,'91f93057-10f0-4a7b-b557-87023c0335da',1,'政策と相談というテーマの連続性だけでは同一出来事・進展を確定しない'),
('流行入りと注意報','09ca7666-428d-40df-bdc9-7d75a21acbe1',2,'09ca7666-428d-40df-bdc9-7d75a21acbe1',3,'流行入りと注意報は状態が違う。同じ流行の進展かは要確認'),
('タクシー買収と新ブランド','e07d39ee-7cf2-4895-a948-14fd727c0d76',0,'e07d39ee-7cf2-4895-a948-14fd727c0d76',2,'企業買収とブランド営業開始は関連しても別の事実。対象範囲を要確認'),
('警察官暴行の媒体差','a344ef1c-cae5-424d-805f-ad8a69c27939',0,'a344ef1c-cae5-424d-805f-ad8a69c27939',4,'見出しには年齢がない組がある。所属と行為だけで別容疑者を排除しない'),
('MICE概要と議会懸念','5d4bafaf-011f-4e26-9014-22a57b3fef2b',0,'5d4bafaf-011f-4e26-9014-22a57b3fef2b',9,'同じ計画の公表と議会審議の関係は要確認。テーマだけで進展を確定しない'),
]


def build(root,revision):
    events=json.loads((root/'events.json').read_text());byid={e['event_id']:e for e in events}
    rows=[]
    def add(name,target,old,new,label,reason,anchors=(),saved=None):
        left,right=source_context(old),source_context(new)
        for kind,value in anchors:
            if normalized(value) not in normalized(left['title']) or normalized(value) not in normalized(right['title']):
                raise AssertionError('curated anchor absent: '+name+' '+value)
        row={'sample_id':'real-'+str(len(rows)+1).zfill(3),'name':name,'label':label,
             'target_event_id':target,
             'stored_incoming_event_ids':[e['event_id'] for e in events if any(a['url']==new['url'] for a in e.get('articles',[]))],
             'stored_reference_event_ids':[e['event_id'] for e in events if any(a['url']==old['url'] for a in e.get('articles',[]))],
             'reference_article':old,'incoming_article':new,
             'reference_quote':left['title'],'incoming_quote':right['title'],
             'anchors':[{'kind':k,'value':v} for k,v in anchors], 'reason':reason,
             'snapshot_revision':revision,'basis':'stored_headlines_and_metadata_only',
             'reviewer':'Codex source-text review; no external article retrieval',
             'independent_human_review':'not_completed','saved_response':saved}
        if not left['url'] or not right['url']:raise AssertionError('missing URL')
        row['evidence_sha256']=hashlib.sha256(json.dumps([old,new],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        rows.append(row)
    for name,eid,a,b,anchors,reason in SAME:
        add(name,eid,byid[eid]['articles'][a],byid[eid]['articles'][b],'same_event',reason,anchors)
    for name,eid,a,other,b,reason in DIFFERENT:
        add(name,eid,byid[eid]['articles'][a],byid[other]['articles'][b],'different_event',reason)
    for name,eid,a,other,b,reason in MANUAL:
        add(name,eid,byid[eid]['articles'][a],byid[other]['articles'][b],'manual_review',reason)
    pool={a['url']:a for e in events for a in e.get('articles',[])}
    for name in ('tracking_pending.json','news_status.json'):
        for g in json.loads((root/name).read_text()):
            for a in g.get('articles',[]):pool[a['url']]=a
    cache=json.loads((root/'ai_response_cache.json').read_text());seen=set();replay=0
    for payload in sorted(cache.values(),key=lambda p:p.get('created_at',0),reverse=True):
        try:answer=json_answer(payload)
        except (ValueError,TypeError,KeyError):continue
        if not set(TRACKING_RESPONSE)<=set(answer) or answer.get('event_id') not in byid:continue
        for match in answer.get('identity_matches',[]):
            uid,ref=match.get('incoming_url'),match.get('existing_url');key=(answer['event_id'],uid)
            if key in seen or uid not in pool or ref not in pool:continue
            seen.add(key)
            add('保存済みAI回答の再生'+str(replay+1),answer['event_id'],pool[ref],pool[uid],'manual_review',
                'AI回答は正解ではない。独立した同一性確認が必要。再生は新プロンプトの実回答やキャッシュヒットとは扱わない',saved=answer)
            replay+=1
            break
        if replay>=8:break
    return {'snapshot_revision':revision,'label_counts':dict(Counter(r['label'] for r in rows)),
            'label_scope':'provisional source-text labels, not externally verified reporting accuracy',
            'selection':'small purpose-selected sample; many positives are same-report reprints',
            'rows':rows}


def markdown(data):
    lines=['# 実ニュース評価表','',f"基準main: `{data['snapshot_revision']}`。保存済み見出しの明確な組を暫定正解とし、曖昧な組は要手動確認。既存AIの判断は正解にしていません。本文・外部事実・独立した人の確認は未実施です。",'',
           '|番号|分類|対象事件ID|記事と根拠|理由|','|---|---|---|---|---|']
    labels={'same_event':'同一（暫定正解）','different_event':'別事件（暫定正解）','manual_review':'要手動確認'}
    for r in data['rows']:
        def esc(s):return s.replace('|','\\|').replace('\n',' ')
        links=f"[既存記事]({r['reference_article']['url']})：{esc(r['reference_quote'])}<br>[評価記事]({r['incoming_article']['url']})：{esc(r['incoming_quote'])}"
        lines.append(f"|{r['sample_id']}|{labels[r['label']]}|{r['target_event_id']}|{links}|{esc(r['reason'])}|")
    return '\n'.join(lines)+'\n'
