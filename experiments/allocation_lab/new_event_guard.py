"""Fail-closed, offline new-event proposal gate. Never writes production events.

Human records in this lab are test inputs, not authenticated production approval.
A lab proposal is neither a real event ID nor publication authorization.
"""
import copy,hashlib,json,sqlite3
from datetime import datetime,timezone
from dataclasses import dataclass,asdict
from pathlib import Path
from failed_response import validation_revision as native_revision
from tracking_v1 import apply_report,evidence
from merge_safety import validate_saved_update,UpdateHeld
from .allocation import dt
from .session import digest,encode


def sha_bytes(value):return hashlib.sha256(value).hexdigest()
def revision():return digest({'native':native_revision('tracking'),'guard':sha_bytes(Path(__file__).read_bytes())})
def versions(group):return {a['url']:digest(a) for a in group.get('articles',[])}

class Snapshot:
    """Read every event in a pinned file, including held/merged/dormant events."""
    def __init__(self,path,expected_sha256):
        self.path=Path(path);self.file_hash=expected_sha256
        raw=self.path.read_bytes()
        if sha_bytes(raw)!=expected_sha256:raise ValueError('snapshot checksum mismatch')
        self.events=json.loads(raw)
        if not isinstance(self.events,list) or any(not isinstance(e,dict) or not e.get('event_id') for e in self.events):raise ValueError('complete event snapshot required')
        ids=[e['event_id'] for e in self.events]
        if len(set(ids))!=len(ids):raise ValueError('duplicate event IDs in snapshot')
        self.signature=digest(self.events)
    def check(self):
        try:current=sha_bytes(self.path.read_bytes())
        except OSError as error:raise ValueError('snapshot unavailable; requeue and reconfirm') from error
        if current!=self.file_hash or digest(self.events)!=self.signature:raise ValueError('snapshot changed; requeue and reconfirm')


def binding(group,snapshot):
    snapshot.check()
    if not group.get('articles') or len(versions(group))!=len(group['articles']):raise ValueError('nonempty unique article URLs required')
    return {'group_hash':digest(group),'article_versions':versions(group),'snapshot':snapshot.signature,'snapshot_file_hash':snapshot.file_hash,'validator':revision()}


def literal_pin(article,claim):
    """Exact text match is not source verification. The human claim is separate."""
    return (claim.get('article_url')==article.get('url') and claim.get('article_version')==digest(article)
            and claim.get('source_kind') in ('title','description','body')
            and isinstance(claim.get('quote'),str) and bool(claim['quote'])
            and isinstance(article.get(claim['source_kind'],''),str) and claim['quote'] in article.get(claim['source_kind'],'')
            and claim.get('source_confirmed_by_operator') is True)


def human_record(value):
    try:return bool(value.get('reviewer')) and bool(value.get('reason')) and dt(value['checked_at'])<=datetime.now(timezone.utc)
    except (KeyError,ValueError,TypeError):return False


def identity_pin(group,identity):
    """No inferred case identifiers: formal namespace and value are explicit."""
    return (isinstance(identity,dict) and isinstance(identity.get('namespace'),str) and bool(identity['namespace']) and isinstance(identity.get('value'),str) and bool(identity['value'])
            and any(literal_pin(a,identity) and identity['value'] in identity['quote'] for a in group.get('articles',[])))

@dataclass(frozen=True)
class MockOperatorConfirmation:
    """Fixture-only capability: cannot arrive as an AI JSON object.

    No production authentication or real operator verification is implemented.
    """
    context: str
    purpose: str
    event_id: str
    record_hash: str


def confirmation_matches(value,context,purpose,event_id,record):
    return isinstance(value,MockOperatorConfirmation) and value==MockOperatorConfirmation(context,purpose,event_id,digest(record))


def confirmation_receipt(value):
    return asdict(value) if isinstance(value,MockOperatorConfirmation) else None

