"""Additive offline article read-model. No incident merge or networking API.

Only sep_* tables may be written. Metadata equality is neither full-body
identity nor proof that two incidents are the same. All original tables stay
under the earlier engine's source-bound validators.
"""
import hashlib
import json
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from engine import atomic,dumps
from merge_safety import normalized,source_context
from operator_revalidation import validate_records,diagnose

def digest(value):return hashlib.sha256(dumps(value).encode()).hexdigest()
def article_core(article):return {key:article.get(key,'') for key in ('url','title','description','published')}
def version_id(article):return digest({'article_snapshot':article_core(article)})

def initialize(db):
    # executescript would commit an existing caller transaction; use savepoints.
    with atomic(db):
        for statement in Path(__file__).with_name('article_store.sql').read_text().split(';'):
            if statement.strip():db.execute(statement)

def save_version(db,article,source,pointer):
    if not isinstance(article,dict) or not isinstance(article.get('url'),str) or not article['url'] or not isinstance(article.get('title'),str):
        raise ValueError('article URL/title required')
    content={'title':article['title'],'description':article.get('description',''),'body_text':None}
    cid=digest({'representation':'stored_metadata','content':content});vid=version_id(article)
    db.execute('INSERT OR IGNORE INTO sep_urls VALUES(?)',(article['url'],))
    db.execute('INSERT OR IGNORE INTO sep_contents VALUES(?,?,?)',(cid,'stored_metadata',dumps(content)))
    db.execute('INSERT OR IGNORE INTO sep_versions VALUES(?,?,?,?,?)',(vid,article['url'],cid,article.get('published',''),dumps(article_core(article))))
    occurrence={'version_id':vid,'source':source,'pointer':pointer,'raw_payload_hash':digest(article)}
    # No source observation/check time is inferred from publication/review time.
    db.execute('INSERT OR IGNORE INTO sep_occurrences VALUES(?,?,?,?,?,?)',
        (digest(occurrence),vid,source,pointer,None,dumps(article)))
    observation={'version':vid,'basis':'legacy_metadata_only'}
    db.execute('INSERT OR IGNORE INTO sep_fetch_observations VALUES(?,?,?,?,?,?,?,?,?)',
        (digest(observation),vid,'not_attempted','legacy_metadata_only',None,None,None,None,dumps(observation)))
    return vid

def walk_articles(value,pointer=''):
    if isinstance(value,dict):
        if isinstance(value.get('url'),str) and isinstance(value.get('title'),str):yield pointer,value
        for key,child in value.items():
            escaped=str(key).replace('~','~0').replace('/','~1')
            yield from walk_articles(child,pointer+'/'+escaped)
    elif isinstance(value,list):
        for i,child in enumerate(value):yield from walk_articles(child,pointer+'/'+str(i))

def import_saved(db,fail_after=None):
    """Append a faithful read-model of existing source_files/events/revisions."""
    initialize(db);count=0
    with atomic(db):
        for source,payload in db.execute('SELECT path,payload FROM source_files ORDER BY path').fetchall():
            for pointer,article in walk_articles(json.loads(payload)):
                save_version(db,article,source,pointer);count+=1
                if count==fail_after:raise RuntimeError('simulated article import failure')
        for rid,payload in db.execute('SELECT revision_id,payload FROM revisions').fetchall():
            vid=save_version(db,json.loads(payload),'legacy.revisions',rid)
            db.execute('INSERT OR IGNORE INTO sep_legacy_versions VALUES(?,?)',(rid,vid))
        for eid,payload in db.execute('SELECT event_id,payload FROM events').fetchall():
            event=json.loads(payload)
            for role,articles in [('current',event.get('articles',[])),
                                  ('history',[v.get('article',v) for v in event.get('source_revisions',[])])]:
                for i,article in enumerate(articles):
                    vid=save_version(db,article,'legacy.events',eid+'/'+role+'/'+str(i))
                    db.execute('INSERT OR IGNORE INTO sep_event_mentions VALUES(?,?,?,?,?)',(eid,vid,role,None,'imported_not_revalidated'))

