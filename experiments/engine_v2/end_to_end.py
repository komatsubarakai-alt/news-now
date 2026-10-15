"""Offline-only integrated pipeline. No HTTP/AI client or production writer.

All model results are rehearsal results, never publication authority. Imported
public snapshots are comparison references, not newly verified evidence.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

from ai_cost_control import BudgetExceeded,periods
from engine import atomic,dumps,events,judge,reserve_mock
from failed_response import digest
from article_store import import_saved
from publication_safety import protect_existing,public_events
import revision_sync as sync
import staged


def initialize(db):
    sync.initialize(db)
    with atomic(db):
        db.execute('CREATE TABLE IF NOT EXISTS e2e_answers (cache_key TEXT PRIMARY KEY, prompt TEXT NOT NULL, batch TEXT NOT NULL, basis TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS e2e_decisions (session_id TEXT, position INTEGER, decision_id TEXT, outcome TEXT NOT NULL, PRIMARY KEY(session_id,position,decision_id))')
        db.execute('CREATE TABLE IF NOT EXISTS e2e_exports (generation TEXT PRIMARY KEY, payload TEXT NOT NULL, context_hash TEXT NOT NULL)')


def seed_inputs(db,group):
    # Explicit snapshot selection; never infer a newer version from a date.
    # Incoming frozen article is the selected input; archived candidate articles
    # remain historical context and do not override that explicit selection.
    for article in group['articles']:
        try:current=sync.head(db,article['url'])
        except ValueError:sync.seed(db,article);continue
        if current['version_id']!=sync.version_id(article):raise ValueError('source changed: select/restart explicitly')
    for event in events(db):
        for article in event.get('articles',[]):
            if not db.execute('SELECT 1 FROM sync_heads WHERE url=?',(article['url'],)).fetchone():sync.seed(db,article)


def response_key(prompt):return digest({'prompt':prompt,'model':'gpt-5.4-mini','output_limit':1200})


def saved_transport(db,mock,now,*,charge=False,run_id='shadow-e2e',failpoint=None):
    """Durable saved/mock transport. Ambiguous response gaps stop for reconciliation.

    Budget reservations are conservative simulations only, never actual charges.
    A non-budgeted byte comparison is explicitly separate from cap tests.
    """
    def call(prompt,batch):
        key=response_key(prompt)
        row=db.execute('SELECT answer FROM sync_answer_cache WHERE cache_key=?',(key,)).fetchone()
        if row:
            provenance=db.execute('SELECT prompt,batch FROM e2e_answers WHERE cache_key=?',(key,)).fetchone()
            if provenance!=(prompt,dumps(batch)):raise RuntimeError('cached answer lacks matching batch provenance')
            return json.loads(row[0])
        reserved=db.execute('SELECT reservation,transport_status FROM sync_reservations WHERE cache_key=?',(key,)).fetchone()
        if reserved:
            reservation,status=json.loads(reserved[0]),reserved[1]
            if status!='reserved':raise RuntimeError('uncertain_response_requires_reconciliation')
            if reservation.get('periods')!=list(periods(now)):raise RuntimeError('reservation_period_requires_reconciliation')
        else:
            with atomic(db):
                result=reserve_mock(db,len(prompt.encode()),now,run_id=run_id) if charge else {'status':'offline_bytes_only_no_budget_charge'}
                if result['status']=='budget_deferred':raise BudgetExceeded('simulation cap reached')
                db.execute('INSERT INTO sync_reservations VALUES(?,?,?)',(key,dumps(result|{'periods':list(periods(now)),'mode':'capped_simulation' if charge else 'byte_comparison'}),'reserved'))
        sync.fault('after_reserve',failpoint)
        with atomic(db):
            changed=db.execute("UPDATE sync_reservations SET transport_status='calling' WHERE cache_key=? AND transport_status='reserved'",(key,)).rowcount
            if changed!=1:raise RuntimeError('request already claimed')
        sync.fault('before_mock',failpoint)
        try:
            answer=mock(prompt,batch)
            try:encoded=dumps(answer)
            except (ValueError,TypeError):answer={'parse_error':'non_json_mock_answer'};encoded=dumps(answer)
        except (ValueError,TypeError) as exc:answer={'parse_error':type(exc).__name__};encoded=dumps(answer)
        except BaseException:
            with atomic(db):db.execute("UPDATE sync_reservations SET transport_status='uncertain' WHERE cache_key=?",(key,))
            raise
        sync.fault('after_response_before_cache',failpoint)
        with atomic(db):
            db.execute('INSERT INTO sync_answer_cache VALUES(?,?,?)',(key,encoded,digest(answer)))
            db.execute('INSERT INTO e2e_answers VALUES(?,?,?,?)',(key,prompt,dumps(batch),'saved_or_mock_offline_only'))
            db.execute("UPDATE sync_reservations SET transport_status='cached' WHERE cache_key=?",(key,))
        sync.fault('after_cache',failpoint)
        return answer
    return call


def adjudicator(failpoint=None):
    def apply(db,sid,cursor,group,answer,batch,prompt,now,triage,registry):
        conditions=json.loads(db.execute('SELECT context FROM sessions WHERE session_id=?',(sid,)).fetchone()[0])
        if staged.context(db,group,triage,registry,conditions['batch_size'])!=conditions:raise RuntimeError('candidate conditions changed during response')
        seed_inputs(db,group)
        key=response_key(prompt)
        cached=db.execute('SELECT answer FROM sync_answer_cache WHERE cache_key=?',(key,)).fetchone()
        if not cached or json.loads(cached[0])!=answer:raise ValueError('response is not bound to actual prompt')
        provenance=db.execute('SELECT prompt,batch FROM e2e_answers WHERE cache_key=?',(key,)).fetchone()
        if provenance!=(prompt,dumps(batch)):raise ValueError('response candidate scope mismatch')
        op='e2e:'+sid+':'+str(cursor)
        valid=staged.valid_answer(answer,batch)
        if valid and answer.get('relation')=='same_event':
            target=answer['event_id']
            jid=sync.make_job(db,target,group,[],now,triage,registry,presented_ids=batch,response_prompt=prompt)
            result=sync.adopt(db,op,jid,now,failpoint=failpoint)
            outcome=result['status'];decisions=[result['decision_id']] if 'decision_id' in result else []
        else:
            # Still run the unchanged legacy guard on well-formed uncertainty;
            # uncertainty never establishes that any candidate is different.
            outcome=judge(db,group,answer,now,triage=triage,registry=registry,presented_ids=batch,rerank=False) if valid else 'invalid_answer_schema_or_target'
            decisions=[]
            for target in batch:
                ids=[]
                for a in group['articles']+[a for e in events(db) if e['event_id'] in batch for a in e.get('articles',[])]:
                    ids.append(sync.add_evidence(db,target,a['url'],'metadata'))
                result=sync.record_reference(db,op+':'+target,target,'ai','unresolved',
                    {'answer':answer,'guard_outcome':outcome,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),'presented_ids':batch,'scope':'offline_evaluation_only'},ids,now)
                decisions.append(result['decision_id'])
        for did in decisions:db.execute('INSERT OR IGNORE INTO e2e_decisions VALUES(?,?,?,?)',(sid,cursor,did,outcome))
        sync.fault('before_cursor',failpoint)
        return outcome
    return apply


def begin(db,group,triage=None,registry=None):
    initialize(db);seed_inputs(db,group)
    # The entire remainder, even when >64, must be examined in the next group.
    return staged.begin(db,group,triage,registry,batch_size=(4,'rest'))


def step(db,sid,mock,now,triage=None,registry=None,*,charge=False,budget_available=True,failpoint=None,transport_failpoint=None):
    # Raw responses and reservations are persisted before the composed adoption /
    # cursor savepoint. Adoption rollback never discards a captured response.
    return staged.step(db,sid,saved_transport(db,mock,now,charge=charge,failpoint=transport_failpoint),now,
        triage,registry,budget_available=budget_available,adjudicate=adjudicator(failpoint))


DISPLAY_FIELDS=('event_id','event_name','category','tracking_value','lifecycle','updated_at','current_stage','summary','latest','next_watch','articles','progress_count','progress_history','last_progress_at','schedules','corrections','source_revisions')

def legacy_public(db,registry):
    candidate=copy.deepcopy(events(db));protect_existing(candidate,registry)
    return public_events(candidate)


def export_context(db):
    # Include all public-affecting and validity state; never stale-serve a prepared
    # generation after a correction, cancellation or operator recheck.
    tables=('events','sync_decision_state','sync_event_holds','sync_heads','sync_component_epochs','sync_adoptions','sync_decisions')
    return digest({t:sorted(db.execute('SELECT * FROM '+t).fetchall(),key=repr) for t in tables})


def prepare_export(db,registry):
    compatibility=legacy_public(db,registry)
    # No imported fact, operator evaluation reference, or mock/saved-response
    # rehearsal has an independent publication approval. Fail closed, explicitly.
    public=[]
    holds=[{'event_id':e['event_id'],'reason':'publication_evidence_not_independently_verified','classification':'unconfirmed'} for e in compatibility]
    payload={'public_events':public,'compatibility_preview':compatibility,'publication_holds':holds,
             'private_holds':[{'stage':r[0],'group_key':r[1],'payload':json.loads(r[2])} for r in db.execute('SELECT stage,group_key,payload FROM holds ORDER BY stage,group_key')],
             'can_publish':False,'preview_only':True,'schema':'same top-level event array; no public wrapper or new mandatory field'}
    context=digest({'state':export_context(db),'registry':registry})
    payload['registry']=copy.deepcopy(registry)
    generation=digest({'payload':payload,'context':context})
    with atomic(db):db.execute('INSERT OR IGNORE INTO e2e_exports VALUES(?,?,?)',(generation,dumps(payload),context))
    return generation


def materialize(db,generation,output_root,*,snapshot_root,failpoint=None):
    """Generation directories + manifest: DB/file commits are never claimed atomic.

    Only an explicit temporary rehearsal root is writable. No live deployment,
    symlink, snapshot or repository path is accepted. A crash leaves a resumable
    generation with no ready manifest. Existing exports are hash-checked.
    """
    output=Path(output_root)
    repo=Path(__file__).resolve().parents[2];snapshot=Path(snapshot_root).resolve()
    if not output.is_absolute():raise ValueError('absolute temporary output root required')
    if any(p.is_symlink() for p in [output,*output.parents]):raise ValueError('symlink output forbidden')
    output=output.resolve()
    import tempfile
    if not output.is_relative_to(Path(tempfile.gettempdir()).resolve()):raise ValueError('export is restricted to temporary directories')
    if output.is_relative_to(snapshot) or output.is_relative_to(repo) or snapshot.is_relative_to(output) or repo.is_relative_to(output):raise ValueError('output overlaps source snapshot')
    row=db.execute('SELECT payload,context_hash FROM e2e_exports WHERE generation=?',(generation,)).fetchone()
    if row is None:raise ValueError('unknown prepared generation')
    payload=json.loads(row[0])
    if row[1]!=digest({'state':export_context(db),'registry':payload['registry']}):raise ValueError('stale export; prepare a new generation')
    target=output/generation
    if target.is_symlink():raise ValueError('symlink generation forbidden')
    target.mkdir(parents=True,exist_ok=True)
    files={'public_events.json':payload['public_events'],'compatibility_preview.json':payload['compatibility_preview'],
           'publication_holds.json':payload['publication_holds'],'private_holds.json':payload['private_holds']}
    def write(name,value):
        path=target/name
        if path.is_symlink():raise ValueError('symlink file forbidden')
        data=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
        if path.exists():
            if path.read_bytes()!=data:raise ValueError('existing generation differs; do not overwrite')
        else:
            # Exclusive temporary file avoids clobbering files from other writers.
            import tempfile
            fd,temporary=tempfile.mkstemp(prefix='.partial-',dir=target)
            try:
                with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,path)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
        return hashlib.sha256(data).hexdigest()
    hashes={}
    for index,(name,value) in enumerate(files.items()):
        hashes[name]=write(name,value)
        if index==0:sync.fault('after_first_file',failpoint)
    sync.fault('before_manifest',failpoint)
    write('manifest.json',{'generation':generation,'files':hashes,'preview_only':True,'can_publish':False})
    return {'directory':str(target),'hashes':hashes,'can_publish':False}


def compare_public(old,new):
    left={e['event_id']:e for e in old};right={e['event_id']:e for e in new}
    differences=[]
    for eid in sorted(left.keys()|right.keys()):
        if eid not in left or eid not in right:
            differences.append({'event_id':eid,'field':'presence','classification':'unconfirmed','reason':'publication proof required or existing publication gate','old':eid in left,'new':eid in right});continue
        for key in DISPLAY_FIELDS:
            if left[eid].get(key)!=right[eid].get(key):
                differences.append({'event_id':eid,'field':key,'classification':'specification_difference' if key in ('articles','source_revisions') else 'unconfirmed','reason':'offline guarded replay, not independently verified improvement','old':left[eid].get(key),'new':right[eid].get(key)})
    return {'old_events':len(old),'new_events':len(new),'event_ids_equal':left.keys()==right.keys(),
            'article_urls_equal':{(e['event_id'],a['url']) for e in old for a in e.get('articles',[])}=={(e['event_id'],a['url']) for e in new for a in e.get('articles',[])},
            'progress_total_old':sum(e.get('progress_count',0) for e in old),'progress_total_new':sum(e.get('progress_count',0) for e in new),
            'differences':differences,'display_fields_compared':list(DISPLAY_FIELDS)}
