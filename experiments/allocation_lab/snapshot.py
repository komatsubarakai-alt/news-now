"""Read-only adapter. Real snapshots and generated receipts must stay private.

Only parses native status prompt syntax; never executes pipeline entrypoints.
"""
import ast,hashlib,json,re
from pathlib import Path
from queue_policy import prioritize_groups, article_fingerprint
from pending_triage import group_key
from ai_candidates import build_candidates
from .allocation import dt


def read(root,name):return json.loads((Path(root)/(name+'.json')).read_text())

def render(node,values):
    if isinstance(node,ast.Constant):return node.value
    if isinstance(node,ast.Name):return values[node.id]
    if isinstance(node,ast.JoinedStr):return ''.join(str(render(x,values)) for x in node.values)
    if isinstance(node,ast.FormattedValue):
        if node.conversion!=-1 or node.format_spec is not None:raise ValueError('unsupported native format')
        return render(node.value,values)
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='get' and isinstance(node.func.value,ast.Name) and node.func.value.id=='article' and not node.keywords:
        args=[render(x,values) for x in node.args]
        if args[0] not in ('title','published'):raise ValueError('unsupported article field')
        return values['article'].get(*args)
    raise ValueError('native prompt changed; recalibrate before comparing')


def status_prompt_factory(source):
    tree=ast.parse(source)
    articles=[n.value for n in ast.walk(tree) if isinstance(n,ast.AugAssign) and isinstance(n.target,ast.Name) and n.target.id=='article_text']
    prompts=[n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='prompt' for t in n.targets)]
    if len(articles)!=1 or len(prompts)!=1:raise ValueError('ambiguous native prompt')
    def prompt(group):
        text=''.join(render(articles[0],{'article':a}) for a in group.get('articles',[]))
        return render(prompts[0],{'event_name':group.get('event_name','名称未設定'),'article_text':text})
    return prompt


def adapt(root,profiles):
    root=Path(root);health=read(root,'queue_health');at=health['updated_at'];state=read(root,'status_triage')['items']
    memory=read(root,'queue_monitor_state').get('first_seen',{});collection=read(root,'collection_triage')['items']
    groups=prioritize_groups(read(root,'status_pending'),state,dt(at));make_prompt=status_prompt_factory((root/'news_status.py').read_text())
    jobs={'pair':[],'merge':[],'status':[]}
    for group in groups:
        key=group_key(group);entry=state.get(key,{})
        if entry.get('rejected_responses'):raise ValueError('rejected-answer snapshot requires native signature preflight; no inferred reuse')
        firsts=[dt(x) for x in [entry.get('first_seen_at')]+[memory.get(article_fingerprint(a)) for a in group.get('articles',[])]+[collection.get(a.get('url'),{}).get('first_seen_at') for a in group.get('articles',[])] if x]
        versions=[article_fingerprint(a) for a in group.get('articles',[])]
        # A signature is an identifier, never evidence that a response is reusable.
        signature=hashlib.sha256(json.dumps(group,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        jobs['status'].append({'id':'status-'+key,'stage':'status','versions':versions,'context_signature':signature,
            'first_seen_at':min(firsts).isoformat() if firsts else None,'priority_first_seen_at':entry.get('first_seen_at'),'prompt_bytes':len(make_prompt(group).encode('utf-8')),
            'headline_signal':bool(re.search('起訴|判決|控訴|復旧|施行',' '.join(a.get('title','') for a in group.get('articles',[]))))})
    candidates=build_candidates(read(root,'news_filtered'))
    # Candidate count is measured; their future prompt size uses historical calibration.
    for i,_ in enumerate(candidates):jobs['pair'].append({'id':'pair-'+str(i),'stage':'pair','prompt_bytes':profiles['pair']['prompt_bytes']})
    # Future group-candidate demand cannot be proved without pair outcomes.
    # Fixed synthetic demand is deliberately identical in all plans, not a measured queue.
    for i in range(16):jobs['merge'].append({'id':'synthetic-merge-'+str(i),'stage':'merge','prompt_bytes':profiles['merge']['prompt_bytes']})
    collection=read(root,'collection_triage')['items']
    others=[article_fingerprint(a) for a in read(root,'news_inbox') if collection.get(a.get('url'),{}).get('status','queued')=='queued']
    others += [article_fingerprint(a) for g in read(root,'tracking_pending') for a in g.get('articles',[])]
    return jobs,others,health


def summarize_wait(root,evidence=None):
    """Primary reasons with downstream precedence; suppression is an overlay.

    Evidence is supplied by a human-audited log, not inferred from budget labels.
    """
    from collections import Counter
    from .allocation import explain_wait,REASONS
    evidence=evidence or {};assignments={};stages={};secondary=0
    status=read(root,'status_triage')['items'];tracking=read(root,'tracking_triage')['items'];collection=read(root,'collection_triage')['items']
    inbox=[a for a in read(root,'news_inbox') if collection.get(a.get('url'),{}).get('status','queued')=='queued']
    sources={'collection':[({'articles':[a]},collection.get(a.get('url'),{})) for a in inbox],
             'status':[(g,status.get(group_key(g),{})) for g in read(root,'status_pending')],
             'tracking':[(g,tracking.get(group_key(g),{})) for g in read(root,'tracking_pending')]}
    for stage,items in sources.items():
        local={}
        for group,entry in items:
            category,detail=explain_wait(entry,stage,evidence.get(stage))
            for a in group.get('articles',[]):
                key=article_fingerprint(a);local[key]=category;assignments[key]=category
            if stage=='tracking' and entry.get('cached_rejection_waiting'):secondary+=len(group.get('articles',[]))
        stages[stage]=dict(Counter(local.values()))
    return {'primary_article_versions':{r:sum(x==r for x in assignments.values()) for r in REASONS},
            'stages':stages,'unique_waiting_versions':len(assignments),
            'tracking_cached_rejection_overlay_not_additive':secondary,
            'meaning':'last observed stopping reason; not necessarily initial historical cause'}
