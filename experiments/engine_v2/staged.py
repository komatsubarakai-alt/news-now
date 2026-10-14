"""Offline bounded batches. Unknown/rejected is unresolved, never 'different'."""
import hashlib
import json
from pathlib import Path

from ai_cost_control import BudgetExceeded
from event_tracker import analysis_prompt, TRACKING_RESPONSE
from failed_response import digest, event_conditions, validation_revision
from operator_review import active_selection
from pending_triage import fingerprint, group_key, preflight
from engine import atomic, dumps, events, rank, judge, reserve_mock


def prior_attempts(group,triage):
    item=triage.get(group_key(group),{})
    return item.get('decision_attempts',0) if item.get('source_fingerprint')==fingerprint(group) else 0


def context(db,group,triage,registry,batch_size):
    return {'group':group,'events':event_conditions(events(db)),
            'selection':active_selection(group,triage),'registry':registry,
            'source_history':triage.get(group_key(group),{}).get('source_history',[]),
            'prior_decision_attempts':prior_attempts(group,triage),
            'validator':validation_revision('tracking'),'model':'gpt-5.4-mini','output_limit':1200,
            'stage_code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'engine_code':hashlib.sha256(Path(__file__).with_name('engine.py').read_bytes()).hexdigest(),
            'batch_size':batch_size,'changes':[list(r) for r in db.execute('SELECT id,reversed FROM changes ORDER BY id')]}


def begin(db,group,triage=None,registry=None,batch_size=4):
    if not 1<=batch_size<=4:raise ValueError('batch size must be 1..4')
    triage,registry=triage or {},registry or {}
    conditions=context(db,group,triage,registry,batch_size)
    sid=digest(conditions)
    if db.execute('SELECT 1 FROM sessions WHERE session_id=?',(sid,)).fetchone():return sid
    rows=rank(db,group,triage,persist=False)
    plan=[r['event_id'] for r in rows]
    known={a['url']:a for e in events(db) for a in e.get('articles',[])}
    reason=preflight(group,known,registry,triage)
    selection=active_selection(group,triage)
    if selection and selection['action']=='same_event':
        # Keep the full catalog but only the designated target can be adjudicated.
        plan=[selection['event_id']] if selection['event_id'] in plan else []
        if not plan:reason='operator_target_unavailable'
    status=reason or ('ready' if plan else 'needs_evidence')
    with atomic(db):
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',(sid,group_key(group),dumps(group),dumps(conditions),dumps(plan),0,status,batch_size))
        for r in rows:
            state='unexamined' if r['event_id'] in plan else 'operator_not_selected'
            db.execute('INSERT INTO candidate_checks VALUES(?,?,?,?)',(sid,r['event_id'],r['rank'],state))
        db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',('staged:'+sid,group_key(group),dumps({'group':group,'status':status})))
    return sid


def inspect(db,sid):
    row=db.execute('SELECT cursor,status,plan,group_payload FROM sessions WHERE session_id=?',(sid,)).fetchone()
    if not row:raise KeyError(sid)
    return {'session_id':sid,'cursor':row[0],'status':row[1],'plan':json.loads(row[2]),'group':json.loads(row[3]),
            'candidate_states':dict(db.execute('SELECT event_id,state FROM candidate_checks WHERE session_id=?',(sid,))),
            'records':[{'position':r[0],'ids':json.loads(r[1]),'bytes':r[2],'outcome':r[3]} for r in db.execute('SELECT position,presented,input_bytes,outcome FROM batch_records WHERE session_id=? ORDER BY position',(sid,))]}


def valid_answer(answer,batch):
    if not isinstance(answer,dict) or not set(TRACKING_RESPONSE)<=set(answer):return False
    if answer.get('relation') not in {'same_event','new_event','uncertain'} or answer.get('confidence') not in {'high','medium','low'}:return False
    if any(not isinstance(answer[k],bool) for k in ('multi_event','meaningful_change','completed')):return False
    if any(not isinstance(answer[k],str) for k in ('event_id','current_stage','summary','latest','next_watch','completion_quote')):return False
    if any(not isinstance(answer[k],list) for k in ('evidence_urls','identity_matches','progress_evidence','schedules')):return False
    if not isinstance(answer['event_identity'],dict):return False
    if any(not isinstance(url,str) for url in answer['evidence_urls']):return False
    return answer.get('relation')!='same_event' or answer.get('event_id') in batch


