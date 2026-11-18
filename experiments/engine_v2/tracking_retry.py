"""Offline retry receipts. No client, finder, adoption, publication or real ledger.

Only manifests captured from known inputs may suppress work. Legacy diagnostic
records remain separate and cannot be used to invent an old input manifest.
"""
import copy
import json
from datetime import timedelta, timezone
from pathlib import Path

from engine import atomic, dumps
from failed_response import digest, event_conditions, validation_revision
from tracking_v1 import date_time
import queue_diagnostics as diagnostics

STAGES={'event_attachment','backfill_query','backfill_selection','backfill_analysis',
        'schedule_selection','schedule_analysis','enrichment'}
CAUSES={'amount_daily','amount_monthly','calls_daily','calls_run','calls_stage',
        'protected_budget','protected_calls','processing_slots','evidence','no_results',
        'format','transport','unknown','operator_hold','success'}
REASONS={
 'daily_budget_deferred':'amount_daily','monthly_budget_deferred':'amount_monthly',
 'tracking_reserved_budget_deferred':'protected_budget',
 'tracking_reserved_calls_deferred':'protected_calls','stage_budget_deferred':'calls_stage',
 'editorial_article_hold':'operator_hold',
 'article_identity_unproved':'evidence','event_name_unproved':'evidence',
 'uncertain_or_invalid_evidence':'evidence','search_identity_uncertain':'evidence',
 'schedule_identity_uncertain':'evidence','uncertain_relation':'evidence',
 'progress_quote_missing':'evidence','stage_not_supported_by_quote':'evidence',
 'invalid_json':'format','JSONDecodeError':'format','incomplete_response':'format','JSON object required':'format',
 'matches must be a list':'format','empty search query':'format',
 'TimeoutError':'transport','ConnectionError':'transport','HTTPError':'transport',
 'no_results':'no_results','calls_daily':'calls_daily','calls_run':'calls_run',
 'processing_slots':'processing_slots','same_rejected_answer':'rejected_unchanged'}
JST=timezone(timedelta(hours=9))


def classify(reason):
    # ValueError, BudgetExceeded and combined call_budget_deferred are ambiguous.
    return REASONS.get(str(reason).split(':',1)[0],'unknown')


def initialize(db):
    with atomic(db):
        db.execute('''CREATE TABLE IF NOT EXISTS retry_manifests(
          context_key TEXT PRIMARY KEY, manifest TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS tracking_retry_attempts(
          id INTEGER PRIMARY KEY, operation TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
          context_key TEXT NOT NULL REFERENCES retry_manifests(context_key), cause TEXT NOT NULL,
          observed_at TEXT NOT NULL, retry_after TEXT, run_id TEXT,
          response_hash TEXT, completion TEXT NOT NULL,
          reservation_micro INTEGER, usage_micro INTEGER)''')
        db.execute('''CREATE TABLE IF NOT EXISTS tracking_retry_replays(
          operation TEXT PRIMARY KEY, input_hash TEXT NOT NULL, result TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS tracking_retry_legacy(
          record_hash TEXT PRIMARY KEY, stage TEXT NOT NULL, cause TEXT NOT NULL)''')


def validator():
    return digest({'tracking':validation_revision('tracking'),
                   'retry_policy':Path(__file__).read_text(),
                   'offline_schedule_validation':Path(diagnostics.__file__).read_text()})


def make_context(stage, event, articles, now, *, search=None, validator_id=None, operator=None):
    """Capture current known inputs, never reconstruct unknown historical inputs.

    Original text/query are not diagnostic fields. All evidence, operator changes
    and event conditions are bound by hashes; article refs retain URL/version/hash.
    Relative search must be supplied with its actual resolved window by the caller.
    """
    if stage not in STAGES or not isinstance(event,dict) or not event.get('event_id'):
        raise ValueError('invalid retry target')
    if now.tzinfo is None: raise ValueError('timezone required')
    if not isinstance(articles,list) or any(not isinstance(a,dict) or not a.get('url') for a in articles):
        raise ValueError('invalid sources')
    if stage=='event_attachment' and not articles: raise ValueError('article required')
    if stage.startswith(('backfill','schedule')):
        if not isinstance(search,dict) or not isinstance(search.get('query'),str) or not search['query'].strip():
            raise ValueError('known search required')
        if not all(k in search for k in ('window_start','window_end')):
            raise ValueError('known search window required')
        for k in ('window_start','window_end'):
            if search[k] is None and search.get('unbounded_'+k.removeprefix('window_')) is not True:
                raise ValueError('unknown search boundary is not unbounded')
            if search[k] is not None and date_time(search[k]) is None: raise ValueError('invalid search window')
    phases=[]
    for schedule in event.get('schedules',[]):
        start,end=date_time(schedule.get('due_start')),date_time(schedule.get('due_end'))
        phase='unknown'
        if start and end:
            phase=('before_watch' if now<start-timedelta(days=7) else 'watch' if now<start
                   else 'due' if now<=end else 'overdue' if now<=end+timedelta(days=14) else 'late')
        phases.append({'schedule_hash':digest(schedule),'phase':phase,
                       'due_start':schedule.get('due_start'),'due_end':schedule.get('due_end')})
    manifest={'schema':1,'origin':'known_offline_capture','stage':stage,'event_id':event['event_id'],
              'event_hash':digest(event_conditions([event])),
              'article_refs':[{'url':a['url'],'version_id':a.get('article_version_id'),
                               'content_hash':digest(a)} for a in articles],
              'request_conditions':{'model':'gpt-5.4-mini','max_output_tokens':1200},
              'search_hash':digest(search),'search_window':({k:search[k] for k in ('window_start','window_end')} if search else None),'schedule_phases':phases,
              'validator':validator_id or validator(),'operator_hash':digest(operator or {})}
    return {**manifest,'context_key':digest(manifest)}


