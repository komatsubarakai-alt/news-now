"""Offline B-policy. Plans and receipts only: no transport, adoption or publication.

Status jobs are groups; tracking jobs are *known sequences of AI prompts*. They
never share a slot counter. Currency is integer micro-USD, using unchanged caps.
"""
import copy
import json
from pathlib import Path

from ai_cost_control import periods
from engine import atomic, dumps, events
from failed_response import digest
from pending_triage import group_key, fingerprint
from queue_policy import PROGRESS, already_saved_group, date_time, pause_reason, prioritize_groups
import queue_diagnostics as diagnostics
import status_cache_replay as cache

PATTERN = ('oldest', 'signal', 'normal', 'normal')


def initialize(db):
    cache.initialize(db)
    with atomic(db):
        db.execute('''CREATE TABLE IF NOT EXISTS priority_runs(
          operation TEXT PRIMARY KEY, input_hash TEXT NOT NULL, result TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS priority_deferrals(
          revision TEXT PRIMARY KEY, first_deferred TEXT NOT NULL,
          last_cause TEXT NOT NULL, last_operation TEXT NOT NULL)''')


def reservation(input_bytes):
    if type(input_bytes) is not int or input_bytes < 0: raise ValueError('invalid input size')
    return ((input_bytes + 2048) * 3 + 3) // 4 + 5400


class Envelope:
    """Private projection, never the production budget ledger or actual usage."""
    def __init__(self, ledger, now):
        day, month = periods(now)
        self.day = copy.deepcopy(ledger.get('days', {}).get(day, {}))
        self.month = copy.deepcopy(ledger.get('months', {}).get(month, {}))
        self.run = copy.deepcopy(ledger.get('run', {}))
        self.reserved = self.calls = 0

    def causes(self, amount, calls=1, *, tracking=False):
        causes = []
        if self.day.get('reserved_micro_usd', 0) + amount > 200000: causes.append('amount_daily')
        if self.month.get('reserved_micro_usd', 0) + amount > 5000000: causes.append('amount_monthly')
        if self.day.get('calls', 0) + calls > 40: causes.append('calls_daily')
        if self.run.get('calls', 0) + calls > 24: causes.append('calls_run')
        if not tracking:
            # Preserve current workflow's existing tracking protection, not new money/calls.
            if self.day.get('reserved_micro_usd', 0) + amount > 200000 - max(0, 100000-self.day.get('tracking_micro_usd', 0)):
                causes.append('tracking_reserved_amount')
            if (self.day.get('calls', 0) + calls > 40-max(0,16-self.day.get('tracking_calls',0)) or
                    self.run.get('calls',0) + calls > 24-max(0,4-self.run.get('tracking_calls',0))):
                causes.append('tracking_reserved_calls')
        return causes

    def take(self, amount, calls=1, *, tracking=False):
        causes = self.causes(amount, calls, tracking=tracking)
        if causes: return causes
        for row in (self.day,self.month,self.run): row['calls'] = row.get('calls',0)+calls
        for row in (self.day,self.month): row['reserved_micro_usd'] = row.get('reserved_micro_usd',0)+amount
        if tracking:
            for row in (self.day,self.month,self.run): row['tracking_calls'] = row.get('tracking_calls',0)+calls
            for row in (self.day,self.month): row['tracking_micro_usd'] = row.get('tracking_micro_usd',0)+amount
        self.reserved += amount; self.calls += calls
        return []


def order_groups(groups, triage, now, *, deferred=None):
    """Stable FIFO inside each lane; oldest lane first. No candidate deletion."""
    indexed = list(enumerate(groups))
    def first(g):
        at = date_time(triage.get(group_key(g),{}).get('first_seen_at'))
        # Unknown clocks cannot be invented as old; stable order at current time.
        return at or now
    def priority(pair):
        i,g=pair; rev=digest(g); pending=(deferred or {}).get(rev)
        return (first(g), date_time(pending) or now, i)
    remaining=sorted(indexed,key=priority); result=[]
    while remaining:
        lane=PATTERN[len(result)%4]
        eligible=[p for p in remaining if lane=='oldest' or bool(PROGRESS.search(' '.join(a.get('title','') for a in p[1].get('articles',[])))) == (lane=='signal')]
        picked=(eligible or remaining)[0]
        result.append(picked[1]);remaining.remove(picked)
    return result


