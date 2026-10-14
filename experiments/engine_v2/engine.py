"""Offline-only SQLite shadow. No HTTP client, production writer, or AI credentials."""
import copy
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

from failed_response import digest, validation_revision, event_conditions
from merge_safety import UpdateHeld, article_content, validate_saved_update
from pending_triage import fingerprint, group_key, preflight, source_saved
from operator_review import active_selection
from tracking_v1 import apply_report, evidence, event_text


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def connect():
    db = sqlite3.connect(':memory:')
    db.executescript(Path(__file__).with_name('schema.sql').read_text())
    return db


def revision(db, article):
    rid = fingerprint({'articles': [article]})
    db.execute('INSERT OR IGNORE INTO articles VALUES(?)', (article['url'],))
    db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?)', (rid, article['url'], dumps(article)))
    return rid


def import_snapshot(db, root, fail_after=None):
    """Atomic import; preserve raw input including unknown fields. Never infer labels."""
    names = ('events.json', 'tracking_pending.json', 'news_status.json', 'tracking_triage.json',
             'status_pending.json', 'operator_review_decisions.json', 'data_corrections.json',
             'ai_budget.json', 'ai_response_cache.json', 'tracking_review_recent.json')
    files = {name: (Path(root) / name).read_text() for name in names if (Path(root) / name).exists()}
    with db:
        for name, text in files.items():
            db.execute('INSERT INTO source_files VALUES(?,?,?)', (name, hashlib.sha256(text.encode()).hexdigest(), text))
        for i, event in enumerate(json.loads(files.get('events.json', '[]'))):
            eid = event['event_id']
            db.execute('INSERT INTO events VALUES(?,?)', (eid, dumps(event)))
            for alias in [event.get('event_name', '')] + event.get('aliases', []):
                if isinstance(alias, str) and alias:
                    db.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?)', (eid, alias, 'stored'))
            for role, articles in [('current', event.get('articles', [])),
                                   ('history', [x.get('article', x) for x in event.get('source_revisions', [])])]:
                for article in articles:
                    rid = revision(db, article)
                    db.execute('INSERT OR IGNORE INTO event_articles VALUES(?,?,?)', (eid, rid, role))
            for position, item in enumerate(event.get('progress_history', [])):
                db.execute('INSERT INTO progress(event_id,position,payload) VALUES(?,?,?)', (eid, position, dumps(item)))
                for proof in item.get('progress_evidence', []):
                    db.execute('INSERT INTO facts(event_id,kind,source_url,quote,payload,verification) VALUES(?,?,?,?,?,?)',
                               (eid, 'legacy_progress', proof.get('url'), proof.get('quote'), dumps(proof), 'imported_not_revalidated'))
            if event.get('completion_evidence'):
                db.execute('INSERT INTO facts(event_id,kind,payload,verification) VALUES(?,?,?,?)',
                           (eid, 'legacy_completion', dumps(event['completion_evidence']), 'imported_not_revalidated'))
            for schedule in event.get('schedules', []):
                for version, item in enumerate(schedule.get('history', []) + [schedule]):
                    db.execute('INSERT INTO schedules VALUES(?,?,?,?)', (eid, schedule['schedule_id'], version, dumps(item)))
            if fail_after == i:
                raise RuntimeError('simulated import interruption')
        for name in ('tracking_pending.json', 'status_pending.json', 'news_status.json'):
            for group in json.loads(files.get(name, '[]')):
                db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)', (name, group_key(group), dumps(group)))
                for article in group.get('articles', []):
                    revision(db, article)
        triage = json.loads(files.get('tracking_triage.json', '{}')).get('items', {})
        for key, item in triage.items():
            db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)', ('tracking_triage', key, dumps(item)))
            for record in [item] + item.get('source_history', []):
                for article in record.get('group', {}).get('articles', []):
                    revision(db, article)
            for signature, memo in item.get('rejected_responses', {}).items():
                db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?)',
                           ('legacy:' + key + ':' + signature, key, None, dumps(memo), 'legacy_rejected', memo.get('reason')))
        operators = json.loads(files.get('operator_review_decisions.json', '{}'))
        for position, item in enumerate(operators.get('decisions', []) if isinstance(operators, dict) else operators):
            db.execute('INSERT INTO operator_decisions VALUES(?,?)', (item.get('decision_id', str(position)), dumps(item)))
        for key,payload in json.loads(files.get('ai_response_cache.json','{}')).items():
            db.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?,?,?)',
                       ('cache:'+key,None,dumps(payload),None,'imported_cache_not_revalidated','legacy_request_key'))
        if 'ai_budget.json' in files:
            db.execute('INSERT INTO budget VALUES(?,?)', ('legacy', files['ai_budget.json']))


def events(db):
    return [json.loads(row[0]) for row in db.execute('SELECT payload FROM events ORDER BY rowid')]


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(text)).casefold())


def grams(text):
    text = normalize(text)
    # Character grams work with Japanese without a word-segmentation dependency.
    return {text[i:i+3] for i in range(max(0, len(text)-2))}


