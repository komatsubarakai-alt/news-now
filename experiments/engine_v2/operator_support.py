"""Pure offline suggestions, not identity/progress decisions. No network client."""
import hashlib
import json
from pathlib import Path
from failed_response import digest
import revision_sync as sync

STAGES=('逮捕','送検','起訴','判決','施行','復旧')
NO_PROGRESS={'新しい事実がない':'no_new_facts','既報と同じ内容':'same_report','解説・振り返り記事':'commentary','既に記録済みの進展':'already_recorded'}

def materials(comp,task):
    """Statement comparison only: absence is unknown, not contrary evidence."""
    result=dict(label='ルールによる参考情報・未確定',matches=[],differences=[],unknown=[],next_checks=[])
    for row in comp.get('rows',[]):
        point={k:row[k] for k in ('field','label','status','existing','incoming')}
        result['matches' if row['status']=='一致' else 'differences' if row['status']=='相違' else 'unknown'].append(point)
        if row['status']=='未確認':result['next_checks'].append(row['label']+'を両側の引用で確認してください。情報がないことは相違ではありません。')
        elif row['status']=='相違':result['next_checks'].append(row['label']+'の相違が、別事件・改訂・時間経過のどれに当たるか確認してください。')
    if task.get('shared_event_ids'):result['next_checks'].insert(0,'共有記事です。対象事件の記述だけを確認し、URL一致で統合しないでください。')
    if task.get('source_changed') or task.get('edition_conflict'):result['next_checks'].insert(0,'現在の記事版と引用を確認し、古い確認結果を流用しないでください。')
    left={q for _,q in comp.get('existing_facts',[])};right={q for _,q in comp.get('incoming_facts',[])}
    # A different quotation can describe progression. It does not prove contradiction.
    result['fact_status']='一致' if left and left==right else '未確認'
    result['fact_note']='保存引用の文字列一致です。事件の同一性や真実性の証明ではありません。' if result['fact_status']=='一致' else '引用が不足、または記述が異なります。事実の一致・矛盾は未確認です。'
    result['next_checks'].append('事件の同一性を確認してから、進展の有無・引用・記録済みの進展を別に確認してください。')
    result['next_checks']=list(dict.fromkeys(result['next_checks']))
    return result

def previous_input(db,task,event_id):
    row=task.get('record')
    if not row or row['event_id']!=event_id or row['context_hash']!=task['review_contexts'][event_id]:return None
    if row['decision_id'] and db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(row['decision_id'],)).fetchone()!=('current',):return None
    ids=json.loads(row['evidence_ids'])
    if any(not sync.evidence_current(db,i) for i in ids):return None
    details=row.get('review_details',{});extra=details.get('progress_details') or {};codes=details.get('reason_codes',[])
    raw=row['reason'];prefix='／'.join(codes)
    free=raw[len(prefix)+1:] if prefix and raw.startswith(prefix+'／') else '' if raw==prefix else raw
    original=json.loads(db.execute('SELECT payload FROM sync_decisions WHERE decision_id=?',(row['decision_id'],)).fetchone()[0]) if row['decision_id'] else {}
    return dict(op=row['op'],recorded_at=row['recorded_at'],relation=row['relation'],progress=row['progress'],reason_codes=codes,reason_text=free,original_reason=raw,
        new_fact=extra.get('new_fact',''),difference=extra.get('difference',''),proposed_stage=original.get('proposed_stage'),
        status='保存済み入力・再確認が必要',confirmation_reused=False)

def cards(db,event_id,url):
    result=[]
    for proof,field,quote,sid in db.execute('SELECT evidence_id,field,quote,state_id FROM sync_evidence WHERE event_id=? AND url=? AND quote IS NOT NULL ORDER BY evidence_id',(event_id,url)):
        state=db.execute('SELECT version_id FROM sync_source_states WHERE state_id=?',(sid,)).fetchone()
        article=json.loads(db.execute('SELECT payload FROM sep_versions WHERE version_id=?',state).fetchone()[0])
        verification=sync.verify_quote(db,proof,current=True)
        current=sync.head(db,url)
        times={v[0] for v in db.execute("SELECT value FROM ui_comparison_notes WHERE event_id=? AND url=? AND field='occurred_at' AND evidence_id=?",(event_id,url,proof)) if v[0] in quote} if verification['status']=='exact_literal_match' else set()
        result.append(dict(event_id=event_id,evidence_id=proof,article=article,version_id=state[0],quote=quote,field=field,verification=verification,
            availability=current['availability'],body_available=bool(current['body_id'] and current['components']['body']),source_verification='未検証',event_occurred_at=None,event_occurrence_text=next(iter(times)) if len(times)==1 else None))
    return result