def _check(context):
    if not isinstance(context,dict):return False
    data={k:v for k,v in context.items() if k!='context_key'}
    required={'schema','origin','stage','event_id','event_hash','article_refs','search_hash','search_window','request_conditions','schedule_phases','validator','operator_hash'}
    return (set(data)==required and isinstance(data.get('article_refs'),list) and isinstance(data.get('schedule_phases'),list) and data.get('schema')==1 and data.get('origin')=='known_offline_capture'
            and data.get('stage') in STAGES and data.get('event_id')
            and context.get('context_key')==digest(data))


def boundary_changed(context, now):
    for schedule in context['schedule_phases']:
        start,end=date_time(schedule.get('due_start')),date_time(schedule.get('due_end'))
        if not start or not end:continue
        phase=('before_watch' if now<start-timedelta(days=7) else 'watch' if now<start
               else 'due' if now<=end else 'overdue' if now<=end+timedelta(days=14) else 'late')
        if schedule['phase']!=phase:return True
    return False


def _save_context(db,context):
    if not _check(context): raise ValueError('unproved retry context')
    db.execute('INSERT OR IGNORE INTO retry_manifests VALUES(?,?)',
               (context['context_key'],dumps(context)))
    row=db.execute('SELECT manifest FROM retry_manifests WHERE context_key=?',(context['context_key'],)).fetchone()
    if row[0]!=dumps(context):raise ValueError('retry context conflict')


def import_legacy(db, records):
    initialize(db)
    with atomic(db):
        for row in records:
            stage=row.get('stage','unknown')
            if stage not in {'event_attachment','backfill','schedule_search','enrichment'}:continue
            # No titles, original answer, arbitrary reason text or fabricated binding.
            db.execute('INSERT OR IGNORE INTO tracking_retry_legacy VALUES(?,?,?)',
                       (digest(row),stage,classify(row.get('reason'))))


def gate(db, context, now, *, run_id=None, resources_available=None):
    initialize(db)
    if now.tzinfo is None:raise ValueError('timezone required')
    if context is None:return {'allowed':True,'cause':'unknown','basis':'legacy_context_missing'}
    if not _check(context):raise ValueError('unproved retry context')
    if boundary_changed(context,now):
        return {'allowed':True,'cause':'unknown','basis':'schedule_boundary_changed'}
    row=db.execute('''SELECT cause,retry_after,run_id FROM tracking_retry_attempts
      WHERE context_key=? ORDER BY id DESC LIMIT 1''',(context['context_key'],)).fetchone()
    if row is None:return {'allowed':True,'cause':'unknown','basis':'new_conditions'}
    cause,after,old_run=row
    if cause in {'success','processing_slots'}:
        return {'allowed':True,'cause':cause,'basis':'no_failure_suppression'}
    if cause in {'amount_daily','amount_monthly','calls_daily','calls_run','calls_stage','protected_budget','protected_calls'}:
        if resources_available is True or (cause in {'calls_run','calls_stage','protected_calls'} and run_id is not None and old_run is not None and run_id!=old_run):
            return {'allowed':True,'cause':cause,'basis':'resources_recovered'}
    allowed=bool(after and now>=date_time(after))
    return {'allowed':allowed,'cause':cause,
            'basis':'cooldown_elapsed' if allowed else 'same_conditions_suppressed',
            'retry_after':after}


