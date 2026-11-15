"""Local retrieval signals only. None establish identity or progress."""
import copy,re,hashlib,json
from datetime import date,datetime,timezone,timedelta
from .allocation import dt

POLICIES=('native','oldest_follow','oldest_progress','oldest_due')
PROGRESS=re.compile('起訴|判決|控訴|復旧|施行')

def work_key(job):
    value={k:job.get(k) for k in ('id','context_signature','validation_revision','prompt_bytes')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def signals(groups,events,at):
    """8-character shared title fragments are retrieval hints, not proof.

    No event/category/region/date cutoffs. All event IDs remain unexamined.
    """
    def fragments(text):
        text=text.rsplit(' - ',1)[0]
        return {text[i:i+8] for i in range(max(0,len(text)-7)) if not any(word in text[i:i+8] for word in ('ニュース','Yahoo','www.','https://'))}
    index=[];schedules=[]
    for event in events:
        evidence=[('event_name',event.get('event_name',''))]+[(a.get('url',''),a.get('title','')) for a in event.get('articles',[])]
        index.append((event['event_id'],{a.get('url') for a in event.get('articles',[])},[(url,fragments(text)) for url,text in evidence]))
        for schedule in event.get('schedules',[]):
            due=schedule.get('due_end') or schedule.get('due_start');state='date_unknown'
            if due:
                try:
                    end=date.fromisoformat(due[:10]);today=at.astimezone(timezone(timedelta(hours=9))).date()
                    state='overdue' if end<today else 'due_today' if end==today else 'future'
                except (ValueError,TypeError):state='date_unknown'
            if schedule.get('status') in ('cancelled','completed','実施済み','中止'):state='inactive'
            schedules.append({'event_id':event['event_id'],'schedule_id':schedule.get('schedule_id'),'state':state,'stored_due':due,'search_performed':False})
    due_events={s['event_id'] for s in schedules if s['state'] in ('due_today','overdue')}
    result={}
    for group in groups:
        matches=[];titles=' '.join(a.get('title','') for a in group.get('articles',[]));parts=set().union(*(fragments(a.get('title','')) for a in group.get('articles',[])));urls={a.get('url') for a in group.get('articles',[])}
        for eid,old_urls,evidence in index:
            overlap=[{'stored_source':source,'shared_fragment_count':len(parts&terms),'sample_shared_title_literals':sorted(parts&terms)[:3]} for source,terms in evidence if parts&terms]
            same_url=bool(urls&old_urls)
            if same_url or overlap:matches.append({'event_id':eid,'same_saved_url':same_url,'title_fragment_matches':overlap,'identity_confirmed':False})
        from pending_triage import group_key
        result[group_key(group)]={'followup_candidate':bool(matches),'progress_candidate':bool(PROGRESS.search(titles)),
            'due_related_candidate':any(m['event_id'] in due_events for m in matches),'retrieval_matches':matches,
            'unexamined_event_ids':[e['event_id'] for e in events],'match_is_reference_only':True}
    return result,schedules


def rank_jobs(items,policy,limit):
    if policy not in POLICIES:raise ValueError('unknown priority policy')
    if policy=='native':return items[:limit]
    remaining=list(items);selected=[]
    def oldest(j):return (dt(j['first_seen_at']) if j.get('first_seen_at') else datetime.max.replace(tzinfo=timezone.utc),j['id'])
    def pick(predicate):
        found=[j for j in remaining if predicate(j)]
        if not found:return False
        job=min(found,key=oldest);selected.append(job);remaining.remove(job);return True
    # An actually eligible oldest item always owns the first available slot.
    pick(lambda j:True)
    # 6 slots: oldest1, followup2/progress1 (or vice versa), ordinary2.
    lanes={'oldest_follow':('follow','progress','follow'),'oldest_progress':('progress','follow','progress'),'oldest_due':('due','progress','follow')}[policy]
    for lane in lanes:
        if len(selected)>=limit:break
        if lane=='due':pick(lambda j:j.get('due_related_candidate',False))
        elif lane=='follow':pick(lambda j:j.get('followup_candidate',False))
        else:
            if not pick(lambda j:j.get('progress_candidate',False)):pick(lambda j:j.get('due_related_candidate',False))
    while len(selected)<limit and pick(lambda j:not any(j.get(k,False) for k in ('followup_candidate','progress_candidate','due_related_candidate'))):pass
    while len(selected)<limit and pick(lambda j:True):pass
    return selected
