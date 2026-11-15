"""Offline operator workflow. Mock authentication; laboratory output only.

No AI, production event writer, credentials, network, or guard capability minting.
"""
import copy,json,sqlite3,hashlib
from pathlib import Path
from dataclasses import dataclass
from datetime import timedelta
from .allocation import dt
from .session import digest,encode

FIELDS=('occurred_at','location','participants','history')

def evidence_revision(bundle):return digest({k:bundle.get(k) for k in ('existing','incoming','depends_on_cases','deadline_at')})

def rule_revision():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

@dataclass(frozen=True)
class MockSession:
    actor: str
    label: str
    expires_at: str
    scopes: frozenset

@dataclass(frozen=True)
class MockIntent:
    revision: str
    action: str
    payload_hash: str


def usable_fact(side,field):
    if not isinstance(side.get('event_id'),str) or not side['event_id']:return None,'event_scope_missing'
    fact=side.get('facts',{}).get(field,{})
    value=fact.get('value')
    if value in (None,'',[]):return None,'missing'
    if field=='participants':
        if not isinstance(value,list) or any(not isinstance(v,str) or not v for v in value):return None,'invalid_fact_type'
    elif not isinstance(value,str):return None,'invalid_fact_type'
    if field=='occurred_at' and fact.get('kind')!='incident':return None,'not_incident_time'
    citations={c['id']:c for c in side.get('citations',[])}
    if len(citations)!=len(side.get('citations',[])):return None,'ambiguous_citation_ids'
    if not fact.get('citation_ids'):return None,'citation_missing'
    for key in fact['citation_ids']:
        c=citations.get(key)
        if not c:return None,'citation_missing'
        a=side.get('article',{});quote=c.get('quote');kind=c.get('kind')
        if c.get('event_id')!=side.get('event_id') or c.get('article_version')!=digest(a):return None,'wrong_event_or_version'
        if c.get('source_url')!=a.get('url') or kind not in ('title','description','body'):return None,'wrong_source'
        if not isinstance(quote,str) or not quote or not isinstance(a.get(kind),str) or quote not in a[kind]:return None,'literal_mismatch'
        if c.get('source_status')!='operator_checked':return None,'source_unconfirmed'
        values=value if isinstance(value,list) else [value]
        if not all(isinstance(v,str) and v in quote for v in values):return None,'fact_not_in_quote'
    return value,'literal_and_operator_claim_only'


def assess(bundle):
    rows={};missing=[]
    for field in FIELDS:
        left,why_left=usable_fact(bundle.get('existing',{}),field);right,why_right=usable_fact(bundle.get('incoming',{}),field)
        state='unknown'
        if left is not None and right is not None:
            if field=='occurred_at':
                try:state='match' if dt(left)==dt(right) else 'different'
                except (ValueError,TypeError):state='unknown'
            else:state='match' if digest(left)==digest(right) else 'different'
        if state=='unknown':missing.append(field)
        rows[field]={'existing':left,'incoming':right,'comparison':state,'existing_check':why_left,'incoming_check':why_right}
    states={k:r['comparison'] for k,r in rows.items()}
    if all(states[k]=='match' for k in FIELDS):signal='same_evidence_signal'
    elif not missing and all(states[k]=='different' for k in ('occurred_at','location','history')):signal='distinct_evidence_signal'
    else:signal='uncertain'
    return {'rules_revision':rule_revision(),'signal':signal,'rows':rows,'missing':missing,'case_numbers_used':False,'identity_confirmed':False,
            'source_truth_independently_verified':False,'body_status':{s:bundle.get(s,{}).get('article',{}).get('body_status','unobtained') for s in ('existing','incoming')}}