def compare_articles(db,left,right):
    """Record a publication-level relation; this function has no event writes."""
    with atomic(db):
        lv=save_version(db,left,'pair_assessment','reference');rv=save_version(db,right,'pair_assessment','incoming')
        a,b=source_context(left),source_context(right)
        score=SequenceMatcher(None,normalized(a['title']),normalized(b['title']),autojunk=False).ratio()
        if lv==rv:relation,status='exact_saved_version','saved_core_equal'
        elif left['title']==right['title'] and left.get('published','')==right.get('published',''):
            relation,status='same_headline_time_candidate','unconfirmed'
        elif score>=0.95:relation,status='similar_headline_candidate','unconfirmed'
        else:relation,status='no_duplicate_signal','unconfirmed'
        payload={'headline_similarity':score,'event_identity':'not_inferred','body_equality':'unconfirmed',
                 'automatic_event_merge':False,'syndication_confirmed':False}
        db.execute('INSERT OR IGNORE INTO sep_article_relations VALUES(?,?,?,?,?)',(lv,rv,relation,status,dumps(payload)))
    return {'left_version':lv,'right_version':rv,'relation':relation,'status':status,**payload}

def timestamp(value,required=False):
    if value is None and not required:return
    if not isinstance(value,str):raise ValueError('timestamp must be supplied explicitly')
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError as exc:raise ValueError('invalid timestamp') from exc
    if parsed.tzinfo is None:raise ValueError('timestamp needs timezone')

def record_fetch(db,vid,result,basis,observed_at=None,reported_at=None,http_status=None,body_text=None,error=None,report_hash=None):
    timestamp(observed_at);timestamp(reported_at)
    if result not in ('available','empty_body') and body_text is not None:raise ValueError('body supplied for a non-body outcome')
    if http_status is not None and (type(http_status) is not int or not 100<=http_status<=599):raise ValueError('invalid HTTP status')
    if basis not in ('operator_report','synthetic','provided_observation'):raise ValueError('invalid observation basis')
    if result in ('reported_deleted','reported_blank_display'):
        if basis!='operator_report' or observed_at is not None or http_status is not None or not report_hash:
            raise ValueError('operator report is not a fetch observation')
        report=db.execute('SELECT payload FROM sep_review_records WHERE record_hash=?',(report_hash,)).fetchone()
        if report is None or reported_at!=json.loads(report[0])['reviewed_at']:
            raise ValueError('report must reference the preserved operator record and its timestamp')
    elif result in ('request_failed','deleted','empty_body','available'):
        if basis=='operator_report':raise ValueError('operator statement cannot assert a fetch result')
        timestamp(observed_at,required=True)
        if result=='deleted' and http_status not in (404,410):raise ValueError('deletion needs explicit 404/410 observation')
        if result in ('empty_body','available') and (type(http_status) is not int or not 200<=http_status<300):raise ValueError('successful response status required')
        if result=='empty_body' and body_text!='':raise ValueError('empty extraction text required')
        if result=='available' and (not isinstance(body_text,str) or not body_text.strip()):raise ValueError('body text required')
        if result=='request_failed' and not error:raise ValueError('failure detail required')
    else:raise ValueError('unknown observation result')
    body={'body_text':body_text} if result in ('available','empty_body') else None
    body_id=digest({'representation':'provided_body','content':body}) if body is not None else None
    payload={'version':vid,'result':result,'basis':basis,'observed_at':observed_at,'reported_at':reported_at,
             'http_status':http_status,'body_content_id':body_id,'error':error,'report_hash':report_hash,
             'identity_evidence':False}
    with atomic(db):
        if body is not None:db.execute('INSERT OR IGNORE INTO sep_contents VALUES(?,?,?)',(body_id,'provided_body',dumps(body)))
        db.execute('INSERT OR IGNORE INTO sep_fetch_observations VALUES(?,?,?,?,?,?,?,?,?)',
            (digest(payload),vid,result,basis,observed_at,reported_at,http_status,body_id,dumps(payload)))
    return digest(payload)

def import_reviews(db,dataset_raw,review_raw):
    dataset,records=validate_records(review_raw,dataset_raw)
    event_payloads=[json.loads(r[0]) for r in db.execute('SELECT payload FROM events')]
    with atomic(db):
        for row in dataset['rows']:
            record=records.get(row['sample_id'])
            if record is None:continue
            rh=digest(record)
            db.execute('INSERT OR IGNORE INTO sep_review_records VALUES(?,?,?,?)',(rh,row['sample_id'],record['event_id'],dumps(record)))
            classification=diagnose(row,record,event_payloads)
            for role in classification['operator_reported_deleted']:
                vid=save_version(db,row[role+'_article'],'evaluation',row['sample_id']+'/'+role)
                record_fetch(db,vid,'reported_deleted','operator_report',reported_at=record['reviewed_at'],report_hash=rh)
            for role in classification['operator_reported_body_missing']:
                vid=save_version(db,row[role+'_article'],'evaluation',row['sample_id']+'/'+role)
                record_fetch(db,vid,'reported_blank_display','operator_report',reported_at=record['reviewed_at'],report_hash=rh)
    return dataset,records