def _known_block(group, triage):
    if not cache.pending_articles(group): return 'evidence'
    entry=triage.get(group_key(group),{})
    pause=pause_reason(group,triage)
    if pause: return 'operator_hold' if pause=='manual_review' else 'out_of_scope_rule'
    if entry.get('source_fingerprint') and entry['source_fingerprint']!=fingerprint(group):
        return None
    if entry.get('status')=='needs_evidence' or diagnostics.legacy_cause(entry.get('reason'))=='evidence':
        return 'evidence'
    if entry.get('cached_rejection_waiting') or diagnostics.legacy_cause(entry.get('reason'))=='rejected_unchanged':
        return 'rejected_unchanged'
    return None


def run_status(db, groups, now, *, operation_id, ledger, triage=None, registry=None,
               raw_legacy_cache=None, mock=None, limit=4, cache_limit=16, fail_save=False, policy="B"):
    """Cache first, B-order fresh *offline fixtures* second, one atomic receipt.

    An observation-only run may select work but never claims to process it.
    Operation replay is idempotent; a different snapshot requires a new operation.
    Failures roll back selection/debt/results together. No source queue is mutated.
    """
    if not isinstance(operation_id,str) or not operation_id.strip(): raise ValueError('operation required')
    if type(limit) is not int or not 0<=limit<=4: raise ValueError('invalid status limit')
    if mock is not None and type(mock) is not cache.MockResponses: raise ValueError('offline fixture transport required')
    if policy not in ('B','current'): raise ValueError('unknown comparison policy')
    initialize(db);triage=triage or {};registry=registry or {};legacy=raw_legacy_cache or {}
    original=copy.deepcopy(groups)
    # Repeated identical revisions coalesce; same logical key with different content is not silently merged.
    unique={};logical={}
    for g in original:
        rev=digest(g);k=group_key(g)
        if k in logical and logical[k]!=rev: raise ValueError('ambiguous group revisions')
        logical[k]=rev;unique.setdefault(rev,g)
    groups=list(unique.values())
    ih=digest({'groups':groups,'triage':triage,'registry':registry,'ledger':ledger,
               'events':events(db),'validator':cache.validator_hash(),'scheduler':digest(Path(__file__).read_text()),'now':now.isoformat(),
               'mock':mock.answers if mock else None,'legacy':legacy,'limit':limit,'cache_limit':cache_limit,'policy':policy})
    receipt=db.execute('SELECT input_hash,result FROM priority_runs WHERE operation=?',(operation_id,)).fetchone()
    if receipt:
        if receipt[0]!=ih: raise ValueError('operation input changed')
        result=json.loads(receipt[1]);result['operation_replayed']=True;return result
    with atomic(db):
        before=cache.run(db,groups,now,triage=triage,registry=registry,raw_legacy_cache=legacy,
                         processing_limit=0,cache_limit=cache_limit)
        eligible=[];waiting={};item_keys={}
        for g in groups:
            rev=digest(g);meta=cache.binding(db,g,triage,registry)
            item=digest({'binding':meta,'answer_hash':cache.response_hash(db,meta,mock=mock,legacy=legacy)})
            item_keys[rev]=item
            # A capture may have changed unbound->bound identity; exact current binding is authoritative.
            saved=db.execute('SELECT 1 FROM status_shadow_results WHERE group_key=? AND article_hash=? AND context_hash=? AND request_hash=?',
                             (group_key(g),meta['article_hash'],meta['context_hash'],meta['request_hash'])).fetchone()
            if saved or already_saved_group(g,events(db)): continue
            block=_known_block(g,triage)
            previous=db.execute('SELECT outcome FROM status_cache_attempts WHERE attempt=?',(item,)).fetchone()
            if previous and previous[0]=='rejected': block='rejected_unchanged'
            if block: waiting[rev]=[block]
            else: eligible.append(g)
        deferred=dict(db.execute('SELECT revision,first_deferred FROM priority_deferrals'))
        ordered=(order_groups(eligible,triage,now,deferred=deferred) if policy=='B'
                 else prioritize_groups(eligible,triage,now))
        envelope=Envelope(ledger,now);chosen=[];stopped=None
        for g in ordered:
            rev=digest(g)
            if len(chosen)>=limit: waiting[rev]=['processing_slots'];continue
            if stopped: waiting[rev]=list(stopped);continue
            causes=envelope.take(reservation(len(cache.request(g)['input'].encode())))
            if causes:
                stopped=causes;waiting[rev]=causes;continue
            chosen.append(g)
        # Deliberately disable the second cache scan; cache work has its own bounded pass.
        after=cache.run(db,chosen,now,triage=triage,registry=registry,raw_legacy_cache=legacy,
                        cache_first=False,processing_limit=limit,mock=mock,fail_save=fail_save)
        for g in chosen:
            meta=cache.binding(db,g,triage,registry);rev=digest(g)
            saved=db.execute('SELECT 1 FROM status_shadow_results WHERE group_key=? AND article_hash=? AND context_hash=? AND request_hash=?',
                              (group_key(g),meta['article_hash'],meta['context_hash'],meta['request_hash'])).fetchone()
            if saved:
                db.execute('DELETE FROM priority_deferrals WHERE revision=?',(rev,))
            elif mock is None or meta['request_hash'] not in mock.answers: waiting[rev]=['unknown']
            else: waiting[rev]=['rejected_unchanged']
        for rev,causes in waiting.items():
            db.execute('''INSERT INTO priority_deferrals VALUES(?,?,?,?) ON CONFLICT(revision)
              DO UPDATE SET last_cause=excluded.last_cause,last_operation=excluded.last_operation''',
                       (rev,now.isoformat(),dumps(causes),operation_id))
        remaining=0
        for g in groups:
            meta=cache.binding(db,g,triage,registry)
            if not db.execute('SELECT 1 FROM status_shadow_results WHERE group_key=? AND article_hash=? AND context_hash=? AND request_hash=?',
                              (group_key(g),meta['article_hash'],meta['context_hash'],meta['request_hash'])).fetchone():
                remaining+=1
        result={'selected':[{'group_key':group_key(g),'revision':digest(g),
                            'signal':bool(PROGRESS.search(' '.join(a.get('title','') for a in g['articles'])))} for g in chosen],
                'reservation_micro_usd':envelope.reserved,'reserved_calls':envelope.calls,
                'cache_validated':before['cache_validated'],'fresh_mock_requests':after['mock_requests'],
                'fresh_results':after['new_results'],'wait_reasons':waiting,
                'source_groups':len(groups),'remaining_status_groups':remaining,
                'operation_replayed':False,'actual_network_calls':0,'can_publish':False}
        if fail_save: raise RuntimeError('simulation_save_failure')
        db.execute('INSERT INTO priority_runs VALUES(?,?,?)',(operation_id,ih,dumps(result)))
        return result


