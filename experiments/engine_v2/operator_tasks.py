"""Human work projection and reference-only two-step review. Never adopts facts."""
import json
from collections import defaultdict
from engine import atomic, dumps
from failed_response import digest, event_conditions
from article_store import version_id
import revision_sync as sync
import publication as pub
import operator_comparison as comparison

SCHEMA='''
CREATE TABLE IF NOT EXISTS ui_task_reviews(op TEXT PRIMARY KEY,task_id TEXT NOT NULL,context_hash TEXT NOT NULL,event_id TEXT REFERENCES events,relation TEXT NOT NULL,progress TEXT,correction TEXT NOT NULL,reason TEXT NOT NULL,evidence_ids TEXT NOT NULL,decision_id TEXT REFERENCES sync_decisions,recorded_at TEXT NOT NULL,event_occurred_at TEXT,progress_assessment TEXT NOT NULL,article_payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_correction_alerts(op TEXT PRIMARY KEY REFERENCES ui_task_reviews,task_id TEXT NOT NULL,event_id TEXT REFERENCES events,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_review_details(op TEXT PRIMARY KEY REFERENCES ui_task_reviews,payload TEXT NOT NULL);
'''
HUMAN={'manual_review','needs_evidence','editorial_hold'}
TERMINAL={'out_of_scope','low_priority','already_saved','accepted','resolved','completed'}
PROGRESS={'new','none','unknown','possible_correction'}


def initialize(db):
    with atomic(db):
        comparison.initialize(db)
        for statement in SCHEMA.split(';'):
            if statement.strip():db.execute(statement)


def head(db,url):
    if not db.execute('SELECT 1 FROM sync_heads WHERE url=?',(url,)).fetchone():return None
    return sync.head(db,url)


def evidence(db,eid,url=None):
    rows=db.execute('SELECT evidence_id,url,field,quote FROM sync_evidence WHERE event_id=? ORDER BY evidence_id',(eid,)).fetchall()
    return [dict(evidence_id=i,url=u,field=f,quote=q,verification=sync.verify_quote(db,i,current=True)) for i,u,f,q in rows if url is None or u==url]


def quote_ids(db,eid,url):
    return [r['evidence_id'] for r in evidence(db,eid,url) if r['quote'] and r['verification']['status']=='exact_literal_match']


def refs(db,task_id):
    keys=('op','task_id','context_hash','event_id','relation','progress','correction','reason','evidence_ids','decision_id','recorded_at','event_occurred_at','progress_assessment','article_payload')
    records=[dict(zip(keys,row)) for row in db.execute('SELECT * FROM ui_task_reviews WHERE task_id=? ORDER BY rowid DESC',(task_id,))]
    for r in records:
        extra=db.execute('SELECT payload FROM ui_review_details WHERE op=?',(r['op'],)).fetchone()
        if extra:r['review_details']=json.loads(extra[0])
    return records


def records_current(db,row,context):
    if row['context_hash']!=context:return False
    if not row['decision_id']:return True
    return db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(row['decision_id'],)).fetchone()==('current',)