class NewEventGuard:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS candidates(context TEXT,event_id TEXT,event_hash TEXT,payload TEXT,state TEXT NOT NULL,PRIMARY KEY(context,event_id));
          CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,context TEXT,event_id TEXT,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS proposals(identity_key TEXT PRIMARY KEY,lab_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS proposal_links(context TEXT PRIMARY KEY,lab_id TEXT NOT NULL,payload TEXT NOT NULL);
        ''');self.db.commit()
    def close(self):self.db.close()
    def _operation(self,op,request,fn,fail=False):
        if not op:raise ValueError('operation ID required')
        request_hash=digest(request);self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT request_hash,result FROM operations WHERE id=?',(op,)).fetchone()
            if old:
                if old[0]!=request_hash:raise ValueError('operation ID conflict')
                self.db.rollback();return json.loads(old[1])
            result=fn();self.db.execute('INSERT INTO operations VALUES(?,?,?)',(op,request_hash,encode(result)))
            if fail:raise OSError('simulated transaction failure')
            self.db.commit();return result
        except BaseException:self.db.rollback();raise
    def queue(self,op,group,snapshot,*,retrieved_ids=(),fail=False):
        bound=binding(group,snapshot);key=digest(bound)
        if not set(retrieved_ids).issubset({e['event_id'] for e in snapshot.events}):raise ValueError('retrieval contains unknown event ID')
        def save():
            self.db.execute('INSERT OR IGNORE INTO contexts VALUES(?,?)',(key,encode({'binding':bound,'group':group,'snapshot_events':snapshot.events})))
            for event in snapshot.events:
                self.db.execute('INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?)',(key,event['event_id'],digest(event),encode(event),'unexamined'))
            return {'context_id':key,'candidate_count':len(snapshot.events),'retrieval_count':len(set(retrieved_ids)),'state':'held','production_write':False}
        return self._operation(op,{'action':'queue','binding':bound,'retrieved_ids':sorted(set(retrieved_ids))},save,fail)
    def _current(self,context,group,snapshot):
        row=self.db.execute('SELECT payload FROM contexts WHERE id=?',(context,)).fetchone()
        if row is None:raise ValueError('unknown context')
        payload=json.loads(row[0])
        if payload['binding']!=binding(group,snapshot):raise ValueError('article, snapshot or validation changed')
        return payload
    def review(self,op,context,event_id,record,group,snapshot,*,confirmation=None,fail=False):
        self._current(context,group,snapshot)
        row=self.db.execute('SELECT payload FROM candidates WHERE context=? AND event_id=?',(context,event_id)).fetchone()
        if row is None:raise ValueError('review target outside complete catalogue')
        event=json.loads(row[0]);relation=record.get('relation');state='needs_review'
        incoming=record.get('incoming_identity',{});existing=event.get('case_identity',{})
        formal=(confirmation_matches(confirmation,context,'review',event_id,record) and record.get('authority')=='operator' and human_record(record)
                and record.get('binding_hash')==context and record.get('event_hash')==digest(event)
                and identity_pin(group,incoming) and identity_pin(event,existing)
                and incoming.get('namespace')==existing.get('namespace'))
        if formal:
            same=incoming['value']==existing['value']
            if relation=='same' and same:state='same_verified'
            if relation=='distinct' and not same and record.get('separate_incident_confirmed_by_operator') is True and bool(record.get('incident_distinction_reason')):state='distinct_reviewed'
        def save():
            prior=self.db.execute('SELECT state FROM candidates WHERE context=? AND event_id=?',(context,event_id)).fetchone()[0]
            final='conflict' if prior=='conflict' or (prior in ('same_verified','distinct_reviewed') and state!=prior) else state
            self.db.execute('INSERT INTO reviews VALUES(?,?,?,?)',(op,context,event_id,encode({'record':record,'validated_state':state})))
            self.db.execute('UPDATE candidates SET state=? WHERE context=? AND event_id=?',(final,context,event_id))
            return {'state':final,'event_id':event_id,'production_write':False}
        return self._operation(op,{'action':'review','context':context,'event_id':event_id,'record':record,'confirmation':confirmation_receipt(confirmation)},save,fail)
    def status(self,context):
        return {row[0]:row[1] for row in self.db.execute('SELECT event_id,state FROM candidates WHERE context=? ORDER BY event_id',(context,))}
    def decide(self,op,context,group,snapshot,ai_report,authorization=None,*,confirmation=None,fail=False):
        # Freshness is checked before replaying a historically successful operation.
        try:self._current(context,group,snapshot)
        except ValueError as error:return {'state':'held','reason':'stale_context','detail':str(error),'production_write':False}
        authorization=authorization or {}
        def save():
            def held(reason):return {'state':'held','reason':reason,'production_write':False,'event_merges':0,'new_production_ids':0}
            if ai_report.get('relation')!='new_event':return held('no_automatic_attachment_or_merge')
            rows=self.db.execute('SELECT state FROM candidates WHERE context=?',(context,)).fetchall()
            if any(r[0]!='distinct_reviewed' for r in rows):return held('unexamined_same_conflicting_or_unproved_candidates')
            identity=authorization.get('identity',{})
            if not (confirmation_matches(confirmation,context,'new_proposal','',authorization) and authorization.get('authority')=='operator' and human_record(authorization) and authorization.get('context_id')==context and authorization.get('authorize_lab_proposal') is True and identity_pin(group,identity)):
                return held('explicit_independent_review_required')
            # Formal comparisons must all concern the very same incoming identity.
            for raw, in self.db.execute('SELECT payload FROM reviews WHERE context=?',(context,)):
                saved=json.loads(raw)
                if saved['validated_state']!='distinct_reviewed':continue
                record=saved['record']
                if record.get('incoming_identity',{}).get('namespace')!=identity['namespace'] or record.get('incoming_identity',{}).get('value')!=identity['value']:return held('identity_or_review_conflict')
            if ai_report.get('confidence')!='high' or not evidence(ai_report,group['articles']):return held('native_evidence_failed')
            candidate={k:copy.deepcopy(v) for k,v in group.items() if k!='articles'}
            identity_key=digest({'namespace':identity['namespace'],'value':identity['value']});lab_id='lab-only-'+identity_key
            candidate.update(event_id=lab_id,articles=[],created_at=authorization['checked_at'])
            try:
                apply_report(candidate,copy.deepcopy(group['articles']),copy.deepcopy(ai_report),dt(authorization['checked_at']),initial=True)
                validate_saved_update(None,candidate)
            except (UpdateHeld,ValueError,TypeError,KeyError):return held('native_validation_failed')
            linked=self.db.execute('SELECT lab_id FROM proposal_links WHERE context=?',(context,)).fetchone()
            if linked and linked[0]!=lab_id:return held('context_already_bound_to_another_identity')
            if not linked and self.db.execute('SELECT 1 FROM proposals LIMIT 1').fetchone():return held('other_lab_proposals_require_reconciliation')
            previous=self.db.execute('SELECT lab_id FROM proposals WHERE identity_key=?',(identity_key,)).fetchone()
            if previous is None:self.db.execute('INSERT INTO proposals VALUES(?,?,?)',(identity_key,lab_id,encode(candidate)))
            else:lab_id=previous[0]
            self.db.execute('INSERT OR IGNORE INTO proposal_links VALUES(?,?,?)',(context,lab_id,encode({'binding':context,'authorization':authorization,'report':ai_report})))
            return {'state':'lab_proposal_only','lab_id':lab_id,'reused_proposal':previous is not None,'production_write':False,'event_merges':0,'new_production_ids':0,'publication_allowed':False}
        return self._operation(op,{'action':'decide','context':context,'report':ai_report,'authorization':authorization,'confirmation':confirmation_receipt(confirmation)},save,fail)