def rank(db, group, triage=None):
    """Retain every legacy-eligible event; rank is retrieval, never identity proof."""
    all_events = events(db)
    selection = active_selection(group, triage or {})
    query = normalize(group.get('event_name', '') + ' ' + ' '.join(article_content(a) for a in group.get('articles', [])))
    qgrams = grams(query)
    texts = {}
    for event in all_events:
        aliases = [r[0] for r in db.execute('SELECT alias FROM aliases WHERE event_id=?', (event['event_id'],))]
        texts[event['event_id']] = ' '.join(aliases + [article_content(a) for a in event.get('articles', [])] + [article_content(h.get('article',h)) for h in event.get('source_revisions',[])])
    frequency = Counter(g for text in texts.values() for g in grams(text))
    output = []
    for event in all_events:
        if event.get('publication_status') == 'held':
            continue
        eid = event['event_id']; common = qgrams & grams(texts[eid])
        score = sum(1 / frequency[g] for g in common)
        reasons = {'shared_character_grams': len(common), 'all_candidates_retained': True}
        if {a['url'] for a in group.get('articles', [])} & {a['url'] for a in event.get('articles', [])}:
            score += 10000; reasons['known_url'] = True
        if selection and selection['action'] == 'same_event' and selection['event_id'] == eid:
            score += 1000000; reasons['operator_target'] = True
        # Identifiers are scored only as literal shared source text, not guessed.
        for identifier in re.findall(r'(?:令和|平成)\d+[^\s]{0,4}第\d+号|[A-Z]{2,}-\d{3,}', query, re.I):
            if identifier in normalize(texts[eid]):
                score += 1000; reasons['shared_case_number'] = True
        output.append({'event_id': eid, 'score': score, 'reasons': reasons, 'event': event})
    output.sort(key=lambda r: (-r['score'], r['event_id']))
    with db:
        db.execute('DELETE FROM candidates WHERE stage=? AND group_key=?', ('tracking', group_key(group)))
        for position, row in enumerate(output, 1):
            row['rank'] = position
            db.execute('INSERT INTO candidates VALUES(?,?,?,?,?,?,?)',
                       ('tracking', group_key(group), row['event_id'], position, row['score'], dumps(row['reasons']), 'unexamined'))
    return output


def judge(db, group, answer, now, budget_available=True, triage=None, registry=None, fail_save=False):
    """Mock-answer injection only. No real client. Validate before a single SQL commit."""
    rows = rank(db, group, triage)
    all_events = events(db); known = {a['url']: a for e in all_events for a in e.get('articles', [])}
    reason = preflight(group, known, registry or {}, triage or {})
    if reason:
        with db:
            db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)', ('judgment', group_key(group), dumps({'group':group,'status':reason})))
        return reason
    conditions = {'group':group,'events':event_conditions(all_events),'registry':registry or {},
                  'selection':active_selection(group, triage or {}),'validator':validation_revision('tracking'),
                  'model':'gpt-5.4-mini','output_limit':1200,
                  'prototype_code':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'prototype_version': 'v1', 'source_history': (triage or {}).get(group_key(group),{}).get('source_history',[]),
                  'changes': list(db.execute('SELECT id,reversed FROM changes ORDER BY id'))}
    signature = digest({'conditions':conditions,'answer':answer})
    prior = db.execute('SELECT status FROM decisions WHERE signature=?', (signature,)).fetchone()
    if prior and prior[0] == 'rejected':
        return 'same_rejected_answer'
    if not budget_available:
        with db:
            db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)', ('judgment', group_key(group), dumps({'group':group,'status':'budget_deferred'})))
        return 'budget_deferred'
    target = next((r['event'] for r in rows if r['event_id'] == answer.get('event_id')), None)
    before = copy.deepcopy(target)
    try:
        selection = active_selection(group, triage or {})
        if selection and (answer.get('relation') != selection['action'] or
                          (selection['action']=='same_event' and answer.get('event_id')!=selection['event_id'])):
            raise UpdateHeld('operator_selection_not_supported')
        fresh = [a for a in group.get('articles', []) if not source_saved(a, known)]
        if answer.get('confidence') != 'high' or not evidence(answer, fresh):
            raise UpdateHeld('uncertain_or_invalid_evidence')
        revised = {a['url'] for a in fresh if a['url'] in known}
        if revised and (target is None or not revised <= {a['url'] for a in target['articles']}):
            raise UpdateHeld('source_revision_target_requires_review')
        if answer.get('relation') != 'same_event' or target is None:
            raise UpdateHeld('unresolved_candidates_no_new_event_prototype')
        candidate = copy.deepcopy(target)
        apply_report(candidate, fresh, answer, now)
        validate_saved_update(target, candidate)
        with db:
            db.execute('UPDATE events SET payload=? WHERE event_id=?', (dumps(candidate), target['event_id']))
            project_event(db,candidate,'validated_mock')
            db.execute('INSERT INTO changes(event_id,before_payload,after_payload,reason) VALUES(?,?,?,?)',
                       (target['event_id'],dumps(before),dumps(candidate),'validated_mock'))
            if fail_save: raise RuntimeError('simulated save interruption')
            db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?)',
                       (signature,group_key(group),dumps(answer),dumps(conditions),'accepted','validated'))
            db.execute('UPDATE candidates SET state=? WHERE group_key=? AND event_id=?', ('validated',group_key(group),target['event_id']))
        return 'accepted'
    except UpdateHeld as exc:
        with db:
            db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?)',
                       (signature,group_key(group),dumps(answer),dumps(conditions),'rejected',str(exc)))
            db.execute('INSERT OR REPLACE INTO holds VALUES(?,?,?)',
                       ('judgment',group_key(group),dumps({'group':group,'status':'needs_evidence','reason':str(exc)})))
        return 'needs_evidence'


