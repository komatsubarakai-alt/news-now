"""Publication authorization rehearsal, only memory or temporary SQLite.

No real authorization, human identity service, production writer or AI client.
Every approval requires explicit simulated operator action plus literal, current
proof and a current adoption that passed the unchanged legacy safety guards.
"""
import copy
import json
import tempfile
from datetime import datetime
from pathlib import Path

from engine import atomic,dumps,events
from failed_response import digest
from article_store import version_id
from publication_safety import protect_existing,public_events
import revision_sync as sync

FIELDS={'current_stage','summary','latest','next_watch','lifecycle','updated_at'}
LISTS={'progress':'progress_history','schedule':'schedules','correction':'corrections','revision':'source_revisions'}


def isolated(db):
    for _,_,name in db.execute('PRAGMA database_list'):
        if name and not Path(name).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError('publication rehearsal requires memory or a temporary SQLite')


def initialize(db):
    isolated(db);sync.initialize(db)
    with atomic(db):
        for statement in Path(__file__).with_suffix('.sql').read_text().split(';'):
            if statement.strip():db.execute(statement)


def actor_time(actor,at):
    if not isinstance(actor,str) or not actor.startswith('simulation:') or not actor[11:].strip() or len(actor)>100 or any(ord(c)<32 for c in actor):raise ValueError('explicit simulated actor required')
    if not isinstance(at,datetime) or at.tzinfo is None or at.utcoffset() is None:raise ValueError('timezone-aware approval time required')
    return at.isoformat()


def key_for(kind,value):
    if kind=='article':return value['url']
    if kind=='schedule':return value['schedule_id']
    return digest(value)


def target(event,kind,key):
    if kind=='event':
        if key!='identity':raise ValueError('unknown event target')
        return {k:copy.deepcopy(event[k]) for k in ('event_id','event_name','category','tracking_value') if k in event}
    if kind=='field':
        if key not in FIELDS or key not in event:raise ValueError('unknown field target')
        return copy.deepcopy(event[key])
    if kind=='completion':
        if key!='evidence' or not event.get('completion_evidence'):raise ValueError('missing completion evidence')
        return copy.deepcopy(event['completion_evidence'])
    name='articles' if kind=='article' else LISTS.get(kind)
    if not name:raise ValueError('unknown target kind')
    matches=[item for item in event.get(name,[]) if key_for(kind,item)==key]
    if len(matches)!=1:raise ValueError('target missing or ambiguous')
    return copy.deepcopy(matches[0])


def event_by_id(db,eid):
    row=db.execute('SELECT payload FROM events WHERE event_id=?',(eid,)).fetchone()
    if row is None:raise ValueError('unknown event')
    return json.loads(row[0])


def receipt(db,op,command):
    if not isinstance(op,str) or not op:raise ValueError('operation ID required')
    row=db.execute('SELECT command_hash,result FROM pub_operations WHERE operation_id=?',(op,)).fetchone()
    if row:
        if row[0]!=digest(command):raise ValueError('operation ID reused for different approval')
        return json.loads(row[1])


def finish(db,op,command,result):
    db.execute('INSERT INTO pub_operations VALUES(?,?,?)',(op,digest(command),dumps(result)));return result


def transition(db,aid,status,op,actor,at,reason):
    old=db.execute('SELECT status FROM pub_state WHERE approval_id=?',(aid,)).fetchone()[0]
    if old==status:return
    db.execute('UPDATE pub_state SET status=? WHERE approval_id=?',(status,aid))
    db.execute('INSERT INTO pub_history VALUES(?,?,?,?,?,?,?,?)',(digest([op,aid,old,status]),aid,op,old,status,actor,at,reason))
    if db.execute('SELECT 1 FROM pub_generated WHERE approval_id=?',(aid,)).fetchone():
        eid=db.execute('SELECT event_id FROM pub_approvals WHERE approval_id=?',(aid,)).fetchone()[0]
        # Only generic system messages, not a copy of the withdrawn facts or a
        # private actor/reason/URL. Existing 34 records do not generate notices.
        message={'needs_recheck':'根拠または内容の変更により再確認中です。以前の内容は表示していません。',
                 'withdrawn':'この情報の掲載を取り消しました。以前の内容は表示していません。',
                 'superseded':'確認記録を更新しました。以前の承認は使用しません。'}[status]
        db.execute('INSERT OR IGNORE INTO pub_notices VALUES(?,?,?,?,?,?)',(digest([op,aid,status]),eid,aid,status,at,message))