def worklist(db,state):
    """One article edition per task; known multi-event owners remain separate.

    Ranking alone never proves identity. Untriaged/queued rows are AI waiting,
    not human tasks. Historical terminal triage rows cannot inflate the badge.
    """
    events={i['event']['event_id']:i for i in state['items']}; grouped={}; selected_sources={}
    for q in state['queues']:
        for article in q['group']['articles']:
            key=article['url']
            if key not in selected_sources:
                current=head(db,key)
                ancestors=set()
                if current:
                    sid=current['state_id']
                    while sid:
                        row=db.execute('SELECT version_id,parent_id FROM sync_source_states WHERE state_id=?',(sid,)).fetchone()
                        ancestors.add(row[0]);sid=row[1]
                selected_sources[key]=(current,ancestors)
            current,ancestors=selected_sources[key]
            bound=bool(current and version_id(article) in ancestors)
            displayed=json.loads(db.execute('SELECT payload FROM sep_versions WHERE version_id=?',(current['version_id'],)).fetchone()[0]) if bound else article
            unit=grouped.setdefault(key,dict(article=displayed,sources=[],candidates={},variants={},source_conflict=False))
            if current and not bound:unit['source_conflict']=True
            unit['variants'][version_id(article)]=article
            unit['sources'].append(dict(queue_id=q['queue_id'],stage=q['stage'],status=q['source_status'],reason=q['source_reason'],original_article=article))
            for c in q['candidates']:
                old=unit['candidates'].get(c['event_id'])
                if old is None or c['rank']<old['rank']:unit['candidates'][c['event_id']]=c
    tasks=[];completed=[];ai_waiting=0;excluded=0
    for key,unit in sorted(grouped.items()):
        a=unit['article'];url=a['url'];source=head(db,url)
        owners=[eid for eid,i in events.items() if any(x['url']==url for x in i['event'].get('articles',[]))]
        edition_conflict=unit['source_conflict'] or not source and len(unit['variants'])>1
        changed=[]
        for eid in owners:
            saved=next(x for x in events[eid]['event']['articles'] if x['url']==url)
            if version_id(saved)!=version_id(a) or (source and source['version_id']!=version_id(saved)) or any(j['status']=='needs_recheck' for j in events[eid]['judgments']):changed.append(eid)
        stale_source=any(r['verification']['status']=='stale_source' for eid in events for r in evidence(db,eid,url))
        statuses={x['status'] for x in unit['sources'] if x['stage']=='tracking_triage'}
        human=bool(statuses&HUMAN)
        latest=[rows[0] for owner in (owners if len(owners)>1 else [None]) if (rows:=refs(db,digest(['article-review',key,owner])))]
        active_review=any(r['relation']=='hold' or r['correction']=='possible' or r['progress_assessment']=='needs_evidence' or r['relation']=='same_event' and r['progress'] is None or r['decision_id'] and db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(r['decision_id'],)).fetchone()!=('current',) for r in latest)
        if owners and not changed and not stale_source and not edition_conflict and not active_review:
            # No work for already-adopted unchanged editions, even if stale queue rows remain.
            excluded+=1;continue
        if not human and not changed and not stale_source and not edition_conflict and not active_review:
            if statuses&TERMINAL:excluded+=1
            else:ai_waiting+=1
            continue
        targets=owners if len(owners)>1 else [None]
        for required in targets:
            candidates=sorted(unit['candidates'].values(),key=lambda c:(c['rank'],c['event_id']))
            # Include an owner omitted by the search without pretending it is safe to merge.
            for eid in owners:
                if eid not in {c['event_id'] for c in candidates}:candidates.append(dict(event_id=eid,rank=len(candidates)+1,score=0,reasons={'known_owner_requires_review':True}))
            task_id=digest(['article-review',key,required])
            history=refs(db,task_id);record=history[0] if history else None
            formal=next((c['event_id'] for c in candidates if c.get('reasons',{}).get('operator_target')),None)
            suggested=required or formal or next((c['event_id'] for c in candidates if quote_ids(db,c['event_id'],url)),None) or (candidates[0]['event_id'] if candidates else None)
            source_context={k:v for k,v in source.items() if k!='state_id'} if source else None
            epochs=list(db.execute('SELECT component,epoch FROM sync_component_epochs WHERE url=? ORDER BY component',(url,)))
            contexts={}
            for selected in [None]+[c['event_id'] for c in candidates]:
                proof_state=[(r['evidence_id'],r['verification']) for r in evidence(db,selected,url) if r['quote']] if selected else []
                contexts[selected or 'none']=digest(dict(article=a,variants=sorted(unit['variants']),source=source_context,epochs=epochs,
                    selected_event=event_conditions([events[selected]['event']]) if selected in events else None,proof=proof_state,comparison=comparison.comparison(db,events[selected]['event'],url) if selected in events else None,required=required,owners=owners))
            context=contexts[(record['event_id'] or 'none') if record else (suggested or 'none')]
            current=bool(record and records_current(db,record,context))
            category='identity';reason='同じ事件か、安全に決める根拠を人が確認する必要があります。';priority=2
            if edition_conflict or (changed or stale_source) and not current or (record and not current):category='hold';reason='記事や根拠が変わったため、以前の確認を見直してください。';priority=0
            elif any(word in a.get('title','') for word in ('訂正','撤回','取り消','誤報')) and not current:
                category='hold';reason='見出しに訂正・撤回を示す語があります。通常の進展と分けて確認してください。';priority=0
            elif 'editorial_hold' in statuses and not current:
                category='hold';reason='運営者の保留指定があります。解除してよい根拠を確認してください。';priority=1
            elif current:
                if record['relation']=='hold' or record['progress'] in ('unknown','possible_correction') or record['progress_assessment'] in ('needs_evidence','possible_correction') or record['correction']=='possible':
                    category='hold';reason='訂正・撤回の可能性を確認してください。' if record['correction']=='possible' else '前回の判断で保留になっています。';priority=0 if record['correction']=='possible' else 1
                elif record['relation']=='same_event' and record['progress'] is None:
                    category='progress';reason='同じ事件かは確認済みです。新しい進展があるか確認してください。';priority=2
                else:
                    completed.append(dict(task_id=task_id,article=a,record=record,history=history,candidate_ids=[c['event_id'] for c in candidates],label='確認記録済み・安全検証待ち'));continue
            formal=next((c['event_id'] for c in candidates if c.get('reasons',{}).get('operator_target')),None)
            suggested=required or formal or next((c['event_id'] for c in candidates if quote_ids(db,c['event_id'],url)),None) or (candidates[0]['event_id'] if candidates else None)
            incoming_evidence=evidence(db,suggested,url) if suggested else []
            ai=[]
            for c in candidates:
                for j in events.get(c['event_id'],{}).get('judgments',[]):
                    if j['kind']=='ai' and db.execute('SELECT 1 FROM sync_dependencies d JOIN sync_evidence e USING(evidence_id) WHERE d.decision_id=? AND e.url=?',(j['decision_id'],url)).fetchone():ai.append(dict(event_id=c['event_id'],decision_id=j['decision_id'],outcome=j['outcome'],status=j['status']))
            tasks.append(dict(task_id=task_id,context_hash=context,review_contexts=contexts,article=a,source=source,category=category,priority=priority,reason=reason,candidates=candidates,suggested_event_id=suggested,required_event_id=required,formal_event_id=formal,shared_event_ids=owners if len(owners)>1 else [],sources=unit['sources'],evidence=incoming_evidence,evidence_by_event={c['event_id']:evidence(db,c['event_id'],url) for c in candidates},ai_records=ai,record=record,history=history,edition_conflict=edition_conflict,article_variants=list(unit['variants'].values()),fetch_wait=db.execute('SELECT reason FROM sync_fetch_waits WHERE url=?',(url,)).fetchone(),correction_hint=any(word in a.get('title','') for word in ('訂正','撤回','取り消','誤報')),source_changed=bool(changed or stale_source)))
    # A stored same-event review without a second-stage choice needs progress review.
    for t in tasks:
        r=t['record']
        if r and records_current(db,r,t['context_hash']) and r['relation']=='same_event' and r['progress'] is None:
            t.update(category='progress',reason='同じ事件かは確認済みです。新しい進展があるか確認してください。',priority=2)
    public=[]
    for eid,item in events.items():
        pending=[t for t in item['targets'] if t['status']!='approved']
        guarded=db.execute("SELECT 1 FROM sync_adoptions a JOIN sync_decision_state s USING(decision_id) WHERE a.event_id=? AND a.cancelled=0 AND s.status='current'",(eid,)).fetchone()
        recheck=any(t['status']=='needs_recheck' for t in pending)
        # Existing legacy publications alone never create 34/407 fresh human tasks.
        operator_pending=db.execute("SELECT 1 FROM sync_decisions d JOIN sync_decision_state s USING(decision_id) WHERE d.event_id=? AND d.kind='operator' AND (s.status='needs_recheck' OR s.status='current' AND d.outcome IN ('hold','different_event'))",(eid,)).fetchone()
        if pending and (guarded or recheck or operator_pending):
            category='hold' if recheck or operator_pending else 'publication'
            public.append(dict(task_id='publish:'+eid,event_id=eid,category=category,priority=0 if recheck or operator_pending else 3,reason='根拠の変更や運営者の保留があるため、公開前に再確認してください。' if recheck or operator_pending else '安全検証を通った内容を、公開前に確認してください。',target_count=len(pending)))
    # If a revision already has an article task, show its publication hold there.
    for p in public:
        related=next((t for t in tasks if t['category']=='hold' and (t['required_event_id']==p['event_id'] or t['suggested_event_id']==p['event_id'] or (t.get('record') or {}).get('event_id')==p['event_id'])),None)
        if p['category']=='hold' and related:related.setdefault('publication_rechecks',[]).append(p)
        else:tasks.append(p)
    for t in tasks:
        if 'article' in t:
            t['comparison_by_event']={c['event_id']:comparison.comparison(db,events[c['event_id']]['event'],t['article']['url']) for c in t['candidates'] if c['event_id'] in events}
    tasks.sort(key=lambda t:(t['priority'],t['task_id']))
    counts={k:sum(t['category']==k for t in tasks) for k in ('identity','progress','publication','hold')}
    review_history=[]
    for task_id, in db.execute('SELECT DISTINCT task_id FROM ui_task_reviews ORDER BY task_id'):
        review_history.extend(refs(db,task_id))
    return dict(counts=counts,tasks=tasks,completed=completed,review_history=review_history,ai_waiting=ai_waiting,excluded_history=excluded,unit='同じURLの現在の記事版を確認する作業。版不明は1件の照合保留。共有記事は事件別、公開確認は事件ごと。')


