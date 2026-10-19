"""Explicit event/version quotation records. No inferred old-version binding."""
import json

from engine import atomic, dumps
from failed_response import digest
from article_store import timestamp, version_id
from tracking_v1 import safe_url
import revision_sync as sync

KINDS = {'headline': 'title', 'rss_description': 'description', 'body': 'body', 'unknown': None}


def initialize(db):
    sync.initialize(db)
    with atomic(db):
        db.execute('''CREATE TABLE IF NOT EXISTS bound_quotes(
          quote_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, command_hash TEXT NOT NULL,
          event_id TEXT REFERENCES events, url TEXT NOT NULL, state_id TEXT REFERENCES sync_source_states,
          version_id TEXT, content_hash TEXT, kind TEXT NOT NULL, quote TEXT NOT NULL,
          start_pos INTEGER, end_pos INTEGER, origin TEXT NOT NULL, response_record_id TEXT,
          sync_evidence_id TEXT REFERENCES sync_evidence, initial_status TEXT NOT NULL,
          saved_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS quote_confirmations(
          operation_id TEXT PRIMARY KEY, command_hash TEXT NOT NULL, quote_id TEXT REFERENCES bound_quotes,
          kind TEXT NOT NULL, actor TEXT NOT NULL, checked_at TEXT NOT NULL,
          decision_id TEXT REFERENCES sync_decisions)''')


def text_for(db, state_id, kind):
    row = db.execute('SELECT version_id,body_id,components FROM sync_source_states WHERE state_id=?', (state_id,)).fetchone()
    if row is None: raise ValueError('unknown article state')
    vid, bid, components = row; components = json.loads(components)
    if kind == 'body':
        if bid is None or components['body'] is None: return vid, None, None
        payload = json.loads(db.execute('SELECT payload FROM sep_contents WHERE content_id=?', (bid,)).fetchone()[0])
        if digest({'representation': 'provided_body', 'content': payload}) != bid:
            raise ValueError('body content checksum mismatch')
        text = payload['body_text']
    else:
        article = json.loads(db.execute('SELECT payload FROM sep_versions WHERE version_id=?', (vid,)).fetchone()[0])
        if version_id(article) != vid: raise ValueError('metadata content checksum mismatch')
        text = article.get(KINDS[kind], '')
    return vid, digest(text), text