def record(db, operation_id, context, cause, now, *, run_id=None, payload=None,
           reservation_micro=None, usage_micro=None, fail_save=False, resources_available=None):
    """Call AFTER an existing validator/transport observed the outcome.

    This receipt cannot adopt a decision. Suppressed calls are NOT new failures.
    Accounting numbers are supplied observations, not charges or invented savings.
    """
    initialize(db)
    if cause not in CAUSES:raise ValueError('invalid outcome')
    if not isinstance(operation_id,str) or not operation_id.strip():raise ValueError('operation required')
    if now.tzinfo is None:raise ValueError('timezone required')
    for value in (reservation_micro,usage_micro):
        if value is not None and (type(value) is not int or value<0):raise ValueError('invalid amount')
    if not _check(context):raise ValueError('unproved retry context')
    response_hash=digest(payload) if payload is not None else None
    completion=diagnostics.completion(payload)
    ih=digest([context,cause,run_id,response_hash,completion,reservation_micro,usage_micro])
    existing=db.execute('SELECT input_hash FROM tracking_retry_attempts WHERE operation=?',(operation_id,)).fetchone()
    if existing:
        if existing[0]!=ih:raise ValueError('operation input changed')
        return {'recorded':False,'operation_replayed':True}
    if boundary_changed(context,now):raise ValueError('stale schedule capture; recapture current inputs')
    # Bounded transient cooldown; new conditions can always bypass an old receipt.
    after=None
    if cause in {'transport','unknown','no_results'}:
        last=db.execute('SELECT cause FROM tracking_retry_attempts WHERE context_key=? ORDER BY id DESC LIMIT 1',
                        ((context or {}).get('context_key'),)).fetchone()
        count=db.execute('SELECT count(*) FROM tracking_retry_attempts WHERE context_key=? AND cause=?',
                         ((context or {}).get('context_key'),cause)).fetchone()[0] if last and last[0]==cause else 0
        minutes=min(360,15*(2**min(count,5))) if cause=='transport' else 60 if cause=='unknown' else 360
        after=now+timedelta(minutes=minutes)
    elif cause in {'amount_daily','calls_daily','protected_budget'}:
        local=now.astimezone(JST);after=(local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    elif cause=='amount_monthly':
        local=now.astimezone(JST);after=(local.replace(day=28)+timedelta(days=4)).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    elif cause in {'calls_run','calls_stage','protected_calls'}:
        after=now+timedelta(hours=6)
    with atomic(db):
        _save_context(db,context)
        # Enforce the gate at recording too: cannot inflate failure counters by replays.
        decision=gate(db,context,now,run_id=run_id,resources_available=resources_available)
        if not decision['allowed']:return {'recorded':False,'operation_replayed':False,'suppressed':True}
        db.execute('''INSERT INTO tracking_retry_attempts(operation,input_hash,context_key,cause,
          observed_at,retry_after,run_id,response_hash,completion,reservation_micro,usage_micro)
          VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
          (operation_id,ih,context['context_key'],cause,now.isoformat(),after.isoformat() if after else None,
           run_id,response_hash,completion,reservation_micro,usage_micro))
        if fail_save:raise RuntimeError('simulation_save_failure')
    return {'recorded':True,'operation_replayed':False}


def replay_schedule(db, context, event, articles, selection_answer, analysis_payload, now,
                    *, operation_id, fail_save=False):
    """Finite saved/mock inputs through existing schedule guards, reference-only.

    No successful event returned by validation is adopted or published. A missing
    answer is unknown, not evidence. An explicitly observed empty result is not a
    claim about completion/no progress.
    """
    if context['stage'] not in {'schedule_selection','schedule_analysis'}:
        raise ValueError('schedule adapter required')
    # Verify the captured context belongs to these exact source and target inputs.
    if context.get('event_hash')!=digest(event_conditions([event])) or context.get('article_refs')!=[
        {'url':a['url'],'version_id':a.get('article_version_id'),'content_hash':digest(a)} for a in articles]:
        raise ValueError('replay inputs changed')
    initialize(db)
    ih=digest([context,event,articles,selection_answer,analysis_payload])
    old=db.execute('SELECT input_hash,result FROM tracking_retry_replays WHERE operation=?',(operation_id,)).fetchone()
    if old:
        if old[0]!=ih:raise ValueError('operation input changed')
        result=json.loads(old[1]);result['operation_replayed']=True;return result
    decision=gate(db,context,now)
    if not decision['allowed']:return {'suppressed':True,'retry':decision,'actual_network_calls':0}
    with atomic(db):
        if not articles:outcome='no_results';validation={'status':'no_results_observed'}
        elif selection_answer is None or analysis_payload is None:
            outcome='unknown';validation={'status':'saved_answer_missing'}
        else:
            validation=diagnostics.schedule_replay(db,event,articles,selection_answer,analysis_payload,now)
            outcome=('success' if validation.get('status')=='validated_reference_only' else
                     'evidence' if validation.get('cause')=='evidence' else
                     classify(validation.get('safe_error')))
        receipt=record(db,operation_id,context,outcome,now,payload=analysis_payload,fail_save=fail_save)
        result={'suppressed':False,'cause':outcome,'validation':validation,'receipt':receipt,
                'actual_network_calls':0,'can_publish':False,'operation_replayed':False}
        db.execute('INSERT INTO tracking_retry_replays VALUES(?,?,?)',(operation_id,ih,dumps(result)))
        return result