def review(db,state,command,at):
    details=comparison.review_details(command)
    task=next((t for t in state['work']['tasks'] if t['task_id']==command.get('task_id') and 'article' in t),None)
    if task is None:raise ValueError('この確認作業は完了済みか変更されています。再読込してください。')
    if command.get('context_hash')!=task['context_hash']:raise ValueError('記事や根拠が変わりました。再確認してください。')
    eid=command.get('event_id');relation=command.get('relation');progress=command.get('progress');reason=command.get('reason')
    if relation not in ('same_event','different_event','hold'):raise ValueError('同じ事件かを選んでください。')
    if relation!='same_event' and progress is not None:raise ValueError('進展の判断は「同じ事件」の場合だけ記録できます。')
    if progress is not None and progress not in PROGRESS:raise ValueError('進展の選択を確認してください。')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('判断・保留の理由を入力してください。')
    if eid is None and relation!='hold':raise ValueError('比較する事件を選んでください。')
    if eid is not None and eid not in [c['event_id'] for c in task['candidates']]:raise ValueError('照合していない事件は選べません。')
    if task['required_event_id'] and eid!=task['required_event_id']:raise ValueError('共有記事は対象の事件ごとに確認してください。')
    if task['formal_event_id'] and relation=='same_event' and eid!=task['formal_event_id']:raise ValueError('既存の運営者指定先と異なります。保留して指定を確認してください。')
    if task.get('edition_conflict') and relation!='hold':raise ValueError('同じURLの複数版があり現在版を確認できません。保留してください。')
    if relation!='hold' and (task.get('fetch_wait') or task.get('source',{} ) and task['source']['availability'] in ('deleted','empty_body')):raise ValueError('記事の削除・取得状態を確認できません。保留してください。')
    quotes=quote_ids(db,eid,task['article']['url']) if eid else []
    if relation!='hold' and not quotes:raise ValueError('保存された引用が不足しています。判断できない・保留を選んでください。')
    occurred=command.get('event_occurred_at')
    if occurred is not None:
        from datetime import datetime
        if not isinstance(occurred,str):raise ValueError('出来事の日時を確認してください。')
        parsed=datetime.fromisoformat(occurred.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError('出来事の日時にはタイムゾーンが必要です。')
    assessment={'none':'operator_reported_no_change','unknown':'needs_evidence','possible_correction':'possible_correction'}.get(progress,'not_reviewed')
    proposed=command.get('proposed_stage')
    if proposed is not None and (not isinstance(proposed,str) or not proposed.strip() or len(proposed)>80):raise ValueError('進展段階の入力を確認してください。')
    if progress=='new':
        incoming_quotes=[r['quote'] for r in evidence(db,eid,task['article']['url']) if r['evidence_id'] in quotes]
        if not proposed or not any(proposed in q for q in incoming_quotes):assessment='needs_evidence'
        else:
            assessment='operator_reported_new'
            event=next(i['event'] for i in state['items'] if i['event']['event_id']==eid)
            for old in event.get('progress_history',[]):
                if old.get('stage')!=proposed:continue
                # Literal repeated fact is a duplicate candidate, never a new counted progression.
                if any(p.get('quote') and p['quote'] in q for p in old.get('progress_evidence',[]) for q in incoming_quotes):assessment='already_recorded_literal_candidate'
                elif assessment!='already_recorded_literal_candidate':assessment='needs_evidence'
        extra=details['progress_details']
        if extra is not None:
            if any(i not in quotes for i in extra.get('evidence_ids',[])):raise ValueError('別事件・別記事版の引用は使えません。')
            selected=[r['quote'] for r in evidence(db,eid,task['article']['url']) if r['evidence_id'] in extra.get('evidence_ids',[])]
            if not extra.get('new_fact','').strip() or not any(extra['new_fact'] in q and proposed and proposed in q for q in selected) or not extra.get('difference','').strip() or not extra.get('duplicate_checked'):assessment='needs_evidence'
    correction_possible=progress=='possible_correction' or progress=='new' and task.get('correction_hint',False)
    if correction_possible:assessment='possible_correction'
    op=command['op'];ids=list(quotes);ref=None
    if eid:
        # Watch the changed edition, including unprovided body as an explicit dependency.
        if head(db,task['article']['url']):
            for field in ('metadata','body','availability'):ids.append(sync.add_evidence(db,eid,task['article']['url'],field))
        previous=next((r for r in refs(db,task['task_id']) if r['event_id']==eid),None)
        blocked=relation=='hold' or progress in ('unknown','possible_correction') or assessment in ('needs_evidence','possible_correction')
        ref=sync.record_reference(db,'ui-task:'+op,eid,'operator','hold' if blocked else relation,
            dict(scope='operator_review_only',relation=relation,progress_review=progress,proposed_stage=proposed,progress_assessment=assessment,new_progress_counted=False,correction_review='possible' if correction_possible else 'not_requested',reason=reason,review_details=details,article=task['article'],source_context_hash=task['review_contexts'][eid or 'none'],event_occurred_at=occurred,occurred_at_verification='operator_reported_not_verified'),
            ids,at,supersedes=previous['decision_id'] if previous else None)
    correction='possible' if correction_possible else 'not_requested'
    db.execute('INSERT INTO ui_task_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(op,task['task_id'],task['review_contexts'][eid or 'none'],eid,relation,progress,correction,reason,dumps(ids),ref['decision_id'] if ref else None,at.isoformat(),occurred,assessment,dumps(task['article'])))
    db.execute('INSERT INTO ui_review_details VALUES(?,?)',(op,dumps(details)))
    if correction=='possible':
        db.execute('INSERT INTO ui_correction_alerts VALUES(?,?,?,?)',(op,task['task_id'],eid,dumps(dict(article=task['article'],reason=reason,scope='unverified_correction_alert_not_published'))))
    return dict(status='recorded_reference_only',decision_id=ref['decision_id'] if ref else None,relation=relation,progress=progress,correction=correction,progress_assessment=assessment,new_progress_counted=False,adopted=False,published=False)


def bundle(db,state,command,at,preview=False,failpoint=None):
    eid=command.get('event_id');item=next((i for i in state['items'] if i['event']['event_id']==eid),None)
    if item is None:raise ValueError('事件が見つかりません。')
    selected=command.get('targets');reason=command.get('reason')
    if not isinstance(selected,list) or not selected or len(selected)>200:raise ValueError('確認する公開項目を選んでください。')
    if not preview and command.get('confirmed') is not True:raise ValueError('内容を確認してから実行してください。')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('確認した理由を入力してください。')
    seen=set();results=[]
    for index,s in enumerate(selected):
        if not isinstance(s,dict):raise ValueError('対象の形式が不正です。')
        identity=(s.get('kind'),s.get('key'))
        if identity in seen:raise ValueError('同じ公開項目が重複しています。')
        seen.add(identity)
        target=next((t for t in item['targets'] if (t['kind'],t['key'])==identity),None)
        if not target or s.get('content_hash')!=target['content_hash']:raise ValueError('公開対象が変更されました。再確認してください。')
        if target['status']=='approved':raise ValueError('すでに承認済みです。再読込してください。')
        result=pub.approve(db,'ui-bundle:'+command['op']+':'+str(index),eid,target['kind'],target['key'],
            expected_hash=target['content_hash'],actor='simulation:local-operator',at=at,evidence_ids=target['evidence_ids'],decision_ids=target['decision_ids'],basis=reason,supersedes=target['approval_id'])
        results.append(dict(kind=target['kind'],key=target['key'],evidence_ids=target['evidence_ids'],decision_ids=target['decision_ids'],result=result))
        if failpoint=='after_first' and index==0:raise RuntimeError('synthetic bundle failure')
    return dict(scope='hypothetical_not_approved' if preview else 'isolated_simulation',results=results,events=pub.project(db,{})['events'])


def reopen(db,state,command,at):
    tid=command.get('task_id');previous=next((r for r in state['work']['completed'] if r['task_id']==tid),None)
    if previous is None:raise ValueError('確認済みの作業を選んでください。')
    old=previous['record'];reason=command.get('reason')
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('見直す理由を入力してください。')
    eid=old['event_id'];ids=json.loads(old['evidence_ids']);did=None
    if eid:
        ref=sync.record_reference(db,'ui-reopen:'+command['op'],eid,'operator','hold',dict(scope='operator_review_only',reopens=old['op'],reason=reason,article=json.loads(old['article_payload'])),ids,at,supersedes=old['decision_id'])
        did=ref['decision_id']
    db.execute('INSERT INTO ui_task_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(command['op'],tid,old['context_hash'],eid,'hold',None,'not_requested',reason,old['evidence_ids'],did,at.isoformat(),None,'not_reviewed',old['article_payload']))
    return dict(status='recorded_reference_only',reopened=tid,adopted=False,published=False)
