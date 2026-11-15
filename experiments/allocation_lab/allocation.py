"""Offline allocation lab. Uses native budget guards on a disposable ledger only.

No API response calls, merge decisions, classifications or production writers.
All 'mock_pass' completions are counterfactual capacity, never actual approvals.
"""
import copy,json,os,math,statistics,tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import ai_cost_control as native

JST=timezone(timedelta(hours=9))
ENV={'AI_DAILY_USD_LIMIT':'0.20','AI_MONTHLY_USD_LIMIT':'5.00','AI_DAILY_CALL_LIMIT':'40','AI_RUN_CALL_LIMIT':'24',
     'AI_TRACKING_DAILY_RESERVED_USD':'0.10','AI_TRACKING_DAILY_RESERVED_CALLS':'16','AI_TRACKING_RUN_RESERVED_CALLS':'4','AI_MAX_OUTPUT_TOKENS':'1200'}
PLANS={'A':{'pair':4,'merge':2,'status':4},'B':{'pair':1,'merge':1,'status':8},'C':{'pair':2,'merge':1,'status':6}}
REASONS=('money_limit','daily_call_limit','run_call_limit','status_slot','interval','evidence_missing','validation_unestablished','other_unknown')

def dt(x):
    value=datetime.fromisoformat(x.replace('Z','+00:00'))
    if value.tzinfo is None:raise ValueError('timezone required')
    return value

def explain_wait(record,stage,evidence=None):
    """Absence of a bill is not proof of a capacity constraint; require provenance."""
    reason=record.get('reason','');e=evidence or {}
    if reason in ('daily_budget_deferred','monthly_budget_deferred','tracking_reserved_budget_deferred'):
        return 'money_limit',reason
    if reason=='call_budget_deferred':
        if e.get('limit_kind')=='day':return 'daily_call_limit',reason
        if e.get('limit_kind')=='run':return 'run_call_limit',reason
        return 'other_unknown','combined_call_limit_without_historical_counter'
    if reason=='budget_deferred' and stage=='status':
        timestamp=record.get('last_seen_at')
        proven=(e.get('stage')=='status' and e.get('source') and e.get('complete_log') is True
                and e.get('status_successes')==e.get('status_limit')==4 and e.get('status_errors')==0)
        if proven and timestamp and dt(e['from'])<=dt(timestamp)<=dt(e['to']):return 'status_slot','verified_four_successes_and_bound_stage'
        return 'other_unknown','legacy_budget_label_without_run_evidence'
    if stage=='collection' and e.get('source') and e.get('gate_deferred') is True and e.get('billing_blocked') is False:
        if timedelta(0)<=dt(e['at'])-dt(e['last_ai_run_at'])<timedelta(hours=6):return 'interval','verified_gate_within_six_hours'
    if reason.startswith(('article_identity_unproved','event_name_unproved')):return 'evidence_missing',reason.split(':',1)[0]
    if reason.startswith('uncertain_or_invalid_evidence'):return 'validation_unestablished','missing_vs_invalid_evidence_not_separable'
    return 'other_unknown',reason or 'unrecorded'


def cost_profile(rows):
    result={}
    for stage in ('pair','merge','status'):
        group=[r for r in rows if r['stage']==stage]
        if not group or any(r['prompt_bytes']<=0 or not 0<=r['output_tokens']<=1200 or not 0<=r['input_tokens']<=r['prompt_bytes']+2048 for r in group):raise ValueError('invalid historical calibration')
        result[stage]={'prompt_bytes':round(statistics.median(r['prompt_bytes'] for r in group)),
                       'input_token_ratio':statistics.median(r['input_tokens']/r['prompt_bytes'] for r in group),
                       'output_tokens':math.ceil(statistics.median(r['output_tokens'] for r in group))}
    return result


def opportunities(asof,last_ai,hours=24):
    """Idealized hourly :17 opportunities; real GitHub scheduling delays unmeasured."""
    start=max(dt(asof),dt(last_ai)+timedelta(hours=6));first=start.replace(minute=17,second=0,microsecond=0)
    if first<start:first+=timedelta(hours=1)
    end=dt(asof)+timedelta(hours=hours);out=[]
    while first<end:out.append(first);first+=timedelta(hours=6)
    return out