def decision_proof(db,eid,dids,kind,key,content):
    if not dids:raise ValueError('guarded decision required')
    result=[];supported=False;dependencies=set()
    for did in sorted(set(dids)):
        row=db.execute('SELECT d.event_id,d.outcome,d.verification,s.status,c.after_payload,a.cancelled FROM sync_decisions d JOIN sync_decision_state s USING(decision_id) JOIN sync_adoptions a USING(decision_id) JOIN changes c ON c.id=a.change_id WHERE d.decision_id=?',(did,)).fetchone()
        if row is None or row[:4]!=(eid,'accepted','legacy_guard_result','current') or row[5]:raise ValueError('current guarded adoption required, not a review or imported AI verdict')
        after=json.loads(row[4])
        try:supported |= digest(target(after,kind,key))==digest(content)
        except (ValueError,KeyError):pass
        ids=[r[0] for r in db.execute('SELECT evidence_id FROM sync_dependencies WHERE decision_id=?',(did,))]
        if not all(sync.evidence_current(db,i) for i in ids):raise ValueError('decision evidence is stale')
        if any(db.execute('SELECT 1 FROM pub_evidence_invalidations WHERE evidence_id=?',(i,)).fetchone() for i in ids):raise ValueError('decision evidence explicitly invalidated')
        dependencies.update(ids)
        result.append({'decision_id':did,'after_hash':digest(after),'dependencies':ids,'generations':sync.source_generations(db,ids)})
    if not supported:raise ValueError('target content not supported by guarded adoption snapshot')
    return result,dependencies


def evidence_proof(db,eid,ids,dependencies):
    if not ids:raise ValueError('literal proof required')
    result=[]
    for ev in sorted(set(ids)):
        row=db.execute('SELECT event_id,url,state_id,field,quote,expected FROM sync_evidence WHERE evidence_id=?',(ev,)).fetchone()
        if row is None or row[0]!=eid or ev not in dependencies:raise ValueError('evidence not in guarded decision scope')
        quote=sync.verify_quote(db,ev,current=True)
        if quote['status']!='exact_literal_match':raise ValueError('current exact literal quote required')
        head=sync.head(db,row[1])
        if head['availability'] in ('deleted','empty_body'):raise ValueError('source unavailable')
        result.append({'evidence_id':ev,'url':row[1],'state_id':row[2],'field':row[3],'quote':row[4],
                       'article_version':head['version_id'],'content_hash':quote['content_hash'],
                       'expected':json.loads(row[5]),'generations':sync.source_generations(db,[ev])[ev]})
    return result


def approval_reason(db,row):
    aid,eid,kind,key,expected_hash,content,actor,at,basis,proof,scope=row
    try:
        if digest(json.loads(content))!=expected_hash:return 'approval_content_corrupt'
        current=target(event_by_id(db,eid),kind,key)
        if digest(current)!=expected_hash:return 'target_content_changed'
        p=json.loads(proof)
        decision_proof(db,eid,[v['decision_id'] for v in p['decisions']],kind,key,current)
        for old in p['decisions']:
            ids=old['dependencies']
            if sync.source_generations(db,ids)!=old['generations']:return 'decision_source_generation_changed'
        for old in p['evidence']:
            ev=old['evidence_id'];head=sync.head(db,old['url'])
            if head['version_id']!=old['article_version']:return 'article_version_changed'
            if sync.source_generations(db,[ev])[ev]!=old['generations']:return 'proof_generation_changed'
            check=sync.verify_quote(db,ev,current=True)
            if check['status']!='exact_literal_match' or check['content_hash']!=old['content_hash']:return 'proof_expired'
        if kind=='article':
            head=sync.head(db,key)
            if head['version_id']!=version_id(current) or head['availability'] in ('deleted','empty_body'):return 'article_current_version_mismatch'
        for required, in db.execute('SELECT required_id FROM pub_dependencies WHERE approval_id=?',(aid,)):
            if db.execute('SELECT status FROM pub_state WHERE approval_id=?',(required,)).fetchone()!=('approved',):return 'required_approval_not_current'
        if db.execute('SELECT 1 FROM sync_event_holds WHERE event_id=?',(eid,)).fetchone():return 'event_judgment_on_hold'
        operator=db.execute("SELECT d.outcome,s.status FROM sync_decisions d JOIN sync_decision_state s USING(decision_id) WHERE d.event_id=? AND d.kind='operator' AND s.status IN ('current','needs_recheck')",(eid,)).fetchall()
        if any(s=='needs_recheck' or o in ('hold','different_event') for o,s in operator):return 'operator_reconfirmation_required'
    except (ValueError,KeyError,TypeError):return 'proof_or_decision_not_current'
    return None


