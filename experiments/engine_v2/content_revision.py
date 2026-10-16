"""Offline source-version adapter. All legacy guards still run, unchanged.

Only disposable validation copies contain an explicitly labelled body-backed
source view in description. Canonical articles/RSS descriptions are not changed.
Every returned quotation must also match the original metadata or stored body.
"""
import copy
import hashlib
import html
import json
from pathlib import Path

from engine import atomic,connect,dumps,events,judge,project_event
from article_store import version_id
from failed_response import digest
from merge_safety import source_context,UpdateHeld,validate_saved_update
from pending_triage import source_saved,group_key,fingerprint
from operator_review import active_selection,digest as operator_digest
import revision_sync as sync

MARKER='\n\n【検証入力：保存済み本文。RSS抜粋とは別の原文】\n'


def state(db,sid,generations=None):
    row=db.execute('SELECT version_id,body_id,availability,components,url FROM sync_source_states WHERE state_id=?',(sid,)).fetchone()
    if row is None:raise ValueError('missing article state')
    metadata,bid,availability,components,url=row;components=json.loads(components);body=None
    if generations is None:
        selected=db.execute('SELECT state_id FROM sync_heads WHERE url=?',(url,)).fetchone()
        if selected!=(sid,):
            previous=db.execute('SELECT payload FROM sync_content_editions WHERE state_id=? ORDER BY rowid DESC LIMIT 1',(sid,)).fetchone()
            if previous is None:raise ValueError('historical article edition requires recorded generation')
            generations=json.loads(previous[0])['generations']
        else:generations={k:(db.execute('SELECT epoch FROM sync_component_epochs WHERE url=? AND component=?',(url,k)).fetchone() or (0,))[0] for k in ('metadata','body','availability')}
    if bid is not None and components['body'] is not None:
        saved=db.execute('SELECT representation,payload FROM sep_contents WHERE content_id=?',(bid,)).fetchone()
        if saved is None or saved[0]!='provided_body':raise ValueError('missing saved body')
        value=json.loads(saved[1]);body=value['body_text']
        if digest({'representation':'provided_body','content':value})!=bid:raise ValueError('saved body hash mismatch')
    result={'state_id':sid,'metadata_version':metadata,'body_hash':components['body'],
            'edition_id':digest({'metadata':metadata,'body':components['body'],'generations':generations}),'generations':generations,'availability':availability,'body_text':body}
    stored=db.execute('SELECT payload FROM sync_content_editions WHERE edition_id=?',(result['edition_id'],)).fetchone()
    if stored:
        representative=json.loads(stored[0])
        if any(representative[k]!=result[k] for k in ('metadata_version','body_hash','generations','body_text','availability')):raise ValueError('article edition content mismatch')
        return representative
    with atomic(db):db.execute('INSERT INTO sync_content_editions VALUES(?,?,?)',(result['edition_id'],sid,dumps(result)))
    return result


def bindings(db,eid):
    return {r[0]:{'edition_id':r[1],'state_id':r[2],'adoption_id':r[3]} for r in db.execute('SELECT url,edition_id,state_id,adoption_id FROM sync_content_heads WHERE event_id=?',(eid,))}


def prior_state(db,eid,article):
    b=bindings(db,eid).get(article['url'])
    if b is None:return None
    saved=db.execute('SELECT payload FROM sync_content_editions WHERE edition_id=?',(b['edition_id'],)).fetchone()
    if saved is None:raise ValueError('missing adopted article edition')
    stored=json.loads(saved[0]);value=state(db,b['state_id'],stored['generations'])
    if value['edition_id']!=b['edition_id']:raise ValueError('adopted article edition hash mismatch')
    return value if value['metadata_version']==version_id(article) else None


def body_urls(db,evidence_ids):
    return {r[0] for ev in evidence_ids for r in db.execute("SELECT url FROM sync_evidence WHERE evidence_id=? AND field='body'",(ev,))}


def view(article,source_state):
    body=source_state['body_text'] if source_state else None
    output=copy.deepcopy(article)
    if body is not None:
        # Escape only for the legacy HTML-to-text parser; origin checks below use
        # the untouched original text. Never save this as an RSS description.
        output['description']=output.get('description','')+MARKER+'版識別:'+source_state['edition_id']+'\n'+html.escape(body,quote=False)
    return output