def step(db,sid,mock,now,triage=None,registry=None,budget_available=True,charge_simulated=False,fail_save=False,simulation_run_id=None):
    triage,registry=triage or {},registry or {}
    row=db.execute('SELECT group_key,group_payload,context,plan,cursor,status,batch_size FROM sessions WHERE session_id=?',(sid,)).fetchone()
    if not row:raise KeyError(sid)
    key,group_payload,old_context,plan_text,cursor,status,limit=row
    group,plan=json.loads(group_payload),json.loads(plan_text)
    if status not in ('ready','budget_deferred','retry_api'):
        return inspect(db,sid)
    if context(db,group,triage,registry,limit)!=json.loads(old_context):
        with atomic(db):
            db.execute('UPDATE sessions SET status=? WHERE session_id=?',('stale_requires_new_session',sid))
            db.execute('UPDATE holds SET payload=? WHERE stage=? AND group_key=?',(dumps({'group':group,'status':'stale_requires_new_session'}),'staged:'+sid,key))
        return inspect(db,sid)
    selection=active_selection(group,triage)
    batch=plan[cursor:cursor+(1 if selection and selection['action']=='same_event' else limit)]
    event_index={e['event_id']:e for e in events(db)}
    known={a['url']:a for e in events(db) for a in e.get('articles',[])}
    from pending_triage import source_saved
    fresh=[a for a in group['articles'] if not source_saved(a,known)]
    operator_context=''
    if selection:operator_context='運営者の振り分け希望（同一性・進展の根拠ではない）。根拠不足ならuncertain。'+json.dumps({k:selection[k] for k in ('action','event_id')},ensure_ascii=False)
    prompt=analysis_prompt([event_index[eid] for eid in batch],fresh,operator_context,group.get('event_name',''))
    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()
    request_key=digest({'session':sid,'position':cursor,'prompt_hash':prompt_hash})
    cached=db.execute('SELECT answer FROM batch_cache WHERE request_key=?',(request_key,)).fetchone()
    if cached:
        answer=json.loads(cached[0])
    else:
        if not budget_available or (charge_simulated and reserve_mock(db,len(prompt.encode()),now,run_id=simulation_run_id)['status']=='budget_deferred'):
            with atomic(db):
                db.execute('UPDATE sessions SET status=? WHERE session_id=?',('budget_deferred',sid))
                db.execute('UPDATE holds SET payload=? WHERE stage=? AND group_key=?',(dumps({'group':group,'status':'budget_deferred'}),'staged:'+sid,key))
            return inspect(db,sid)
        try:
            answer=mock(prompt,batch)
        except (ValueError,TypeError) as exc:
            answer={'parse_error':type(exc).__name__}
        except (OSError,RuntimeError) as exc:
            with atomic(db):db.execute('UPDATE sessions SET status=? WHERE session_id=?',('budget_deferred' if isinstance(exc,BudgetExceeded) else 'retry_api',sid))
            return inspect(db,sid)
        try:
            encoded=dumps(answer)
        except (ValueError,TypeError):
            answer={'parse_error':'non_json_mock_answer'};encoded=dumps(answer)
        # Capture response before state adoption. A failed adoption can reuse it.
        with atomic(db):
            db.execute('INSERT INTO batch_cache VALUES(?,?,?,?)',(request_key,sid,prompt_hash,encoded))
    valid=valid_answer(answer,batch)
    with atomic(db):
        outcome=judge(db,group,answer,now,triage=triage,registry=registry,presented_ids=batch,rerank=False) if valid else 'invalid_answer_schema_or_target'
        next_cursor=cursor+len(batch)
        invalid_count=db.execute("SELECT count(*) FROM batch_records WHERE session_id=? AND outcome='invalid_answer_schema_or_target'",(sid,)).fetchone()[0]+int(not valid)
        result_status='accepted' if outcome=='accepted' else ('ready' if next_cursor<len(plan) else 'needs_evidence')
        if outcome!='accepted' and (invalid_count+prior_attempts(group,triage)>=3 or (next_cursor>=len(plan) and prior_attempts(group,triage)+1>=3)):
            result_status='manual_review'
        db.execute('INSERT INTO batch_records VALUES(?,?,?,?,?,?,?)',
                   (sid,cursor,dumps(batch),hashlib.sha256(prompt.encode()).hexdigest(),len(prompt.encode()),dumps(answer),outcome))
        db.execute('UPDATE sessions SET cursor=?,status=? WHERE session_id=?',(next_cursor,result_status,sid))
        for eid in batch:
            db.execute('UPDATE candidate_checks SET state=? WHERE session_id=? AND event_id=?',
                       ('validated' if outcome=='accepted' and eid==answer.get('event_id') else 'examined_unresolved',sid,eid))
        db.execute('UPDATE holds SET payload=? WHERE stage=? AND group_key=?',
                   (dumps({'group':group,'status':result_status,'unexamined':plan[next_cursor:]}),'staged:'+sid,key))
        if fail_save:raise RuntimeError('simulated staged save interruption')
    return inspect(db,sid)