def undo(db, change_id):
    row = db.execute('SELECT event_id,before_payload,after_payload,reversed FROM changes WHERE id=?', (change_id,)).fetchone()
    if not row or row[3]: raise ValueError('unknown_or_already_reversed')
    current = db.execute('SELECT payload FROM events WHERE event_id=?', (row[0],)).fetchone()[0]
    if current != row[2]: raise ValueError('later_changes_require_review')
    with db:
        db.execute('UPDATE events SET payload=? WHERE event_id=?', (row[1],row[0]))
        db.execute('UPDATE changes SET reversed=1 WHERE id=?', (change_id,))
        project_event(db,json.loads(row[1]),'restored_not_revalidated')
        # Decision history is immutable; change/reversal metadata invalidates context.


def reserve_mock(db, input_bytes, now, output_tokens=1200):
    """Conservative SQL reservation for a hypothetical call; never performs a call."""
    from ai_cost_control import INPUT_RATE, OUTPUT_RATE, MODEL, periods
    if input_bytes < 0 or not 1 <= output_tokens <= 3500:
        raise ValueError('invalid_reservation')
    day, month = periods(now)
    amount = int(((input_bytes + 2048) * INPUT_RATE + output_tokens * OUTPUT_RATE).to_integral_value(rounding='ROUND_CEILING'))
    with db:
        row = db.execute('SELECT payload FROM budget WHERE name=?', ('simulation',)).fetchone()
        if not row:
            row = db.execute('SELECT payload FROM budget WHERE name=?', ('legacy',)).fetchone()
        state = json.loads(row[0]) if row else {'days':{},'months':{}}
        daily = state.setdefault('days', {}).setdefault(day, {'reserved_micro_usd':0,'calls':0})
        monthly = state.setdefault('months', {}).setdefault(month, {'reserved_micro_usd':0,'calls':0})
        run=state.setdefault('run',{'calls':0,'tracking_calls':0})
        if state.get('billing_blocked') or daily['reserved_micro_usd']+amount>200000 or monthly['reserved_micro_usd']+amount>5000000 or daily['calls']>=40 or run.get('calls',0)>=24 or run.get('tracking_calls',0)>=8:
            return {'status':'budget_deferred','reserved_micro_usd':0,'model':MODEL}
        for record in (daily,monthly):
            record['reserved_micro_usd']+=amount; record['calls']+=1
        run['calls']+=1;run['tracking_calls']=run.get('tracking_calls',0)+1
        db.execute('INSERT OR REPLACE INTO budget VALUES(?,?)', ('simulation',dumps(state)))
    return {'status':'reserved_simulation_only','reserved_micro_usd':amount,'model':MODEL}


def project_event(db,event,verification):
    """Derived rows change in the caller's event transaction; raw histories remain."""
    eid=event['event_id']
    db.execute('DELETE FROM event_articles WHERE event_id=? AND role=?',(eid,'current'))
    for a in event.get('articles',[]):
        db.execute('INSERT OR IGNORE INTO event_articles VALUES(?,?,?)',(eid,revision(db,a),'current'))
    for item in event.get('source_revisions',[]):
        a=item.get('article',item)
        db.execute('INSERT OR IGNORE INTO event_articles VALUES(?,?,?)',(eid,revision(db,a),'history'))
    for table in ('facts','progress','schedules'):
        db.execute('DELETE FROM '+table+' WHERE event_id=?',(eid,))
    for position,item in enumerate(event.get('progress_history',[])):
        db.execute('INSERT INTO progress(event_id,position,payload) VALUES(?,?,?)',(eid,position,dumps(item)))
        for proof in item.get('progress_evidence',[]):
            db.execute('INSERT INTO facts(event_id,kind,source_url,quote,payload,verification) VALUES(?,?,?,?,?,?)',
                       (eid,'progress',proof.get('url'),proof.get('quote'),dumps(proof),verification))
    if event.get('completion_evidence'):
        db.execute('INSERT INTO facts(event_id,kind,payload,verification) VALUES(?,?,?,?)',
                   (eid,'completion',dumps(event['completion_evidence']),verification))
    for schedule in event.get('schedules',[]):
        for version,item in enumerate(schedule.get('history',[])+[schedule]):
            db.execute('INSERT INTO schedules VALUES(?,?,?,?)',(eid,schedule['schedule_id'],version,dumps(item)))
