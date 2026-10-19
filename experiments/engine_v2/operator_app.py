"""Loopback-only, isolated operator rehearsal. No production writer or AI client."""
import argparse
import copy
import hmac
import json
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from engine import atomic, connect, dumps, events, import_snapshot, rank
from failed_response import digest
import revision_sync as sync
import publication as pub
import operator_tasks as tasks

SCOPE = 'isolated_operator_rehearsal'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ui_meta(scope TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_receipts(op TEXT PRIMARY KEY, command_hash TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_queue_reviews(op TEXT PRIMARY KEY, queue_id TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_reviews(review_id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events,
 group_payload TEXT NOT NULL, result TEXT NOT NULL, reason TEXT NOT NULL, decision_id TEXT NOT NULL REFERENCES sync_decisions,
 actor TEXT NOT NULL, at TEXT NOT NULL);
'''


def initialize(db):
    pub.initialize(db)
    tasks.initialize(db)
    with atomic(db):
        for stmt in SCHEMA.split(';'):
            if stmt.strip(): db.execute(stmt)
        if not db.execute('SELECT 1 FROM ui_meta').fetchone(): db.execute('INSERT INTO ui_meta VALUES(?)', (SCOPE,))
        if db.execute('SELECT scope FROM ui_meta').fetchall() != [(SCOPE,)]: raise ValueError('wrong UI scope')


def now(): return datetime.now(timezone.utc)


def catalog(event):
    out = [('event', 'identity')]
    out += [('field', key) for key in sorted(pub.FIELDS) if key in event]
    for kind, name in [('article', 'articles'), *pub.LISTS.items()]:
        out += [(kind, pub.key_for(kind, item)) for item in event.get(name, [])]
    return out


def proofs(db, eid, kind, key):
    dids = [r[0] for r in db.execute('''SELECT d.decision_id FROM sync_decisions d
      JOIN sync_decision_state s USING(decision_id) JOIN sync_adoptions a USING(decision_id)
      WHERE d.event_id=? AND s.status='current' AND a.cancelled=0 ORDER BY d.rowid DESC''', (eid,))]
    if not dids: return [], []
    # Never turn operator reference records into guarded adoptions.
    dids = dids[:1]
    evid = [r[0] for r in db.execute('SELECT evidence_id FROM sync_dependencies WHERE decision_id=?', (dids[0],))]
    evid = [i for i in evid if sync.verify_quote(db,i,current=True)['status']=='exact_literal_match']
    if kind == 'article':
        evid = [i for i in evid if db.execute('SELECT url FROM sync_evidence WHERE evidence_id=?', (i,)).fetchone()[0] == key]
    return dids, evid


def target_status(db, event, kind, key):
    value = pub.target(event, kind, key)
    row = db.execute('''SELECT h.approval_id,s.status FROM pub_heads h JOIN pub_state s USING(approval_id)
      WHERE event_id=? AND kind=? AND target_key=?''', (event['event_id'], kind, key)).fetchone()
    dids, evid = proofs(db, event['event_id'], kind, key)
    result = dict(kind=kind, key=key, content=value, content_hash=digest(value),
                  approval_id=row[0] if row else None, status=row[1] if row else 'unapproved',
                  decision_ids=dids, evidence_ids=evid)
    # Ask the existing approval validator inside an ALWAYS rolled-back savepoint.
    # This is only a readiness check, never an approval record.
    db.execute('SAVEPOINT ui_readiness')
    try:
        pub.approve(db, 'ui-readiness', event['event_id'], kind, key,
                    expected_hash=result['content_hash'], actor='simulation:readiness', at=now(),
                    evidence_ids=evid, decision_ids=dids, basis='isolated readiness only',
                    supersedes=row[0] if row else None)
        result.update(ready=True, warning=None)
    except ValueError as exc:
        result.update(ready=False, warning=str(exc))
    finally:
        db.execute('ROLLBACK TO ui_readiness'); db.execute('RELEASE ui_readiness')
    return result


def snapshot(db):
    pub.refresh(db)
    items = []
    for event in events(db):
        eid = event['event_id']
        articles = []
        for a in event.get('articles', []):
            source = sync.head(db, a['url'])
            evidence = []
            for i, field, quote in db.execute('SELECT evidence_id,field,quote FROM sync_evidence WHERE event_id=? AND url=?', (eid, a['url'])):
                evidence.append(dict(evidence_id=i, field=field, quote=quote,
                                     verification=sync.verify_quote(db, i, current=True)))
            articles.append(dict(article=a, version=source, evidence=evidence))
        judgments = [dict(zip(('decision_id','kind','outcome','at','status'), row)) for row in db.execute('''
          SELECT d.decision_id,d.kind,d.outcome,d.recorded_at,s.status FROM sync_decisions d
          JOIN sync_decision_state s USING(decision_id) WHERE event_id=? ORDER BY d.rowid DESC''', (eid,))]
        targets = [target_status(db, event, kind, key) for kind, key in catalog(event)]
        reviews = [dict(zip(('review_id','result','reason','decision_id','at'), row)) for row in db.execute(
            'SELECT review_id,result,reason,decision_id,at FROM ui_reviews WHERE event_id=? ORDER BY rowid DESC', (eid,))]
        items.append(dict(event=event, articles=articles, judgments=judgments, targets=targets, reviews=reviews))
    queues=[]
    for stage, key, raw in db.execute('SELECT stage,group_key,payload FROM holds ORDER BY stage,group_key'):
        payload=json.loads(raw); group=payload.get('group',payload)
        if not isinstance(group,dict) or not group.get('articles'): continue
        queue_id=digest([stage,key])
        triage_row=db.execute("SELECT payload FROM source_files WHERE path='tracking_triage.json'").fetchone()
        triage=json.loads(triage_row[0]).get('items',{}) if triage_row else {}
        choices=[{k:v for k,v in row.items() if k!='event'} for row in rank(db,group,triage=triage,persist=False)]
        records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM ui_queue_reviews WHERE queue_id=? ORDER BY rowid DESC',(queue_id,))]
        queues.append(dict(queue_id=queue_id,stage=stage,source_status=payload.get('status','未確認'),source_reason=payload.get('reason'),group=group,candidates=choices,records=records))
    projection = pub.project(db, {})
    history = [dict(zip(('approval_id','operation_id','old_status','new_status','actor','at','reason'), row))
               for row in db.execute('SELECT approval_id,operation_id,old_status,new_status,actor,at,reason FROM pub_history ORDER BY rowid DESC')]
    result = dict(scope=SCOPE, can_publish=False, items=items, queues=queues, history=history,
                  legacy_operator_records=[json.loads(r[0]) for r in db.execute('SELECT payload FROM operator_decisions ORDER BY rowid')],
                  approved_preview=projection['events'], notices=projection['notices'])
    result['data_scope']='synthetic_demo' if db.execute("SELECT 1 FROM sqlite_master WHERE name='ui_demo_meta'").fetchone() else 'saved_snapshot_not_revalidated'
    result['work']=tasks.worklist(db,result)
    result['revision'] = digest(result)
    return result


def operate(db, command, failpoint=None):
    if not isinstance(command, dict): raise ValueError('object required')
    op = command.get('op')
    if not isinstance(op, str) or not 8 <= len(op) <= 120: raise ValueError('operation ID required')
    original = digest(command)
    with atomic(db):
        cached = db.execute('SELECT command_hash,result FROM ui_receipts WHERE op=?', (op,)).fetchone()
        if cached:
            if cached[0] != original: raise ValueError('operation ID reused for another command')
            return json.loads(cached[1])
        state = snapshot(db)
        if command.get('revision') != state['revision']: raise ValueError('画面が古くなりました。再読込して再確認してください。')
        action = command.get('action')
        eid = command.get('event_id')
        item = next((v for v in state['items'] if v['event']['event_id'] == eid), None)
        if item is None and not (action in ('queue_review','task_review','reopen_task','quote_record') and eid is None): raise ValueError('unknown event')
        at = now(); actor = 'simulation:local-operator'
        if action=='quote_record':
            import quote_bindings
            task=next((t for t in state['work']['tasks'] if t['task_id']==command.get('task_id') and t.get('article')),None)
            if not task or command.get('context_hash')!=task['review_contexts'].get(eid or 'none'):
                raise ValueError('記事・引用の確認画面が変更されています。')
            if eid is not None and eid not in [c['event_id'] for c in task['candidates']]:raise ValueError('unknown quote candidate')
            if task.get('required_event_id') and eid!=task['required_event_id']:raise ValueError('shared article event scope mismatch')
            if task.get('edition_conflict'):raise ValueError('article version selection is unresolved')
            if command.get('confirmed') is not True:raise ValueError('引用の保存先を確認してください。')
            url=task['article']['url']
            source=tasks.head(db,url)
            if source is None:source=sync.seed(db,task['article'])
            if command.get('kind')=='body' and source['components']['body'] is None:raise ValueError('本文未取得です。')
            qid=quote_bindings.record(db,'ui-quote:'+op,event_id=eid,url=url,quote=command.get('quote'),kind=command.get('kind'),origin='operator_input',now=at,state_id=source['state_id'])
            result=dict(status='quote_reference_saved',quote_id=qid,adopted=False,published=False)
        elif action=='quote_confirm':
            import quote_bindings
            quote=quote_bindings.inspect(db,command.get('quote_id'))
            if quote['event_id']!=eid or command.get('confirmed') is not True:raise ValueError('引用・対象事件の確認が必要です。')
            quote_bindings.confirm(db,'ui-quote-check:'+op,quote['quote_id'],kind=command.get('kind'),actor=actor,checked_at=command.get('checked_at'),decision_id=command.get('decision_id'))
            result=dict(status='quote_confirmation_saved',adopted=False,published=False)
        elif action=='reopen_task':
            result=tasks.reopen(db,state,command,at)
        elif action=='task_review':
            result=tasks.review(db,state,command,at)
        elif action=='approve_bundle':
            result=tasks.bundle(db,state,command,at,failpoint=failpoint)
        elif action=='queue_review':
            queue=next((q for q in state['queues'] if q['queue_id']==command.get('queue_id')),None)
            if queue is None: raise ValueError('unknown queue')
            outcome=command.get('result'); reason=command.get('reason','')
            if outcome not in ('same_event','different_event','hold') or not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise ValueError('判断と理由が必要です')
            if eid is not None and eid not in [v['event_id'] for v in queue['candidates']]: raise ValueError('unknown candidate')
            ids=[]
            if eid is not None:
                urls={a['url'] for a in queue['group']['articles']}
                ids=[r[0] for r in db.execute('SELECT evidence_id,url FROM sync_evidence WHERE event_id=?',(eid,)) if r[1] in urls and sync.verify_quote(db,r[0],current=True)['status']=='exact_literal_match']
            if outcome!='hold' and not ids: raise ValueError('原文一致の根拠が不足しています。保留にしてください。')
            ref=None
            if eid is not None:
                previous=db.execute('SELECT payload FROM ui_queue_reviews WHERE queue_id=? ORDER BY rowid DESC LIMIT 1',(queue['queue_id'],)).fetchone()
                old=json.loads(previous[0]) if previous else {}
                supersedes=old.get('decision_id') if old.get('event_id')==eid else None
                ref=sync.record_reference(db,'ui:'+op,eid,'operator',outcome,{'scope':'operator_review_only','group':queue['group'],'reason':reason},ids,at,supersedes=supersedes)
            result=dict(status='recorded_reference_only',decision_id=ref['decision_id'] if ref else None,event_id=eid,result=outcome,reason=reason,group=queue['group'],at=at.isoformat())
            db.execute('INSERT INTO ui_queue_reviews VALUES(?,?,?)',(op,queue['queue_id'],dumps(result)))
        elif action == 'review':
            result = command.get('result'); reason = command.get('reason', '')
            if not isinstance(reason,str): raise ValueError('reason must be text')
            reason=reason.strip()
            if result not in ('same_event', 'different_event', 'hold'): raise ValueError('unknown review')
            if not isinstance(reason, str) or len(reason) > 1000 or (result == 'hold' and not reason): raise ValueError('保留理由を入力してください')
            group = command.get('group')
            if not isinstance(group, dict) or not group.get('articles'): raise ValueError('article selection required')
            # Only the exact articles currently displayed for this incident can be reviewed.
            if group != {'articles': item['event']['articles']}: raise ValueError('article scope mismatch')
            ids = [r[0] for r in db.execute('SELECT evidence_id FROM sync_evidence WHERE event_id=?', (eid,))]
            if result != 'hold' and not any(sync.verify_quote(db, i, current=True).get('status') == 'exact_literal_match' for i in ids):
                raise ValueError('原文一致の根拠が不足しています。保留にしてください。')
            previous=db.execute('SELECT decision_id FROM ui_reviews WHERE event_id=? ORDER BY rowid DESC LIMIT 1',(eid,)).fetchone()
            ref = sync.record_reference(db, 'ui:'+op, eid, 'operator', result,
                       {'scope':'operator_review_only','group':group,'reason':reason,'source_revision':state['revision']}, ids, at, supersedes=previous[0] if previous else None)
            db.execute('INSERT INTO ui_reviews VALUES(?,?,?,?,?,?,?,?)',
                       (op,eid,dumps(group),result,reason,ref['decision_id'],actor,at.isoformat()))
            result = dict(ref)
        elif action in ('approve','withdraw'):
            if command.get('confirmed') is not True: raise ValueError('explicit confirmation required')
            target = next((t for t in item['targets'] if t['kind']==command.get('kind') and t['key']==command.get('key')),None)
            if target is None: raise ValueError('unknown approval target')
            reason = command.get('reason','')
            if not isinstance(reason,str): raise ValueError('reason must be text')
            reason=reason.strip()
            if not reason or len(reason)>1000: raise ValueError('承認・取消し理由を入力してください')
            if action == 'approve':
                result = pub.approve(db, 'ui:'+op, eid, target['kind'], target['key'],
                    expected_hash=target['content_hash'],actor=actor,at=at,
                    evidence_ids=target['evidence_ids'],decision_ids=target['decision_ids'],basis=reason,
                    supersedes=target['approval_id'])
            else:
                if not target['approval_id']: raise ValueError('no approval to withdraw')
                result = pub.withdraw(db,'ui:'+op,target['approval_id'],actor=actor,at=at,reason=reason)
        else: raise ValueError('unknown action')
        if failpoint == 'before_receipt': raise RuntimeError('synthetic save failure')
        db.execute('INSERT INTO ui_receipts VALUES(?,?,?)', (op,original,dumps(result)))
    if failpoint == 'after_commit': raise RuntimeError('synthetic lost acknowledgment')
    return result


def preview_target(db, command):
    state=snapshot(db)
    if command.get('revision')!=state['revision']: raise ValueError('古い画面です。再読込してください。')
    item=next((i for i in state['items'] if i['event']['event_id']==command.get('event_id')),None)
    if item is None: raise ValueError('unknown event')
    target=next((t for t in item['targets'] if t['kind']==command.get('kind') and t['key']==command.get('key')),None)
    if target is None: raise ValueError('unknown target')
    db.execute('SAVEPOINT ui_preview')
    try:
        pub.approve(db,'ui-preview',item['event']['event_id'],target['kind'],target['key'],
            expected_hash=target['content_hash'],actor='simulation:preview',at=now(),
            evidence_ids=target['evidence_ids'],decision_ids=target['decision_ids'],
            basis='hypothetical preview, always rolled back',supersedes=target['approval_id'])
        return dict(scope='hypothetical_not_approved',before=state['approved_preview'],after=pub.project(db,{})['events'])
    finally:
        db.execute('ROLLBACK TO ui_preview');db.execute('RELEASE ui_preview')


def preview_bundle(db,command):
    state=snapshot(db)
    if command.get('revision')!=state['revision']:raise ValueError('画面が古くなりました。再読込してください。')
    db.execute('SAVEPOINT ui_bundle_preview')
    try:return tasks.bundle(db,state,dict(command,op='preview-bundle'),now(),preview=True)
    finally:
        db.execute('ROLLBACK TO ui_bundle_preview');db.execute('RELEASE ui_bundle_preview')


def open_owned(path):
    p=Path(path)
    if p.is_symlink() or p.parent.is_symlink(): raise ValueError('symlink DB forbidden')
    p=p.resolve()
    if not p.is_relative_to(Path(tempfile.gettempdir()).resolve()) or p.is_relative_to(ROOT):
        raise ValueError('UI DB must be an isolated /tmp copy outside repository')
    if p.exists():
        import sqlite3
        probe=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
        try: marker=probe.execute('SELECT scope FROM ui_meta').fetchall()
        except sqlite3.Error: marker=[]
        finally: probe.close()
        if marker != [(SCOPE,)]: raise ValueError('refusing an unmarked existing DB')
    db=connect(p);initialize(db);return db


def copy_isolated(source, destination):
    """Read-only backup of an existing rehearsal DB, never open its writer."""
    import sqlite3
    src=Path(source);dst=Path(destination)
    if src.is_symlink() or not src.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError('copy source must be a temporary rehearsal DB')
    if dst.exists() or dst.is_symlink(): raise ValueError('copy destination must be new')
    # Validate destination constraints before opening any destination file.
    db=open_owned(dst);db.close()
    ro=sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if not ro.execute("SELECT 1 FROM sqlite_master WHERE name='sync_decisions'").fetchone():
            raise ValueError('source is not a judgment-sync rehearsal')
        out=sqlite3.connect(dst)
        try: ro.backup(out)
        finally: out.close()
    finally: ro.close()
    db=connect(dst);initialize(db);return db


class LocalServer(HTTPServer):
    def __init__(self, app, port=8765):
        self.app=app;self.token=secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1',port), Handler)
        self.origin='http://127.0.0.1:'+str(self.server_port)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def valid_host(self): return self.headers.get('Host') == '127.0.0.1:'+str(self.server.server_port)
    def respond(self,status,body,mime='application/json; charset=utf-8'):
        data=body.encode('utf-8');self.send_response(status)
        for k,v in {'Content-Type':mime,'Content-Length':str(len(data)),'Cache-Control':'no-store',
           'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
           'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}.items(): self.send_header(k,v)
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if not self.valid_host(): return self.respond(403,dumps({'error':'invalid host'}))
        if self.path=='/':
            text=Path(__file__).with_name('operator_ui.html.template').read_text()
            return self.respond(200,text.replace('__LOCAL_TOKEN__',self.server.token),'text/html; charset=utf-8')
        if self.path=='/api/state': return self.respond(200,dumps(snapshot(self.server.app)))
        self.respond(404,'{}')
    def do_POST(self):
        if (not self.valid_host() or self.headers.get('Origin') != self.server.origin or
            not hmac.compare_digest(self.headers.get('X-Local-Token',''),self.server.token)):
            return self.respond(403,dumps({'error':'local session required'}))
        if self.path not in ('/api/operate','/api/preview','/api/preview-bundle'): return self.respond(404,'{}')
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=65536: raise ValueError('request size rejected')
            if self.headers.get('Content-Type')!='application/json': raise ValueError('JSON required')
            command=json.loads(self.rfile.read(size))
            result=preview_bundle(self.server.app,command) if self.path=='/api/preview-bundle' else preview_target(self.server.app,command) if self.path=='/api/preview' else operate(self.server.app,command)
            self.respond(200,dumps({'result':result,'state':snapshot(self.server.app)}))
        except (ValueError,TypeError,KeyError) as exc: self.respond(409,dumps({'error':str(exc)}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db');parser.add_argument('--snapshot',type=Path);parser.add_argument('--copy-db',type=Path)
    parser.add_argument('--port',type=int,default=8765);parser.add_argument('--demo',action='store_true')
    args=parser.parse_args()
    if sum(bool(x) for x in (args.demo,args.snapshot,args.copy_db))>1: parser.error('choose only one input mode')
    path=args.db or str(Path(tempfile.mkdtemp(prefix='news-now-operator-'))/'operator.sqlite')
    db=copy_isolated(args.copy_db,path) if args.copy_db else open_owned(path)
    if args.demo:
        from operator_task_demo import populate
        if not events(db): populate(db)
    elif args.snapshot and not events(db):
        import_snapshot(db,args.snapshot.resolve())
        for event in events(db):
            for article in event.get('articles',[]):
                if not db.execute('SELECT 1 FROM sync_heads WHERE url=?',(article['url'],)).fetchone(): sync.seed(db,article)
    server=LocalServer(db,args.port)
    print('隔離試作・本番反映不可 '+server.origin,flush=True)
    print('再起動用SQLite: '+str(Path(path).resolve()),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close();db.close()

if __name__=='__main__': main()