def build(db,task,event):
    eid=event['event_id'];url=task['article']['url'];proofs=cards(db,eid,url)
    valid=[p for p in proofs if p['verification']['status']=='exact_literal_match']
    usable={p['evidence_id']:p for p in valid if task.get('source') and p['version_id']==task['source']['version_id']}
    reused=[]
    for did,payload,status,outcome in db.execute("SELECT d.decision_id,d.payload,s.status,d.outcome FROM sync_decisions d JOIN sync_decision_state s USING(decision_id) WHERE d.event_id=? AND d.kind='ai' ORDER BY d.rowid DESC",(eid,)):
        deps=[r[0] for r in db.execute('SELECT evidence_id FROM sync_dependencies WHERE decision_id=?',(did,))]
        if status!='current' or outcome not in ('accepted','same_event','different_event') or not set(deps)&set(usable) or any(not sync.evidence_current(db,i) for i in deps):continue
        value=json.loads(payload)
        if not isinstance(value,dict):continue
        cited=value.get('progress_evidence',[])
        if not isinstance(cited,list):continue
        # Only reuse a literal quote already scoped to this event and current URL.
        matched=[p for p in usable.values() if any(isinstance(c,dict) and c.get('url')==url and c.get('quote')==p['quote'] for c in cited)]
        if matched:reused.append(dict(decision_id=did,stage=value.get('current_stage') if isinstance(value.get('current_stage'),str) else None,reason=value.get('reason')[:1000] if isinstance(value.get('reason'),str) else None,evidence_ids=[p['evidence_id'] for p in matched]))
    stage_candidates=sorted({s for s in STAGES if any(s in p['quote'] for p in valid)})
    suggested=next((r['stage'] for r in reused if r['stage'] in stage_candidates),None) or (stage_candidates[0] if len(stage_candidates)==1 else None)
    source=next((p for r in reused for p in valid if p['evidence_id'] in r['evidence_ids'] and suggested and suggested in p['quote']),None)
    source=source or next((p for p in sorted(valid,key=lambda p:(p['field']!='body',len(p['quote']),p['evidence_id'])) if suggested and suggested in p['quote']),None)
    comp=task['comparison_by_event'].get(eid,{})
    matches=[r['label']+'の保存記述が一致（提案・未確認）' for r in comp.get('rows',[]) if r['status']=='一致']
    differences=[r['label']+'の保存記述が相違（別事件の可能性・未確認）' for r in comp.get('rows',[]) if r['status']=='相違']
    if len(task.get('shared_event_ids',[]))>1:differences.append('共有記事です。事件ごとの引用を分け、URL一致では統合しません。')
    duplicate=[]
    for i,row in enumerate(event.get('progress_history',[])):
        if row.get('stage') in stage_candidates:
            literal=any(isinstance(q,dict) and q.get('quote') and q['quote'] in p['quote'] for q in row.get('progress_evidence',[]) for p in valid)
            duplicate.append(dict(index=i,stage=row.get('stage'),kind='literal_overlap' if literal else 'same_stage_only',status='重複候補・未確認'))
    result=dict(status='提案・未確認',method='saved_ai_and_literal_rules' if reused else 'literal_rules_only',ai_called=False,reused_ai=reused,
        same_event_reasons=matches or ['一致を裏付ける項目別引用は未確認です。'],different_event_reasons=differences or ['別事件でないことは確認できていません。'],
        proposed_stage=suggested,stage_candidates=stage_candidates,new_fact=source['quote'] if source else '',difference=('保存記録「'+str(event.get('current_stage') or '未確認')+'」と引用中の「'+suggested+'」を比較してください。時系列・進展は未確認です。') if suggested else '',
        evidence_ids=[source['evidence_id']] if source else [],duplicate_candidates=duplicate,event_occurred_at=None,evidence_cards=proofs,
        context_hash=task['review_contexts'][eid],event_id=eid,article_url=url,article_version=task.get('source'),scope='unconfirmed_operator_support_only',
        materials=materials(comp,task),previous_input=previous_input(db,task,eid))
    result['draft_id']=digest(dict(value=result,code=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))
    return result

def audit(task,command,details):
    selected=command.get('draft_review')
    details['no_progress_types']=[NO_PROGRESS[c] for c in details['reason_codes'] if c in NO_PROGRESS] if command.get('progress')=='none' else []
    if selected is None:
        if command.get('copied_review_op') is not None:raise ValueError('再利用元の下書きを確認してください。')
        return
    if not isinstance(selected,dict) or set(selected)!={'draft_id','applied_fields'}:raise ValueError('下書きの確認記録を確認してください。')
    draft=task.get('drafts_by_event',{}).get(command.get('event_id'))
    allowed={'new_fact','difference','proposed_stage'}
    fields=selected['applied_fields']
    if not draft or selected['draft_id']!=draft['draft_id']:raise ValueError('下書きや根拠が変わりました。再確認してください。')
    copied=command.get('copied_review_op')
    if copied is not None:
        previous=draft.get('previous_input')
        if not isinstance(copied,str) or not previous or copied!=previous['op']:raise ValueError('前回の入力が変更・失効しています。再確認してください。')
        details['review_reuse']=dict(source=previous,confirmation_reused=False)
    if not isinstance(fields,list) or any(not isinstance(f,str) or f not in allowed for f in fields) or len(fields)!=len(set(fields)):raise ValueError('取り込んだ下書き項目を確認してください。')
    extra=details.get('progress_details') or {}
    details['draft_review']=dict(original=draft,applied_fields=fields,operator_values={k:extra.get(k,'') if k!='proposed_stage' else command.get(k) for k in sorted(allowed)},
        operator_relation=command.get('relation'),operator_progress=command.get('progress'),status='運営者の確認記録・採用未実施')