def plan_tracking(due_jobs, normal_jobs, ledger, now, *, call_limit=8):
    """One due job (up to two known AI prompts) and at least one normal job.

    No news finder/transport. Caller supplies saved prompt sizes, never estimates
    missing search results. A job is reserved atomically as a call bundle.
    Protect one eligible normal job before admitting the due job. If both cannot
    fit, normal work remains possible and the due job stays pending. No completion
    or last-search timestamps are changed. Tracking has its own call budget.
    """
    if type(call_limit) is not int or not 0<=call_limit<=8: raise ValueError('invalid tracking call limit')
    # tracking_calls includes this run's prior tracking traffic; never grant a fresh eight.
    call_limit=min(call_limit,max(0,8-ledger.get('run',{}).get('tracking_calls',0)))
    selected=[];waiting={};env=Envelope(ledger,now);seen=set();dues=[];normals=[]
    for jobs,dest in ((due_jobs,dues),(normal_jobs,normals)):
        for job in jobs:
            ident=job['id']
            if ident in seen: raise ValueError('duplicate tracking job')
            seen.add(ident)
            if job.get('blocked'):
                cause=job['blocked'];waiting[ident]=[cause if cause in ('evidence','rejected_unchanged','operator_hold') else 'unknown'];continue
            sizes=job.get('prompt_bytes')
            if not isinstance(sizes,list) or not sizes or any(type(x) is not int or x<0 for x in sizes):
                waiting[ident]=['unknown'];continue
            dest.append((job,sum(reservation(x) for x in sizes),len(sizes)))
    def take(entry):
        job,amount,calls=entry
        causes=(['processing_slots'] if env.calls+calls>call_limit else [])+env.causes(amount,calls,tracking=True)
        if causes: waiting[job['id']]=causes;return False
        env.take(amount,calls,tracking=True);selected.append(job['id']);return True
    # One ordinary job is protected so a due search cannot consume every call.
    if normals: take(normals.pop(0))
    admitted=False
    for entry in dues:
        if admitted or entry[2]>2: waiting[entry[0]['id']]=['processing_slots'];continue
        if take(entry): admitted=True
    for entry in normals: take(entry)
    return {'selected':selected,'due_selected':int(admitted),'wait_reasons':waiting,
            'reservation_micro_usd':env.reserved,'reserved_calls':env.calls,
            'actual_network_calls':0,'can_publish':False}