def quote_template(dataset_raw,review_raw):
    dataset,records=validate_records(review_raw,dataset_raw);pairs=[]
    for row in dataset['rows']:
        r=records.get(row['sample_id'])
        if not r or r['choice']!='same_event' or not r['incoming_body_checked'] or not r['reference_body_checked']:continue
        pairs.append({'sample_id':row['sample_id'],'event_id':r['event_id'],'evidence_sha256':row['evidence_sha256'],
            'operator_decision_recorded_at':r['reviewed_at'],'sources':[
                {'role':role,'url':row[role+'_article']['url'],'version_id':version_id(row[role+'_article']),
                 'checked_at':None,'source_body_sha256':None,'quotes':[]} for role in ('reference','incoming')]})
    return {'schema':'news-now-source-quotes-v1','repository':'komatsubarakai-alt/news-now',
        'record_scope':'evaluation_quote_draft_only','dataset_sha256':hashlib.sha256(dataset_raw).hexdigest(),
        'review_file_sha256':hashlib.sha256(review_raw).hexdigest(),'pairs':pairs}

def validate_quotes(document,dataset_raw,review_raw):
    expected=quote_template(dataset_raw,review_raw)
    if not isinstance(document,dict) or set(document)!=set(expected) or any(document[k]!=v for k,v in expected.items() if k!='pairs'):
        raise ValueError('quote document schema/scope/dataset mismatch')
    if not isinstance(document['pairs'],list) or len(document['pairs'])!=len(expected['pairs']):raise ValueError('incomplete quote pairs')
    by_id={p['sample_id']:p for p in expected['pairs']};seen=set()
    for pair in document['pairs']:
        if not isinstance(pair,dict) or pair.get('sample_id') not in by_id or pair['sample_id'] in seen:raise ValueError('duplicate or unknown pair')
        source=by_id[pair['sample_id']];seen.add(pair['sample_id'])
        if set(pair)!=set(source) or any(pair[k]!=v for k,v in source.items() if k!='sources'):raise ValueError('pair identity or decision timestamp mismatch')
        sources=pair['sources']
        if not isinstance(sources,list) or len(sources)!=2:raise ValueError('two quote sources required')
        roles=set()
        for item in sources:
            if not isinstance(item,dict) or item.get('role') not in ('incoming','reference') or item['role'] in roles:raise ValueError('duplicate quote role')
            roles.add(item['role']);template=next(x for x in source['sources'] if x['role']==item['role'])
            if set(item)!=set(template) or any(item[k]!=template[k] for k in ('url','version_id')):raise ValueError('quote source/version mismatch')
            timestamp(item['checked_at'])
            body_sha=item['source_body_sha256']
            if body_sha is not None and (not isinstance(body_sha,str) or len(body_sha)!=64 or any(c not in '0123456789abcdef' for c in body_sha)):
                raise ValueError('invalid body content hash')
            if not isinstance(item['quotes'],list) or len(item['quotes'])>10 or any(not isinstance(q,str) or not q.strip() or len(q)>500 for q in item['quotes']):
                raise ValueError('quotes must be short explicit source strings')
    # Even a complete operator submission is not source-verified or gold data.
    return {'pairs':len(seen),'sources_with_quotes':sum(bool(s['quotes']) for p in document['pairs'] for s in p['sources']),
            'sources_with_check_time':sum(s['checked_at'] is not None for p in document['pairs'] for s in p['sources']),
            'source_verified':False,'gold_promoted':False}

def import_quotes(db,document,dataset_raw,review_raw):
    summary=validate_quotes(document,dataset_raw,review_raw)
    dataset=json.loads(dataset_raw);rows={r['sample_id']:r for r in dataset['rows']};dh=digest(document)
    with atomic(db):
        db.execute('INSERT OR IGNORE INTO sep_quote_documents VALUES(?,?)',(dh,dumps(document)))
        for pair in document['pairs']:
            for s in pair['sources']:
                vid=save_version(db,rows[pair['sample_id']][s['role']+'_article'],'quote_source',pair['sample_id']+'/'+s['role'])
                db.execute('INSERT OR IGNORE INTO sep_quote_sources VALUES(?,?,?,?,?,?,?,?)',
                    (dh,pair['sample_id'],s['role'],pair['event_id'],vid,s['checked_at'],dumps(s['quotes']),
                     'operator_text_not_source_verified' if s['quotes'] else 'not_provided'))
    return summary