def rejection_reason(ledger,at,amount):
    day,month=native.periods(at);d=ledger.get('days',{}).get(day,{});m=ledger.get('months',{}).get(month,{})
    run=ledger.get('run',{});calls=d.get('calls',0);money=d.get('reserved_micro_usd',0)
    if ledger.get('billing_blocked'):return 'other_unknown:billing_blocked'
    if run.get('rate_blocked'):return 'other_unknown:rate_blocked'
    if calls>=40:return 'daily_call_limit'
    if run.get('calls',0)>=24:return 'run_call_limit'
    if money+amount>200000:return 'money_limit:day'
    if m.get('reserved_micro_usd',0)+amount>5000000:return 'money_limit:month'
    protected_money=max(0,100000-d.get('tracking_micro_usd',0))
    if money+amount>200000-protected_money:return 'money_limit:tracking_protection'
    protected_day=max(0,16-d.get('tracking_calls',0));protected_run=max(0,4-run.get('tracking_calls',0))
    if calls+1>40-protected_day:return 'daily_call_limit:tracking_protection'
    if run.get('calls',0)+1>24-protected_run:return 'run_call_limit:tracking_protection'
    return 'other_unknown:unmapped_native_guard'


@contextmanager
def shadow_ledger(seed):
    previous=os.getcwd()
    with tempfile.TemporaryDirectory(prefix='allocation-ledger-') as temp:
        os.chdir(temp)
        try:
            Path('ai_budget.json').write_text(json.dumps(copy.deepcopy(seed)))
            with patch.dict(os.environ,ENV):yield
        finally:os.chdir(previous)