def record(db, operation_id, *, event_id, url, quote, kind, origin, now,
           state_id=None, position=None, response_record_id=None, fail_save=False):
    initialize(db); timestamp(now.isoformat(), required=True)
    if kind not in KINDS or origin not in ('saved_ai_response', 'operator_input', 'mock_response'):
        raise ValueError('explicit quote kind and origin required')
    if not isinstance(quote, str) or not quote or len(quote) > 500 or not isinstance(operation_id, str) or not operation_id:
        raise ValueError('short quotation and operation required')
    if not safe_url(url): raise ValueError('safe quote URL required')
    if event_id is not None and not db.execute('SELECT 1 FROM events WHERE event_id=?', (event_id,)).fetchone():
        raise ValueError('unknown quote event')
    command = dict(event=event_id, url=url, quote=quote, kind=kind, origin=origin,
                   state=state_id, position=position, response=response_record_id)
    key = digest(command)
    old = db.execute('SELECT command_hash,quote_id FROM bound_quotes WHERE operation_id=?', (operation_id,)).fetchone()
    if old:
        if old[0] != key: raise ValueError('quote operation reused for different content')
        return old[1]
    with atomic(db):
        vid = content_hash = start = end = proof = None; status = 'unbound_version'
        if state_id is not None:
            if kind == 'unknown': raise ValueError('quote source kind is unconfirmed')
            if origin == 'saved_ai_response':
                from response_provenance import input_record
                row = db.execute('SELECT capture_id FROM response_records WHERE record_id=?', (response_record_id,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='response_records'").fetchone() else None
                if row is None: raise ValueError('AI quotation needs original captured response')
                captured = input_record(db, row[0])
                if not any(s['url'] == url and s['state'] and s['state']['state_id'] == state_id for s in captured['sources']):
                    raise ValueError('AI quote state was not captured')
                if event_id not in captured['event_ids']: raise ValueError('AI quote event outside captured scope')
                from ai_cost_control import json_answer
                answer_payload = json.loads(db.execute('SELECT payload FROM response_records WHERE record_id=?', (response_record_id,)).fetchone()[0])
                answer = json_answer(answer_payload)
                if answer.get('event_id') != event_id: raise ValueError('AI quote target is not the answered event')
                def values(value):
                    if isinstance(value, str): yield value
                    elif isinstance(value, dict):
                        for child in value.values(): yield from values(child)
                    elif isinstance(value, list):
                        for child in value: yield from values(child)
                if not any(quote in value for value in values(answer)): raise ValueError('quote not present in captured answer')
            state = db.execute('SELECT url FROM sync_source_states WHERE state_id=?', (state_id,)).fetchone()
            if state != (url,): raise ValueError('quote URL/state mismatch')
            vid, content_hash, text = text_for(db, state_id, kind)
            status = 'body_missing' if text is None else 'literal_mismatch'
            if text is not None:
                if position is None:
                    found = [i for i in range(len(text)) if text.startswith(quote, i)]
                    if len(found) == 1: start, end = found[0], found[0] + len(quote)
                    elif len(found) > 1: status = 'ambiguous_position'
                else:
                    if not isinstance(position, (list, tuple)) or len(position) != 2 or any(type(i) is not int for i in position):
                        raise ValueError('explicit character range required')
                    start, end = position
                    if not 0 <= start <= end <= len(text): raise ValueError('quote range outside original text')
                if start is not None and text[start:end] == quote: status = 'exact_literal_match'
                elif start is not None: start = end = None
            if sync.head(db, url)['availability'] in ('deleted', 'empty_body'):
                status = 'source_unavailable'
            if status == 'exact_literal_match' and event_id is not None and sync.head(db, url)['state_id'] == state_id:
                proof = sync.add_evidence(db, event_id, url, KINDS[kind], quote)
        qid = digest({'operation': operation_id, 'binding': command})
        db.execute('INSERT INTO bound_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (qid, operation_id, key, event_id, url, state_id, vid, content_hash, kind, quote,
                    start, end, origin, response_record_id, proof, status, now.isoformat()))
        if fail_save: raise RuntimeError('simulated quote save failure')
    return qid


def inspect(db, quote_id):
    row = db.execute('SELECT event_id,url,state_id,version_id,content_hash,kind,quote,start_pos,end_pos,origin,response_record_id,sync_evidence_id,initial_status,saved_at FROM bound_quotes WHERE quote_id=?', (quote_id,)).fetchone()
    if row is None: raise ValueError('unknown quote')
    keys = ('event_id', 'url', 'state_id', 'version_id', 'content_hash', 'kind', 'quote', 'start', 'end',
            'origin', 'response_record_id', 'evidence_id', 'initial_status', 'saved_at')
    out = dict(zip(keys, row)); status = out['initial_status']
    if out['state_id']:
        _, h, text = text_for(db, out['state_id'], out['kind'])
        if h != out['content_hash']: status = 'content_hash_mismatch'
        elif out['start'] is not None and (text is None or text[out['start']:out['end']] != out['quote']): status = 'literal_mismatch'
        if sync.head(db, out['url'])['state_id'] != out['state_id']: status = 'stale_source'
    if out['event_id'] is None and status == 'exact_literal_match': status = 'event_unassigned'
    confirmations = [dict(kind=k, actor=a, checked_at=t, decision_id=d,
                          current=bool(db.execute("SELECT 1 FROM sync_decision_state WHERE decision_id=? AND status='current'", (d,)).fetchone()) and status == 'exact_literal_match')
                     for k, a, t, d in db.execute('SELECT kind,actor,checked_at,decision_id FROM quote_confirmations WHERE quote_id=? ORDER BY rowid', (quote_id,))]
    out.update(quote_id=quote_id, literal_status=status, confirmations=confirmations,
               body_confirmation='confirmed' if out['kind'] == 'body' and any(c['kind'] == 'body' and c['current'] for c in confirmations) else 'unconfirmed',
               source_confirmation='confirmed' if any(c['kind'] == 'source' and c['current'] for c in confirmations) else 'unconfirmed',
               gold_promoted=False)
    out['article_edition_id'] = None
    out['article_content_hash'] = None
    if out['state_id']:
        state = db.execute('SELECT components FROM sync_source_states WHERE state_id=?', (out['state_id'],)).fetchone()
        components = json.loads(state[0])
        out['article_edition_id'] = digest({'metadata_version': out['version_id'], 'body': components['body']})
        out['article_content_hash'] = digest(components)
    return out


def confirm(db, operation_id, quote_id, *, kind, actor, checked_at, decision_id):
    initialize(db); timestamp(checked_at, required=True)
    if kind not in ('body', 'source') or not isinstance(actor, str) or not actor.strip():
        raise ValueError('explicit confirmation type and actor required')
    command = dict(quote=quote_id, kind=kind, actor=actor, checked_at=checked_at, decision=decision_id)
    old = db.execute('SELECT command_hash FROM quote_confirmations WHERE operation_id=?', (operation_id,)).fetchone()
    if old:
        if old[0] != digest(command): raise ValueError('confirmation operation reused')
        return
    quote = inspect(db, quote_id)
    if quote['literal_status'] != 'exact_literal_match' or not quote['evidence_id']:
        raise ValueError('current event-scoped literal evidence required')
    if kind == 'body' and quote['kind'] != 'body': raise ValueError('headline is not body confirmation')
    row = db.execute('''SELECT d.event_id,d.kind,s.status FROM sync_decisions d JOIN sync_decision_state s USING(decision_id)
      JOIN sync_dependencies e USING(decision_id) WHERE d.decision_id=? AND e.evidence_id=?''', (decision_id, quote['evidence_id'])).fetchone()
    if row != (quote['event_id'], 'operator', 'current'): raise ValueError('current matching operator reference required')
    with atomic(db):
        db.execute('INSERT INTO quote_confirmations VALUES(?,?,?,?,?,?,?)',
                   (operation_id, digest(command), quote_id, kind, actor, checked_at, decision_id))


def for_event(db, event_id, url):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='bound_quotes'").fetchone(): return []
    return [inspect(db, qid) for qid, in db.execute('SELECT quote_id FROM bound_quotes WHERE url=? AND (event_id=? OR event_id IS NULL) ORDER BY rowid', (url, event_id))]