def prepare(db,eid,group,evidence_ids):
    selected=body_urls(db,evidence_ids)
    owned={a['url']:a for e in events(db) if e['event_id']==eid for a in e.get('articles',[])}
    global_urls={a['url'] for e in events(db) for a in e.get('articles',[])}
    relevant=[]
    for a in group['articles']:
        if a['url'] not in selected:continue
        if a['url'] in owned:relevant.append(a)
        elif a['url'] not in global_urls:
            current=state(db,sync.head(db,a['url'])['state_id'])
            if current['body_text'] is not None and current['body_text'].strip() and current['availability']=='available':relevant.append(a)
    if not relevant:return None
    sources=[];waiting=None;changed=False
    for a in relevant:
        head=sync.head(db,a['url']);current=state(db,head['state_id']);before=prior_state(db,eid,owned[a['url']]) if a['url'] in owned else None
        if db.execute('SELECT 1 FROM sync_fetch_waits WHERE url=?',(a['url'],)).fetchone():waiting='source_fetch_pending'
        if current['body_text'] is None or not current['body_text'].strip() or current['availability']!='available':waiting='body_evidence_unavailable'
        changed |= before is None or before['edition_id']!=current['edition_id']
        sources.append({'url':a['url'],'before':before,'after':current})
    return {'event_id':eid,'sources':sources,'changed':changed,'waiting':waiting,
            'adapter_code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def views(db,eid,group,evidence_ids):
    selected=body_urls(db,evidence_ids);catalog=events(db);mapping={};origins={}
    def mapped(article,old_state=None):
        v=view(article,old_state)
        mapping[digest(v)]=copy.deepcopy(article)
        origins[digest(v)]={'article':copy.deepcopy(article),'state':copy.deepcopy(old_state)}
        return v
    output=[]
    for event in catalog:
        row=copy.deepcopy(event)
        row['articles']=[mapped(a,prior_state(db,event['event_id'],a)) for a in event.get('articles',[])]
        output.append(row)
    # Preserve the target event's historical state when another event happens
    # to have an identical source view from a different observation record.
    target=next(e for e in catalog if e['event_id']==eid)
    for article in target.get('articles',[]):mapped(article,prior_state(db,eid,article))
    incoming=copy.deepcopy(group)
    incoming['articles']=[mapped(a,state(db,sync.head(db,a['url'])['state_id']) if a['url'] in selected else None) for a in group['articles']]
    return output,incoming,mapping,origins


def quote_origin(quote,article,origin):
    if not isinstance(quote,str) or not quote:return False
    meta=source_context(origin['article'])
    body=(origin['state'] or {}).get('body_text')
    return quote in meta['title'] or quote in meta['description'] or (body is not None and quote in body)


def check_answer_origins(answer,before,group,origins):
    old={a['url']:a for a in before['articles']};new={a['url']:a for a in group['articles']}
    def check(url,quote,sources):
        article=sources.get(url)
        if article is None or not quote_origin(quote,article,origins[digest(article)]):raise UpdateHeld('quotation_not_in_original_source_version')
    for match in answer.get('identity_matches',[]):
        for anchor in match.get('anchors',[]):
            check(match.get('existing_url'),anchor.get('existing_quote'),old)
            check(match.get('incoming_url'),anchor.get('incoming_quote'),new)
    for item in answer.get('progress_evidence',[]):check(item.get('url'),item.get('quote'),new)
    for item in answer.get('schedules',[]):check(item.get('source_url'),item.get('quote') or item.get('date_text'),new)
    identity=answer.get('event_identity',{})
    if identity.get('quote'):check(identity.get('url'),identity['quote'],new)
    if answer.get('completion_quote') and not any(
        url in new and quote_origin(answer['completion_quote'],new[url],origins[digest(new[url])]) for url in answer.get('evidence_urls',[])):
        raise UpdateHeld('completion_quote_not_in_original_source_version')


def validate(db,c,answer,now):
    """Run engine.judge on a temporary source-view DB, then save canonical data.

    No force/skip-validation flag. The original preflight, ownership restriction,
    identity, contradiction, progress and final-save guards are all invoked.
    """
    eid=c['event_id'];catalog,group,mapping,origins=views(db,eid,c['group'],c['evidence'])
    before=next(e for e in catalog if e['event_id']==eid);input_hash=digest(catalog+[group])
    try:check_answer_origins(answer,before,group,origins)
    except (UpdateHeld,TypeError,KeyError,AttributeError):return 'quote_validation_failed',None
    guard=connect()
    try:
        with atomic(guard):
            for e in catalog:guard.execute('INSERT INTO events VALUES(?,?)',(e['event_id'],dumps(e)))
        triage=copy.deepcopy(c['triage']);key=group_key(c['group']);item=triage.get(key,{})
        if item.get('source_fingerprint')==fingerprint(c['group']):item['source_fingerprint']=fingerprint(group)
        selection=active_selection(c['group'],c['triage'])
        if selection:
            item=triage.setdefault(key,item);item['operator_review']=copy.deepcopy(selection)
            item['operator_review'].update(source_fingerprint=fingerprint(group),group_hash=operator_digest(group))
        outcome=judge(guard,group,answer,now,triage=triage,registry=c['registry'],presented_ids=c['presented_ids'],rerank=False,known_event_id=eid)
        if outcome!='accepted':
            diagnostic=guard.execute("SELECT payload FROM holds WHERE stage='judgment' AND group_key=?",(group_key(group),)).fetchone()
            reason=json.loads(diagnostic[0]).get('reason',outcome) if diagnostic else outcome
            with atomic(db):db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',('body_revision:'+eid,group_key(c['group']),dumps({'group':c['group'],'status':outcome,'reason':reason,'source_editions':c['content_revision']})))
            return outcome,input_hash
        after=next(e for e in events(guard) if e['event_id']==eid)
        canonical=copy.deepcopy(after)
        canonical['articles']=[copy.deepcopy(mapping[digest(a)]) for a in after['articles']]
        # Existing historical entries are untouched. Only convert the newly
        # appended source views, recording the old body edition beside metadata.
        history=canonical.get('source_revisions',[])
        for item in history[len(before.get('source_revisions',[])):]:
            viewed=item['article'];origin=origins[digest(viewed)];item['article']=copy.deepcopy(origin['article'])
            if origin['state']:
                item['content_edition_id']=origin['state']['edition_id'];item['body_content_hash']=origin['state']['body_hash'];item['source_state_id']=origin['state']['state_id']
        actual_before=next(e for e in events(db) if e['event_id']==eid)
        try:validate_saved_update(actual_before,canonical)
        except UpdateHeld as exc:
            with atomic(db):db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',('body_revision:'+eid,group_key(c['group']),dumps({'group':c['group'],'status':'needs_evidence','reason':str(exc),'source_editions':c['content_revision']})))
            return 'needs_evidence',input_hash
        with atomic(db):
            db.execute('UPDATE events SET payload=? WHERE event_id=?',(dumps(canonical),eid));project_event(db,canonical,'body_revision_legacy_guards')
            db.execute('INSERT INTO changes(event_id,before_payload,after_payload,reason) VALUES(?,?,?,?)',(eid,dumps(actual_before),dumps(canonical),'body_revision_legacy_guards'))
        return outcome,input_hash
    finally:guard.close()


def record_bindings(db,eid,group,evidence_ids,adoption):
    before=bindings(db,eid);after=copy.deepcopy(before);selected=body_urls(db,evidence_ids)
    current_articles={a['url']:a for e in events(db) if e['event_id']==eid for a in e.get('articles',[])}
    considered={a['url']:a for a in group['articles']}
    considered.update({u:a for u,a in current_articles.items() if u in selected})
    for article in considered.values():
        url=article['url']
        if url in selected and sync.head(db,url)['version_id']==version_id(article):
            current=state(db,sync.head(db,url)['state_id'])
            if current['body_text'] is not None:
                db.execute('INSERT OR IGNORE INTO sync_content_editions VALUES(?,?,?)',(current['edition_id'],current['state_id'],dumps(current)))
                after[url]={'edition_id':current['edition_id'],'state_id':current['state_id'],'adoption_id':adoption}
        elif url in after and state(db,after[url]['state_id'])['metadata_version']!=version_id(article):after.pop(url)
    with atomic(db):
        db.execute('INSERT INTO sync_content_changes VALUES(?,?,?)',(adoption,dumps(before),dumps(after)))
        replace_bindings(db,eid,after)


def replace_bindings(db,eid,values):
    db.execute('DELETE FROM sync_content_heads WHERE event_id=?',(eid,))
    for url,item in values.items():db.execute('INSERT INTO sync_content_heads VALUES(?,?,?,?,?)',(eid,url,item['edition_id'],item['state_id'],item['adoption_id']))


def restore_bindings(db,eid,adoption):
    row=db.execute('SELECT before_payload,after_payload FROM sync_content_changes WHERE adoption_id=?',(adoption,)).fetchone()
    if row is None:return
    before,after=map(json.loads,row)
    if bindings(db,eid)!=after:raise ValueError('later_body_binding_requires_review')
    replace_bindings(db,eid,before)


def result_status(db,result):
    # A historical operation receipt is not a new/current judgment.
    did=result.get('decision_id')
    if did:
        row=db.execute('SELECT status FROM sync_decision_state WHERE decision_id=?',(did,)).fetchone()
        if row is None or row[0]!='current':return {'status':'needs_recheck','previous_decision_id':did,'job_id':result.get('job_id')}
    return result


def request_status(db,request_id,command,status,now,detail):
    previous=db.execute('SELECT transition_id,new_status FROM sync_revision_transitions WHERE request_id=? ORDER BY rowid DESC LIMIT 1',(request_id,)).fetchone()
    old=previous[1] if previous else None
    if old!=status:
        db.execute('INSERT INTO sync_revision_transitions VALUES(?,?,?,?,?)',(digest([request_id,previous[0] if previous else None,status,now.isoformat()]),request_id,old,status,now.isoformat()))
    db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',('revision:'+request_id,group_key(command['group']),dumps(command|detail|{'status':status,'terminal':status not in ('ready','budget_deferred','source_fetch_pending','body_evidence_unavailable','uncertain_response_requires_reconciliation','reservation_period_requires_reconciliation')})))


def rejudge(db,request_id,eid,group,evidence_ids,mock,now,*,triage=None,registry=None,failpoint=None,transport_failpoint=None,supersedes=None):
    """Persist inputs, resume saved/mock processing, never implicitly approve."""
    sync.initialize(db)
    if not isinstance(request_id,str) or not request_id:raise ValueError('request ID required')
    command={'event':eid,'group':group,'evidence':sorted(set(evidence_ids)),'triage':triage or {},'registry':registry or {},'supersedes':supersedes}
    row=db.execute('SELECT command_hash,job_id,result FROM sync_revision_requests WHERE request_id=?',(request_id,)).fetchone()
    if row:
        if row[0]!=digest(command):raise ValueError('request ID reused with different revision inputs')
        jid=row[1]
        if row[2] is not None:return result_status(db,json.loads(row[2]))
        completed=sync.receipt(db,'revision:'+request_id,{'action':'adopt','job':jid,'supersedes':supersedes})
        if completed is not None:
            result=completed|{'job_id':jid}
            with atomic(db):
                db.execute('UPDATE sync_revision_requests SET result=? WHERE request_id=?',(dumps(result),request_id))
                request_status(db,request_id,command,result['status'],now,result)
            return result_status(db,result)
    else:
        try:
            if not db.execute('SELECT 1 FROM events WHERE event_id=?',(eid,)).fetchone():raise ValueError('unknown event target')
            jid=sync.make_job(db,eid,group,evidence_ids,now,triage,registry)
        except ValueError as exc:
            result={'status':'needs_evidence','reason':str(exc)}
            with atomic(db):
                db.execute('INSERT INTO sync_revision_requests VALUES(?,?,?,?,?)',(request_id,digest(command),None,dumps(result),dumps(command)))
                request_status(db,request_id,command,result['status'],now,result)
            return result
        with atomic(db):
            db.execute('INSERT INTO sync_revision_requests VALUES(?,?,?,NULL,?)',(request_id,digest(command),jid,dumps(command)))
            request_status(db,request_id,command,'ready',now,{})
    reply=sync.call_mock(db,jid,mock,now,failpoint=transport_failpoint)
    if reply['status']!='cached':
        with atomic(db):request_status(db,request_id,command,reply['status'],now,reply)
        return reply|{'job_id':jid}
    result=sync.adopt(db,'revision:'+request_id,jid,now,failpoint=failpoint,supersedes=supersedes)|{'job_id':jid}
    with atomic(db):
        db.execute('UPDATE sync_revision_requests SET result=? WHERE request_id=?',(dumps(result),request_id))
        request_status(db,request_id,command,result['status'],now,result)
        if result['status']=='accepted':db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',('body_revision:'+eid,group_key(group),dumps({'group':group,'status':'accepted','decision_id':result['decision_id'],'terminal':True})))
    return result


def reported_evidence(db,c,answer):
    """Bind validated incoming quotations actually used by the new judgment."""
    pairs=[]
    for match in answer.get('identity_matches',[]):
        pairs.extend((match.get('incoming_url'),a.get('incoming_quote')) for a in match.get('anchors',[]))
    pairs.extend((p.get('url'),p.get('quote')) for p in answer.get('progress_evidence',[]))
    pairs.extend((p.get('source_url'),p.get('quote') or p.get('date_text')) for p in answer.get('schedules',[]))
    item=answer.get('event_identity',{})
    if item.get('quote'):pairs.append((item.get('url'),item['quote']))
    if answer.get('completion_quote'):pairs.extend((url,answer['completion_quote']) for url in answer.get('evidence_urls',[]))
    incoming={a['url']:a for a in c['group']['articles']};result=set(c['evidence'])
    for url,quote in pairs:
        if url not in incoming or not isinstance(quote,str) or not quote or len(quote)>500:continue
        current=state(db,sync.head(db,url)['state_id']);article=incoming[url];field=None
        if current['body_text'] is not None and quote in current['body_text']:field='body'
        elif quote in article.get('title',''):field='title'
        elif quote in article.get('description',''):field='description'
        if field:
            eid=sync.add_evidence(db,c['event_id'],url,field,quote)
            if sync.verify_quote(db,eid,current=True)['status']=='exact_literal_match':result.add(eid)
    return sorted(result)