def simulate(plan,jobs,seed,windows,profiles,*,outcome='mock_pass',cost_mode='estimated_settlement',other_versions=(),asof=None,resume=None,priority_policy='native'):
    if plan not in PLANS or outcome not in ('mock_pass','mock_downstream','unknown','mock_invalid') or cost_mode not in ('estimated_settlement','reservation_only'):raise ValueError('unsupported experiment')
    if not windows and asof is None:raise ValueError('asof required for empty horizon')
    from .priority import work_key,rank_jobs
    resume=copy.deepcopy(resume or {})
    contract={'plan':plan,'outcome':outcome,'cost_mode':cost_mode,'priority_policy':priority_policy,'profiles':profiles}
    if resume and resume.get('contract')!=contract:raise ValueError('resume contract changed')
    if resume.get('last_window') and any(at<=dt(resume['last_window']) for at in windows):raise ValueError('window already completed')
    seed=resume.get('ledger',seed)
    source=copy.deepcopy(jobs);seed_before=copy.deepcopy(seed)
    attempted=set(resume.get('attempted',[]));completed=set(resume.get('completed',[]));mock_tracking=set(resume.get('mock_tracking',[]))
    receipts=resume.get('receipts',[]);trace=resume.get('trace',[]);reserved=resume.get('reserved',0);selected=Counter(resume.get('selected',{}));hold=Counter(resume.get('hold',{}))
    all_ids=[j['id'] for stage in jobs.values() for j in stage]
    if len(set(all_ids))!=len(all_ids):raise ValueError('duplicate work ID')
    for stage,items in jobs.items():
        if stage not in ('pair','merge','status'):raise ValueError('unknown stage')
        for job in items:
            if job['stage']!=stage or not isinstance(job['prompt_bytes'],int) or job['prompt_bytes']<=0:raise ValueError('invalid job')
    def pending_versions():return set(other_versions)|mock_tracking|{v for j in jobs['status'] if work_key(j) not in completed for v in j['versions']}
    initial=resume.get('initial_waiting',len(pending_versions()));at_start=dt(asof) if asof else windows[0]
    def blocked(j):return bool(j.get('blocked_reason')) and bool(j.get('blocked_signature')) and j.get('blocked_signature')==j.get('context_signature')
    with shadow_ledger(seed):
        for round_index,at in enumerate(windows):
            per_run=Counter();failures=Counter()
            with patch.object(native,'now',return_value=at),patch.dict(os.environ,{'GITHUB_RUN_ID':'allocation-'+at.isoformat(),'AI_PROCESSING_STAGE':'upstream'}):
                for stage in ('pair','merge','status'):
                    eligible=[j for j in jobs[stage] if work_key(j) not in attempted and not blocked(j)]
                    chosen=rank_jobs(eligible,priority_policy,PLANS[plan][stage]) if stage=='status' else eligible[:PLANS[plan][stage]]
                    for j in chosen:
                        prompt='x'*j['prompt_bytes']
                        # Native reserve/settle are file writers to the temporary ledger only.
                        try:reservation=native.reserve(prompt,native.MODEL,1200)
                        except native.BudgetExceeded as error:
                            ledger=native.read('ai_budget.json',{});amount=math.ceil((j['prompt_bytes']+2048)*.75+1200*4.5)
                            why=rejection_reason(ledger,at,amount);failures[why]+=1
                            receipts.append({'at':at.isoformat(),'stage':stage,'id':j['id'],'state':'budget_wait','reason':why,'native_reason':str(error)})
                            break
                        amount=reservation[2];reserved+=amount;selected[stage]+=1;per_run[stage]+=1;attempted.add(work_key(j))
                        if cost_mode=='estimated_settlement':
                            profile=profiles[stage];inp=min(j['prompt_bytes']+2048,math.ceil(j['prompt_bytes']*profile['input_token_ratio']))
                            native.settle(reservation,{'usage':{'input_tokens':inp,'output_tokens':profile['output_tokens']}})
                        if stage=='status' and outcome in ('mock_pass','mock_downstream'):
                            completed.add(work_key(j));state='mock_pass_not_approval'
                            if outcome=='mock_downstream':mock_tracking.update(j['versions'])
                        else:state='unverified' if outcome=='unknown' else 'mock_invalid' if outcome=='mock_invalid' else 'comparison_only_no_merge'
                        if stage=='status' and state!='mock_pass_not_approval':hold[state]+=1
                        age=round((at-dt(j['first_seen_at'])).total_seconds()/3600,2) if j.get('first_seen_at') else None
                        receipts.append({'at':at.isoformat(),'stage':stage,'id':j['id'],'state':state,'reservation_micro_usd':amount,'context_signature':j.get('context_signature'),'age_hours':age,'headline_signal':j.get('headline_signal',False),'followup_candidate':j.get('followup_candidate',False),'progress_candidate':j.get('progress_candidate',False),'due_related_candidate':j.get('due_related_candidate',False),'can_apply':False,'can_publish':False})
                ledger=native.read('ai_budget.json',{});day,month=native.periods(at);daily=ledger.get('days',{}).get(day,{});monthly=ledger.get('months',{}).get(month,{})
                if daily.get('calls',0)>40 or daily.get('reserved_micro_usd',0)>200000 or monthly.get('reserved_micro_usd',0)>5000000:raise AssertionError('native cap exceeded')
                upstream_calls=daily.get('calls',0)-daily.get('tracking_calls',0);upstream_cost=daily.get('reserved_micro_usd',0)-daily.get('tracking_micro_usd',0)
                if upstream_calls>24 or upstream_cost>100000:raise AssertionError('tracking protection weakened')
                trace.append({'at':at.isoformat(),'calls':dict(per_run),'deferrals':dict(failures),'waiting_article_versions':len(pending_versions()),'completed_mock_groups':sum(work_key(j) in completed for j in jobs['status']),'day':day,'daily_accounted_micro_usd':daily.get('reserved_micro_usd',0),'daily_calls':daily.get('calls',0),'run_calls':ledger.get('run',{}).get('calls',0),'monthly_accounted_micro_usd':monthly.get('reserved_micro_usd',0)})
        ledger=native.read('ai_budget.json',{})
    if jobs!=source or seed!=seed_before:raise AssertionError('input mutation')
    finish=at_start+timedelta(hours=24);remaining=[j for j in jobs['status'] if work_key(j) not in completed]
    max_age=max(((finish-dt(j['first_seen_at'])).total_seconds()/3600 for j in remaining if j.get('first_seen_at')),default=None)
    return {'plan':plan,'caps':PLANS[plan],'outcome':outcome,'cost_mode':cost_mode,'simulated_calls':dict(selected),'real_API_calls':0,
            'completed_actual_groups':0,'completed_mock_groups':sum(work_key(j) in completed for j in jobs['status']),'closed_mock_groups':sum(work_key(j) in completed for j in jobs['status']) if outcome=='mock_pass' else 0,'transferred_mock_article_versions':len(mock_tracking),'final_status_groups':sum(work_key(j) not in completed for j in jobs['status']),'initial_waiting_article_versions':initial,'final_waiting_article_versions':len(pending_versions()),
            'unexamined_pair_jobs':sum(work_key(j) not in attempted for j in jobs['pair']),'unexamined_merge_jobs':sum(work_key(j) not in attempted for j in jobs['merge']),'selected_count_by_stage':dict(selected),'attempted_work_ids':sorted(attempted),
            'remaining_blocked_groups':sum(blocked(j) for j in jobs['status']),'holds':dict(hold),'reserved_sum_micro_usd':reserved,
            'oldest_remaining_status_age_hours':round(max_age,2) if max_age is not None else None,
            'selected_headline_signal_groups':sum(r.get('headline_signal',False) for r in receipts if r['stage']=='status' and 'reservation_micro_usd' in r),
            'selected_oldest_status_age_hours':max((r['age_hours'] for r in receipts if r['stage']=='status' and r.get('age_hours') is not None),default=None),
            'checkpoint':{'contract':contract,'last_window':windows[-1].isoformat() if windows else resume.get('last_window'),'attempted':sorted(attempted),'completed':sorted(completed),'mock_tracking':sorted(mock_tracking),'receipts':receipts,'trace':trace,'reserved':reserved,'selected':dict(selected),'hold':dict(hold),'ledger':ledger,'initial_waiting':initial},'priority_policy':priority_policy,'trace':trace,'receipts':receipts,'ledger':ledger,'approval_count':0,'event_merges':0,'source_records_preserved':True,'inflow':'frozen_zero','tracking_demand':'no_new_tracking_calls; protected capacity remains available'}