def refresh(db,op='publication-refresh',at=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='pub_approvals'").fetchone():return []
    # No invented observation time: hook passes the operation time, and read-only
    # status checks may store an explicit unknown timestamp instead of "now".
    stamp=at.isoformat() if at is not None else 'unrecorded-observation-time'
    affected=[]
    with atomic(db):
        while True:
            changed=False
            for row in db.execute("SELECT a.* FROM pub_approvals a JOIN pub_state s USING(approval_id) WHERE s.status='approved'").fetchall():
                reason=approval_reason(db,row)
                if reason:
                    transition(db,row[0],'needs_recheck',op,'system:dependency-check',stamp,reason)
                    affected.append(row[0]);changed=True
            if not changed:break
    return affected


def verify_display_quotes(value,proof):
    """Every quotation that would be shown needs its own literal source proof.

    A valid quote elsewhere in the article is not a substitute for this quote.
    Narrative fields are explicit operator-reviewed claims, not literal quotes.
    """
    pairs={(p['url'],p['quote']) for p in proof}
    if isinstance(value,dict):
        quote=value.get('quote')
        if quote:
            url=value.get('url') or value.get('source_url')
            urls=[url] if isinstance(url,str) else value.get('urls',[])
            if not any((u,quote) in pairs for u in urls):raise ValueError('displayed quotation lacks matching version-bound proof')
        for item in value.values():verify_display_quotes(item,proof)
    elif isinstance(value,list):
        for item in value:verify_display_quotes(item,proof)


def approve(db,op,eid,kind,key,*,expected_hash,actor,at,evidence_ids,decision_ids,basis,requires=(),supersedes=None,failpoint=None):
    initialize(db);stamp=actor_time(actor,at)
    if not isinstance(basis,str) or not basis.strip() or len(basis)>1000:raise ValueError('explicit approval basis required')
    command={'event':eid,'kind':kind,'key':key,'hash':expected_hash,'actor':actor,'at':stamp,'evidence':sorted(set(evidence_ids)),'decisions':sorted(set(decision_ids)),'basis':basis,'requires':sorted(set(requires)),'supersedes':supersedes}
    cached=receipt(db,op,command)
    if cached is not None:return cached # Receipt never reactivates an old approval.
    with atomic(db):
        refresh(db,op,at);event=event_by_id(db,eid);content=target(event,kind,key)
        if digest(content)!=expected_hash:raise ValueError('target content hash mismatch')
        old=db.execute('SELECT approval_id FROM pub_heads WHERE event_id=? AND kind=? AND target_key=?',(eid,kind,key)).fetchone()
        if (old is not None and old[0]!=supersedes) or (old is None and supersedes is not None):raise ValueError('explicit supersedes must match current approval head')
        ds,dependencies=decision_proof(db,eid,decision_ids,kind,key,content)
        proof=evidence_proof(db,eid,evidence_ids,dependencies)
        verify_display_quotes(content,proof)
        if kind=='article' and key not in {p['url'] for p in proof}:raise ValueError('article needs proof from its own version')
        if supersedes in requires:raise ValueError('replacement cannot depend on superseded approval')
        for required in set(requires):
            r=db.execute('SELECT a.event_id,s.status FROM pub_approvals a JOIN pub_state s USING(approval_id) WHERE a.approval_id=?',(required,)).fetchone()
            if r!=(eid,'approved'):raise ValueError('required approval missing, stale or another event')
        aid=digest({'operation':op,'command':command})
        db.execute('INSERT INTO pub_approvals VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,eid,kind,key,expected_hash,dumps(content),actor,stamp,basis,dumps({'decisions':ds,'evidence':proof}),'isolated_simulation'))
        db.execute('INSERT INTO pub_state VALUES(?,?)',(aid,'approved'))
        for required in set(requires):db.execute('INSERT INTO pub_dependencies VALUES(?,?)',(aid,required))
        db.execute('INSERT INTO pub_history VALUES(?,?,?,?,?,?,?,?)',(digest([op,aid]),aid,op,None,'approved',actor,stamp,basis))
        db.execute('INSERT OR REPLACE INTO pub_heads VALUES(?,?,?,?)',(eid,kind,key,aid))
        reason=approval_reason(db,db.execute('SELECT * FROM pub_approvals WHERE approval_id=?',(aid,)).fetchone())
        if reason:raise ValueError('new approval not valid: '+reason)
        if supersedes:
            transition(db,supersedes,'superseded',op,actor,stamp,'explicit reapproval')
            if db.execute('SELECT 1 FROM pub_generated WHERE approval_id=?',(supersedes,)).fetchone():
                db.execute('INSERT INTO pub_notices VALUES(?,?,?,?,?,?)',(digest([op,aid,'reapproved']),eid,aid,'reapproved',stamp,'内容と根拠を再確認し、掲載承認を更新しました。'))
        refresh(db,op,at)
        sync.fault('after_approval',failpoint)
        result=finish(db,op,command,{'approval_id':aid,'scope':'isolated_simulation','status':'recorded'})
    sync.fault('after_commit',failpoint);return result


def withdraw(db,op,aid,*,actor,at,reason,failpoint=None):
    initialize(db);stamp=actor_time(actor,at)
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('withdrawal reason required')
    command={'action':'withdraw','approval':aid,'actor':actor,'at':stamp,'reason':reason};cached=receipt(db,op,command)
    if cached is not None:return cached
    with atomic(db):
        if not db.execute('SELECT 1 FROM pub_approvals WHERE approval_id=?',(aid,)).fetchone():raise ValueError('unknown approval')
        transition(db,aid,'withdrawn',op,actor,stamp,reason);refresh(db,op,at)
        sync.fault('after_withdrawal',failpoint)
        result=finish(db,op,command,{'approval_id':aid,'status':'withdrawn'})
    sync.fault('after_commit',failpoint);return result


def embedded_urls(value):
    result=set()
    if isinstance(value,dict):
        for key,v in value.items():
            if key in ('url','source_url','incoming_url','existing_url') and isinstance(v,str):result.add(v)
            elif key in ('evidence_urls','source_urls','urls') and isinstance(v,list):result.update(x for x in v if isinstance(x,str))
            else:result.update(embedded_urls(v))
    elif isinstance(value,list):
        for v in value:result.update(embedded_urls(v))
    return result


def project(db,registry):
    initialize(db);refresh(db)
    candidates=copy.deepcopy(events(db));protect_existing(candidates,registry)
    eligible={e['event_id']:e for e in public_events(candidates)}
    # A valid approval does not override the unchanged legacy publication gates.
    active={};holds=[];used=[]
    for row in db.execute("SELECT a.* FROM pub_approvals a JOIN pub_state s USING(approval_id) JOIN pub_heads h ON h.approval_id=a.approval_id WHERE s.status='approved'"):
        active[(row[1],row[2],row[3])]=row
    output=[]
    for event in events(db):
        eid=event['event_id'];identity=active.get((eid,'event','identity'))
        if eid not in eligible or identity is None:
            holds.append({'event_id':eid,'reason':'legacy_publication_gate' if eid not in eligible else 'event_identity_unapproved'});continue
        approved_articles=[(a,active[(eid,'article',a['url'])]) for a in event.get('articles',[]) if (eid,'article',a['url']) in active]
        for a in event.get('articles',[]):
            if (eid,'article',a['url']) not in active:holds.append({'event_id':eid,'kind':'article','target_ref':digest([eid,'article',a['url']]),'reason':'article_unapproved'})
        if not approved_articles:holds.append({'event_id':eid,'reason':'no_approved_current_article'});continue
        urls={a['url'] for a,_ in approved_articles}
        def usable(row):
            proof=json.loads(row[9])
            return {e['url'] for e in proof['evidence']}|embedded_urls(json.loads(row[5])) <= urls
        if not usable(identity):holds.append({'event_id':eid,'reason':'identity_source_article_unapproved'});continue
        result=json.loads(identity[5]);accepted=[identity[0]]+[r[0] for _,r in approved_articles]
        result.update(articles=[copy.deepcopy(a) for a,_ in approved_articles],article_count=len(approved_articles),
            current_stage='確認中',summary='確認中',latest='確認中',next_watch='確認中',lifecycle='review',progress_count=0,progress_history=[],schedules=[],corrections=[],source_revisions=[])
        for key in sorted(FIELDS):
            row=active.get((eid,'field',key))
            if row is not None and usable(row):result[key]=json.loads(row[5]);accepted.append(row[0])
            elif row is not None:holds.append({'event_id':eid,'kind':'field','target_ref':digest([eid,'field',key]),'reason':'source_article_unapproved'})
        for kind,name in LISTS.items():
            for item in event.get(name,[]):
                row=active.get((eid,kind,key_for(kind,item)))
                if row is not None and usable(row):result[name].append(copy.deepcopy(item));accepted.append(row[0])
                else:holds.append({'event_id':eid,'kind':kind,'target_ref':digest([eid,kind,key_for(kind,item)]),'reason':'item_unapproved' if row is None else 'source_article_unapproved'})
        completion=active.get((eid,'completion','evidence'))
        if result['lifecycle']=='completed':
            if completion is None or not usable(completion):result['lifecycle']='review'
            else:result['completion_evidence']=json.loads(completion[5]);accepted.append(completion[0])
        result['progress_count']=len(result['progress_history'])
        if result['progress_history']:result['last_progress_at']=result['progress_history'][-1]['at']
        # Preserve identifiers and approved URLs only; unknown raw fields and
        # unapproved source revisions never leak through an event-wide copy.
        output.append(result);used.extend(accepted)
        omitted=[key for key in FIELDS if key in event and (eid,'field',key) not in active]
        if omitted:holds.append({'event_id':eid,'reason':'some_fields_unapproved','fields':sorted(omitted)})
    notices=[]
    for eid,aid,status,at,message,kind,key in db.execute('SELECT n.event_id,n.approval_id,n.status,n.at,n.message,a.kind,a.target_key FROM pub_notices n JOIN pub_approvals a USING(approval_id) ORDER BY n.rowid'):
        current=db.execute('SELECT s.status FROM pub_heads h JOIN pub_state s USING(approval_id) WHERE h.event_id=? AND h.kind=? AND h.target_key=?',(eid,kind,key)).fetchone()
        notices.append({'event_id':eid,'target_kind':kind,'target_ref':digest([eid,kind,key]),'status':status,'current_status':current[0] if current else 'unapproved',
                        'event_visible':eid in {e['event_id'] for e in output},'at':None if at=='unrecorded-observation-time' else at,'message':message})
    return {'events':output,'holds':holds,'notices':notices,'used_approval_ids':sorted(set(used)),'scope':'isolated_simulation','deployable':False}


def mark_generated(db,generation,ids):
    with atomic(db):
        for aid in ids:db.execute('INSERT OR IGNORE INTO pub_generated VALUES(?,?)',(aid,generation))


def invalidate_evidence(db,op,evidence_id,*,actor,at,reason,failpoint=None):
    """Explicit simulated evidence withdrawal; preserve the original quotation.

    Revocation is permanent for this evidence identity. A replacement needs a
    newly verified evidence version and a fresh explicit publication approval.
    """
    initialize(db);stamp=actor_time(actor,at)
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('invalidation reason required')
    command={'action':'invalidate_evidence','evidence':evidence_id,'actor':actor,'at':stamp,'reason':reason}
    cached=receipt(db,op,command)
    if cached is not None:return cached
    with atomic(db):
        if not db.execute('SELECT 1 FROM sync_evidence WHERE evidence_id=?',(evidence_id,)).fetchone():raise ValueError('unknown evidence')
        db.execute('INSERT INTO pub_evidence_invalidations VALUES(?,?,?,?,?,?)',(digest(command),evidence_id,actor,stamp,reason,op))
        affected=refresh(db,op,at);sync.fault('after_evidence_invalidation',failpoint)
        result=finish(db,op,command,{'evidence_id':evidence_id,'affected_approvals':affected})
    sync.fault('after_commit',failpoint);return result