def scheduled_jobs(saved_events, now, prompt_sizes=None):
    """Use existing due-date rules; missing saved search inputs remain unknown."""
    from tracking_v1 import scheduled_search_due
    due=[e for e in saved_events if scheduled_search_due(e,now)]
    # Never-confirmed schedules first, then least recently checked.
    due.sort(key=lambda e: (date_time(e.get('last_schedule_search_at')) or
                            date_time('1970-01-01T00:00:00+00:00'), e['event_id']))
    return [{'id':'schedule:'+e['event_id'],'event_id':e['event_id'],
             'prompt_bytes':(prompt_sizes or {}).get(e['event_id'])} for e in due]


def run_round(db, groups, saved_events, normal_jobs, now, *, operation_id, ledger,
              schedule_prompt_sizes=None, triage=None, registry=None,
              raw_legacy_cache=None, mock=None, fail_save=False):
    """Share ONE projected ledger across tracking and status; no real reservations.

    The due/normal plan is made first. Its full call bundles are deducted before
    status selection, so independent stage limits cannot overspend together.
    Replaying the operation returns the same status receipt, not new AI work.
    """
    initialize(db)
    input_hash=digest({'groups':groups,'events':saved_events,'normal':normal_jobs,
                       'now':now.isoformat(),'ledger':ledger,'sizes':schedule_prompt_sizes,
                       'triage':triage,'registry':registry,'legacy':raw_legacy_cache,
                       'mock':mock.answers if mock else None,'validator':cache.validator_hash(),'scheduler':digest(Path(__file__).read_text())})
    op='round:'+operation_id
    previous=db.execute('SELECT input_hash,result FROM priority_runs WHERE operation=?',(op,)).fetchone()
    if previous:
        if previous[0]!=input_hash:raise ValueError('operation input changed')
        result=json.loads(previous[1]);result['operation_replayed']=True;return result
    with atomic(db):
        tracking=plan_tracking(scheduled_jobs(saved_events,now,schedule_prompt_sizes),normal_jobs,ledger,now)
        projected=copy.deepcopy(ledger);day,month=periods(now)
        amount=tracking['reservation_micro_usd'];calls=tracking['reserved_calls']
        for name,key in [('days',day),('months',month)]:
            row=projected.setdefault(name,{}).setdefault(key,{})
            for field,delta in [('reserved_micro_usd',amount),('calls',calls),('tracking_micro_usd',amount),('tracking_calls',calls)]:
                row[field]=row.get(field,0)+delta
        run=projected.setdefault('run',{})
        for field in ('calls','tracking_calls'):run[field]=run.get(field,0)+calls
        status=run_status(db,groups,now,operation_id='status:'+op,ledger=projected,
                          triage=triage,registry=registry,raw_legacy_cache=raw_legacy_cache,
                          mock=mock,fail_save=fail_save)
        result={'status':status,'tracking':tracking,'reservation_micro_usd':amount+status['reservation_micro_usd'],
                'reserved_calls':calls+status['reserved_calls'],'actual_network_calls':0,
                'operation_replayed':False,'can_publish':False}
        db.execute('INSERT INTO priority_runs VALUES(?,?,?)',(op,input_hash,dumps(result)))
        return result