class HoldWorkflow:
    def __init__(self,path):
        self.db=sqlite3.connect(path);self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS versions(case_id TEXT,revision TEXT,payload TEXT NOT NULL,PRIMARY KEY(case_id,revision));
          CREATE TABLE IF NOT EXISTS heads(case_id TEXT PRIMARY KEY,revision TEXT NOT NULL,state TEXT NOT NULL,first_seen TEXT NOT NULL,review_id TEXT,budget_wait INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS dependencies(parent TEXT,child TEXT,PRIMARY KEY(parent,child));
          CREATE TABLE IF NOT EXISTS checks(case_id TEXT PRIMARY KEY,revision TEXT NOT NULL,result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS operations(op TEXT PRIMARY KEY,request_hash TEXT NOT NULL,result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,op TEXT,case_id TEXT,revision TEXT,action TEXT,payload TEXT NOT NULL,previous_hash TEXT,record_hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS lab_publications(case_id TEXT PRIMARY KEY,revision TEXT,state TEXT NOT NULL,payload TEXT NOT NULL);
        ''');self.db.commit()
    def close(self):self.db.close()
    def verify_audit(self):
        previous=''
        for row in self.db.execute('SELECT op,case_id,revision,action,payload,previous_hash,record_hash FROM audit ORDER BY seq'):
            op,case,rev,action,payload,prior,current=row
            if prior!=previous or digest([op,case,rev,action,json.loads(payload),prior])!=current:raise ValueError('audit integrity failed')
            previous=current
        return previous
    def _append(self,op,case,rev,action,payload):
        previous=self.verify_audit();current=digest([op,case,rev,action,payload,previous])
        self.db.execute('INSERT INTO audit(op,case_id,revision,action,payload,previous_hash,record_hash) VALUES(?,?,?,?,?,?,?)',(op,case,rev,action,encode(payload),previous,current))
    def _transaction(self,op,request,fn,fail=False):
        if not op:raise ValueError('operation ID required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.verify_audit();old=self.db.execute('SELECT request_hash,result FROM operations WHERE op=?',(op,)).fetchone()
            if old:
                if old[0]!=digest(request):raise ValueError('operation conflict')
                self.db.rollback();return json.loads(old[1])
            result=fn();self.db.execute('INSERT INTO operations VALUES(?,?,?)',(op,digest(request),encode(result)))
            if fail:raise OSError('mock persistence failure')
            self.db.commit();return result
        except BaseException:self.db.rollback();raise
    def head(self,case):
        row=self.db.execute('SELECT revision,state,first_seen,review_id,budget_wait FROM heads WHERE case_id=?',(case,)).fetchone()
        if not row:raise ValueError('unknown case')
        return dict(zip(('revision','state','first_seen','review_id','budget_wait'),row))
    def bundle(self,case,rev=None):
        rev=rev or self.head(case)['revision'];row=self.db.execute('SELECT payload FROM versions WHERE case_id=? AND revision=?',(case,rev)).fetchone()
        if not row:raise ValueError('unknown revision')
        return json.loads(row[0])
    def descendants(self,case):
        found=set();stack=[case]
        while stack:
            parent=stack.pop()
            for child, in self.db.execute('SELECT child FROM dependencies WHERE parent=?',(parent,)):
                if child not in found and child!=case:found.add(child);stack.append(child)
        return found
    def _invalidate(self,case,op,at,actor):
        for child in sorted(self.descendants(case)):
            self.db.execute("UPDATE heads SET state='needs_review',review_id=NULL WHERE case_id=?",(child,))
            self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(child,))
            self._append(op,child,self.head(child)['revision'],'dependency_invalidated',{'parent':case,'actor':actor,'at':at})
    def ingest(self,op,case,bundle,at,fail=False):
        dt(at);bundle=copy.deepcopy(bundle);rev=evidence_revision(bundle)
        def save():
            row=self.db.execute('SELECT revision FROM heads WHERE case_id=?',(case,)).fetchone()
            if row and row[0]==rev:
                if self.bundle(case)!=bundle:self._append(op,case,rev,'metadata_observed',{'at':at,'input':bundle,'review_reopened':False})
                return {'state':'unchanged','revision':rev,'automatic_approval':False}
            for parent in bundle.get('depends_on_cases',[]):
                if parent==case or parent in self.descendants(case):raise ValueError('cyclic case dependency')
                self.head(parent)
            self.db.execute('DELETE FROM dependencies WHERE child=?',(case,))
            for parent in set(bundle.get('depends_on_cases',[])):self.db.execute('INSERT INTO dependencies VALUES(?,?)',(parent,case))
            self.db.execute('INSERT OR IGNORE INTO versions VALUES(?,?,?)',(case,rev,encode(bundle)))
            if row:
                self.db.execute("UPDATE heads SET revision=?,state='needs_review',review_id=NULL WHERE case_id=?",(rev,case))
                self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(case,))
            else:self.db.execute("INSERT INTO heads(case_id,revision,state,first_seen) VALUES(?,?,'needs_review',?)",(case,rev,at))
            if row:self._invalidate(case,op,at,'local_revision_import')
            self._append(op,case,rev,'ingest',{'at':at,'previous_revision':row[0] if row else None})
            return {'state':'needs_review','revision':rev,'automatic_approval':False}
        return self._transaction(op,{'kind':'ingest','case':case,'bundle':bundle,'at':at},save,fail)
    def local_check(self,case):
        head=self.head(case);signature=digest({'revision':head['revision'],'rules':rule_revision()});old=self.db.execute('SELECT revision,result FROM checks WHERE case_id=?',(case,)).fetchone()
        if old and old[0]==signature:return {'suppressed':True,'assessment':json.loads(old[1]),'API_calls':0}
        result=assess(self.bundle(case))
        with self.db:self.db.execute('INSERT OR REPLACE INTO checks VALUES(?,?,?)',(case,signature,encode(result)))
        return {'suppressed':False,'assessment':result,'API_calls':0}
    def preview(self,case):
        h=self.head(case);b=self.bundle(case);a=assess(b)
        return {'lab_only':True,'case_id':case,'revision':h['revision'],'review_id':h['review_id'],'rules_revision':rule_revision(),'comparison':a['rows'],
                'sources':{s:[{k:c.get(k) for k in ('id','source_url','quote','kind','source_status','article_version','event_id')} for c in b.get(s,{}).get('citations',[])] for s in ('existing','incoming')},
                'body_status':a['body_status'],'identity_confirmed':False}
    def review_is_current(self,case):
        h=self.head(case)
        if h['state']!='reviewed' or not h['review_id']:return False
        row=self.db.execute("SELECT payload FROM audit WHERE op=? AND case_id=? AND revision=? AND action='review'",(h['review_id'],case,h['revision'])).fetchone()
        return bool(row) and json.loads(row[0]).get('assessment',{}).get('rules_revision')==rule_revision()
    def act(self,op,case,rev,action,data,session,intent,at,fail=False):
        allowed={'review':'review','revoke':'review','archive':'review','recheck':'review','correct':'review','budget_pause':'review','budget_resume':'review','publish_lab':'publish','stop_lab':'publish','send_back':'publish'}
        scope=allowed.get(action)
        if not isinstance(session,MockSession) or not session.actor or scope not in session.scopes or dt(session.expires_at)<=dt(at):raise PermissionError('mock authentication required or expired')
        if not isinstance(intent,MockIntent) or intent!=MockIntent(rev,action,digest(data)):raise PermissionError('explicit action confirmation required')
        h=self.head(case)
        if h['revision']!=rev:raise ValueError('stale article/evidence/event revision')
        request={'case':case,'revision':rev,'action':action,'data':data,'actor':session.actor,'rules_revision':rule_revision()}
        def save():
            head=self.head(case);assessment=assess(self.bundle(case));state=head['state'];outcome='recorded'
            if action=='review':
                if not data.get('reason') or data.get('relation') not in ('same','distinct','hold'):raise ValueError('reason and relation required')
                expected={'same':'same_evidence_signal','distinct':'distinct_evidence_signal'}
                ready=data['relation'] in expected and assessment['signal']==expected.get(data['relation']) and not assessment['missing']
                self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(case,))
                state='reviewed' if ready else 'held';outcome='review_task_recorded' if ready else 'conflict_or_insufficient_evidence'
                self.db.execute('UPDATE heads SET state=?,review_id=? WHERE case_id=?',(state,op if ready else None,case))
            elif action=='publish_lab':
                if not self.review_is_current(case) or assessment['missing']:raise ValueError('current explicit review required')
                preview=self.preview(case)
                if not data.get('confirmed') or data.get('preview_hash')!=digest(preview):raise ValueError('current preview confirmation required')
                self.db.execute('INSERT OR REPLACE INTO lab_publications VALUES(?,?,?,?)',(case,rev,'lab_visible',encode(preview)))
                outcome='lab_preview_visible_only'
            elif action in ('stop_lab','send_back','correct','revoke'):
                if not data.get('reason'):raise ValueError('reason required')
                if action=='correct':
                    if data.get('type') not in ('wrong_merge','duplicate_incident','source_correction'):raise ValueError('correction type required')
                    for target in data.get('related_cases',[]):self.head(target)
                self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(case,))
                state='needs_review' if action in ('correct','send_back') else 'held'
                self.db.execute('UPDATE heads SET state=?,review_id=NULL WHERE case_id=?',(state,case));outcome='stopped_without_data_deletion'
            elif action=='archive':
                if not data.get('reason'):raise ValueError('reason required')
                state='archived';self.db.execute("UPDATE heads SET state='archived',review_id=NULL WHERE case_id=?",(case,));self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(case,));outcome='retained_not_excluded'
            elif action=='recheck':
                if not data.get('reason'):raise ValueError('manual recheck reason required')
                state='needs_review';self.db.execute("UPDATE heads SET state='needs_review',review_id=NULL WHERE case_id=?",(case,));self.db.execute("UPDATE lab_publications SET state='stopped' WHERE case_id=?",(case,))
            elif action in ('budget_pause','budget_resume'):
                self.db.execute('UPDATE heads SET budget_wait=? WHERE case_id=?',(int(action=='budget_pause'),case));outcome='no_call_or_data_loss'
            if action in ('correct','revoke','stop_lab','send_back','archive','recheck'):self._invalidate(case,op,at,session.actor)
            self._append(op,case,rev,action,{'actor':session.actor,'at':at,'data':data,'assessment':assessment,'outcome':outcome,'lab_preview':self.preview(case) if action=='publish_lab' else None})
            return {'state':state,'outcome':outcome,'production_write':False,'new_event_ids':0,'automatic_merge':False,'publication_approval':False,'API_calls':0}
        result=self._transaction(op,request,save,fail)
        current=self.head(case)['state'];visible=self.export_lab(case)['state']
        if result['outcome']=='lab_preview_visible_only' and visible!='lab_visible':result['outcome']='historical_operation_not_reapplied'
        result['state']=current;result['lab_visibility']=visible
        return result
    def export_lab(self,case):
        self.verify_audit()
        row=self.db.execute('SELECT state,payload FROM lab_publications WHERE case_id=?',(case,)).fetchone()
        head=self.head(case)
        valid=bool(row) and row[0]=='lab_visible' and json.loads(row[1]).get('revision')==head['revision'] and json.loads(row[1]).get('review_id')==head['review_id'] and self.review_is_current(case) and json.loads(row[1]).get('rules_revision')==rule_revision()
        if not valid:return {'lab_only':True,'case_id':case,'state':'not_visible','notice':'確認中のため表示を停止しています','reader_content':None}
        return {'lab_only':True,'case_id':case,'state':'lab_visible','reader_content':json.loads(row[1])}
    def tasks(self,at,limit=4):
        if limit<1:raise ValueError('manual card limit must be positive')
        now=dt(at);tasks=[];waiting=[]
        for case, in self.db.execute('SELECT case_id FROM heads'):
            h=self.head(case);assessment=assess(self.bundle(case));age=max(0,(now-dt(h['first_seen'])).total_seconds()/3600)
            bundle=self.bundle(case);deadline=bundle.get('deadline_at');due=False
            if deadline:
                try:due=dt(deadline)<=now
                except (ValueError,TypeError):pass
            stopped=self.db.execute('SELECT 1 FROM lab_publications WHERE case_id=?',(case,)).fetchone() is not None and self.export_lab(case)['state']!='lab_visible'
            task={'deadline_due':due,'stopped_preview':stopped,'case_id':case,'revision':h['revision'],'age_hours':round(age,2),'budget_wait':bool(h['budget_wait']),'reason':assessment['signal']}
            if h['state'] in ('held','archived'):waiting.append({**task,'storage_review_due':age>=7*24,'state':h['state'],'next':'追加根拠または明示的な再確認依頼を待つ'});continue
            if h['state']=='reviewed' and self.export_lab(case)['state']=='lab_visible':continue
            if self.review_is_current(case):task.update(type='publication_preview',priority=1)
            else:task.update(type='review',priority=0 if stopped or due or assessment['signal']=='same_evidence_signal' else 2)
            tasks.append(task)
        selected=[]
        if tasks:selected.append(min(tasks,key=lambda t:(-t['age_hours'],t['case_id'])))
        rest=sorted((t for t in tasks if t not in selected),key=lambda t:(t['priority'],-t['age_hours'],t['case_id']))
        selected+=rest[:max(0,limit-len(selected))]
        return {'actionable':selected,'actionable_total':len(tasks),'waiting_for_evidence':sorted(waiting,key=lambda t:(-t['age_hours'],t['case_id'])),'manual_card_limit_not_API_cap':limit}
