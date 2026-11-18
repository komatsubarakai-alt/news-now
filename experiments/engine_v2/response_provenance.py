"""Private, offline capture sidecar. Input bindings precede the response.

No network, legacy provenance upgrade, event adoption or production writer.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from engine import atomic, dumps
from failed_response import digest
from article_store import initialize as article_initialize, save_version, version_id, timestamp
from queue_diagnostics import completion


def initialize(db):
    article_initialize(db)
    with atomic(db):
        db.execute('''CREATE TABLE IF NOT EXISTS response_inputs(
          capture_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, stage TEXT NOT NULL,
          context_hash TEXT NOT NULL, validator_version TEXT NOT NULL,
          sources TEXT NOT NULL, event_ids TEXT NOT NULL, prepared_at TEXT NOT NULL,
          legacy_job TEXT, private_context TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS response_records(
          record_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
          command_hash TEXT NOT NULL, capture_id TEXT REFERENCES response_inputs,
          response_external_id TEXT, saved_at TEXT NOT NULL, completion TEXT NOT NULL,
          format_result TEXT NOT NULL, reuse_reason TEXT NOT NULL,
          payload_hash TEXT NOT NULL, payload TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS response_reuse_checks(
          check_id TEXT PRIMARY KEY, record_id TEXT REFERENCES response_records,
          checked_at TEXT NOT NULL, allowed INTEGER NOT NULL, reason TEXT NOT NULL,
          conditions_hash TEXT NOT NULL, validator_version TEXT NOT NULL)''')


def code_version():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def sources(db, articles):
    result = []
    for article in articles:
        vid = version_id(article)
        # This is an explicit supplied input snapshot, not an inferred live head.
        save_version(db, article, 'response_input', digest(article))
        row = db.execute("SELECT 1 FROM sqlite_master WHERE name='sync_heads'").fetchone()
        state = None
        if row:
            from revision_sync import head
            if db.execute('SELECT 1 FROM sync_heads WHERE url=?', (article['url'],)).fetchone():
                state = head(db, article['url'])
                if state['version_id'] != vid: raise ValueError('input differs from selected article version')
        result.append(dict(url=article['url'], version_id=vid, content_hash=digest(article),
                           article_edition_id=digest({'metadata_version': vid, 'body': state['components']['body'] if state else None}),
                           state=state, input_snapshot=copy.deepcopy(article)))
    return result


def prepare(db, request, articles, event_ids, conditions, validator_version, now, *, stage='status', legacy_job=None):
    initialize(db); timestamp(now.isoformat(), required=True)
    if stage not in ('status', 'tracking') or not articles or not validator_version:
        raise ValueError('complete capture input required')
    if not isinstance(request, dict) or request.get('model') != 'gpt-5.4-mini' or request.get('max_output_tokens') != 1200 or not isinstance(request.get('input'), str) or not request['input']:
        raise ValueError('complete unchanged-model request required')
    from queue_policy import pending_articles
    if not pending_articles({'articles': articles}): raise ValueError('valid capture articles required')
    ids = sorted(set(event_ids))
    if any(not db.execute('SELECT 1 FROM events WHERE event_id=?', (eid,)).fetchone() for eid in ids):
        raise ValueError('unknown capture target')
    with atomic(db):
        bound = sources(db, articles)
        context = {'conditions': conditions, 'sidecar_code': code_version()}
        value = dict(request_hash=digest(request), stage=stage, context_hash=digest(context),
                     validator_version=validator_version, sources=bound, event_ids=ids,
                     legacy_job=legacy_job, private_context=context)
        capture_id = digest(value)
        db.execute('INSERT OR IGNORE INTO response_inputs VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (capture_id, value['request_hash'], stage, value['context_hash'], validator_version,
                    dumps(bound), dumps(ids), now.isoformat(), legacy_job, dumps(context)))
    return capture_id


def input_record(db, capture_id):
    row = db.execute('SELECT request_hash,stage,context_hash,validator_version,sources,event_ids,prepared_at,legacy_job,private_context FROM response_inputs WHERE capture_id=?', (capture_id,)).fetchone()
    if not row: raise ValueError('unknown capture')
    keys = ('request_hash', 'stage', 'context_hash', 'validator_version', 'sources', 'event_ids', 'prepared_at', 'legacy_job', 'private_context')
    result = dict(zip(keys, row))
    for field in ('sources', 'event_ids', 'private_context'): result[field] = json.loads(result[field])
    identity = {key: result[key] for key in ('request_hash', 'stage', 'context_hash', 'validator_version',
                'sources', 'event_ids', 'legacy_job', 'private_context')}
    if digest(identity) != capture_id: raise ValueError('capture metadata checksum mismatch')
    return result


def current(db, capture_id, request, articles, conditions, validator_version):
    try: original = input_record(db, capture_id)
    except ValueError: return False
    if original['request_hash'] != digest(request) or original['validator_version'] != validator_version:
        return False
    if original['context_hash'] != digest({'conditions': conditions, 'sidecar_code': code_version()}):
        return False
    if len(articles) != len(original['sources']): return False
    for article, source in zip(articles, original['sources']):
        if digest(article) != source['content_hash'] or version_id(article) != source['version_id']:
            return False
        if source['state'] is not None:
            from revision_sync import head
            if head(db, article['url']) != source['state']: return False
            if source['state']['availability'] in ('deleted', 'empty_body'): return False
        elif db.execute("SELECT 1 FROM sqlite_master WHERE name='sync_heads'").fetchone() and db.execute('SELECT 1 FROM sync_heads WHERE url=?', (article['url'],)).fetchone():
            # Selecting a head after capture changes the input binding; never infer it.
            return False
    return True


def format_result(stage, group, payload, event_ids):
    if completion(payload) != 'completed': return 'unvalidated_completion'
    try:
        from ai_cost_control import json_answer
        answer = json_answer(payload)
        if stage == 'status':
            from status_cache_replay import validate
            validate(group, payload)
        else:
            from staged import valid_answer
            if not valid_answer(answer, event_ids): return 'invalid_format'
    except (ValueError, TypeError, KeyError, AttributeError):
        return 'invalid_format'
    return 'valid_format'


def save(db, operation_id, capture_id, payload, now, *, fail_save=False):
    """Atomic append, even incomplete/invalid replies. No backdated timestamps."""
    initialize(db); timestamp(now.isoformat(), required=True)
    if not isinstance(operation_id, str) or not operation_id or not isinstance(payload, dict):
        raise ValueError('response operation and payload required')
    command_hash = digest({'capture': capture_id, 'payload': payload})
    old = db.execute('SELECT command_hash,record_id FROM response_records WHERE operation_id=?', (operation_id,)).fetchone()
    if old:
        if old[0] != command_hash: raise ValueError('response operation reused for different input')
        return old[1]
    legacy = db.execute("SELECT payload FROM source_files WHERE path='ai_response_cache.json'").fetchone()
    if legacy:
        entries = json.loads(legacy[0]).values()
        external_id = payload.get('id')
        if any(digest(value) == digest(payload) or
               (external_id is not None and value.get('id') == external_id) for value in entries if isinstance(value, dict)):
            raise ValueError('legacy response has no original capture provenance')
    source = input_record(db, capture_id)
    if now < datetime.fromisoformat(source['prepared_at']): raise ValueError('response precedes prepared input')
    group = source['private_context']['conditions'].get('group', {})
    result = format_result(source['stage'], group, payload, source['event_ids'])
    reason = 'requires_current_validation' if result == 'valid_format' else result
    record_id = digest({'operation': operation_id, 'capture': capture_id, 'payload_hash': digest(payload)})
    external = payload.get('id') if isinstance(payload.get('id'), str) else None
    with atomic(db):
        db.execute('INSERT INTO response_records VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (record_id, operation_id, command_hash, capture_id, external, now.isoformat(),
                    completion(payload), result, reason, digest(payload), dumps(payload)))
        if fail_save: raise RuntimeError('simulated provenance save failure')
    return record_id


def _evaluate_reuse(db, record_id, request, articles, conditions, validator_version):
    row = db.execute('SELECT capture_id,payload_hash,payload,operation_id,command_hash FROM response_records WHERE record_id=?', (record_id,)).fetchone()
    if not row: return {'allowed': False, 'reason': 'provenance_missing'}
    payload = json.loads(row[2])
    if digest(payload) != row[1]: return {'allowed': False, 'reason': 'payload_changed'}
    if digest({'capture': row[0], 'payload': payload}) != row[4] or digest({'operation': row[3], 'capture': row[0], 'payload_hash': row[1]}) != record_id:
        return {'allowed': False, 'reason': 'response_binding_changed'}
    if not current(db, row[0], request, articles, conditions, validator_version):
        return {'allowed': False, 'reason': 'input_or_conditions_changed'}
    source = input_record(db, row[0])
    result = format_result(source['stage'], conditions.get('group', {}), payload, source['event_ids'])
    if result == 'valid_format' and source['stage'] == 'tracking':
        from revision_sync import job_current
        from engine import judge
        from ai_cost_control import json_answer
        from datetime import datetime
        if not source['legacy_job'] or not job_current(db, source['legacy_job']):
            return {'allowed': False, 'reason': 'tracking_context_changed'}
        # Reuse the existing source/contradiction/before-save checks, always rollback.
        db.execute('SAVEPOINT provenance_guard')
        try:
            if conditions.get('content_revision') is not None:
                from content_revision import validate
                outcome, _ = validate(db, conditions, json_answer(payload), datetime.fromisoformat(source['prepared_at']))
            else:
                outcome = judge(db, conditions['group'], json_answer(payload), datetime.fromisoformat(source['prepared_at']),
                                triage=conditions['triage'], registry=conditions['registry'],
                                presented_ids=conditions['presented_ids'], rerank=False)
        finally:
            db.execute('ROLLBACK TO provenance_guard'); db.execute('RELEASE provenance_guard')
        if outcome != 'accepted': return {'allowed': False, 'reason': 'tracking_guard_not_passed'}
    return {'allowed': result == 'valid_format', 'reason': 'exact_current_format_valid' if result == 'valid_format' else result}


def reusable(db, record_id, request, articles, conditions, validator_version):
    result = _evaluate_reuse(db, record_id, request, articles, conditions, validator_version)
    if db.execute('SELECT 1 FROM response_records WHERE record_id=?', (record_id,)).fetchone():
        check = digest({'record': record_id, 'request': request, 'articles': articles,
                        'conditions': conditions, 'validator': validator_version, 'result': result})
        with atomic(db):
            db.execute('INSERT OR IGNORE INTO response_reuse_checks VALUES(?,?,?,?,?,?,?)',
                       (check, record_id, datetime.now(timezone.utc).isoformat(), int(result['allowed']),
                        result['reason'], digest(conditions), validator_version))
    return result


def prepare_status(db, group, triage, registry, now):
    import status_cache_replay as cache
    meta = cache.binding(db, group, triage, registry)
    return prepare(db, cache.request(group), group['articles'], [],
                   {'group': group, 'binding': meta}, meta['validator_hash'], now)


def install_status(db, record_id, group, triage, registry):
    """An explicit newly captured response; never an importer for raw legacy JSON."""
    import status_cache_replay as cache
    cache.initialize(db)
    meta = cache.binding(db, group, triage, registry)
    result = reusable(db, record_id, cache.request(group), group['articles'],
                      {'group': group, 'binding': meta}, meta['validator_hash'])
    if not result['allowed']: return result
    payload = db.execute('SELECT payload,payload_hash FROM response_records WHERE record_id=?', (record_id,)).fetchone()
    with atomic(db):
        db.execute('INSERT OR REPLACE INTO status_exact_cache VALUES(?,?,?,?,?,?,?,?)',
                   (digest(meta), *meta.values(), payload[1], payload[0], 'provenance_response'))
        db.execute('INSERT OR REPLACE INTO status_cache_response_records VALUES(?,?)', (digest(meta), record_id))
    return result


def prepare_job(db, jid, now):
    row = db.execute('SELECT event_id,context,prompt FROM sync_jobs WHERE job_id=?', (jid,)).fetchone()
    context = json.loads(row[1])
    if not context['group'].get('articles'): return None
    return prepare(db, {'model': 'gpt-5.4-mini', 'input': row[2], 'max_output_tokens': 1200},
                   context['group']['articles'], context['presented_ids'], context,
                   context['guard']['validator'], now, stage='tracking', legacy_job=jid)


def save_job_response(db, jid, answer, now):
    row = db.execute('SELECT capture_id FROM response_inputs WHERE legacy_job=? ORDER BY rowid LIMIT 1', (jid,)).fetchone()
    if row is None: return None  # An old job is not permission to invent provenance.
    # call_mock supplies a complete Python object, not an actual API envelope.
    payload = {'status': 'completed', 'output': [{'content': [{'type': 'output_text', 'text': dumps(answer)}]}]}
    return save(db, 'sync-response:' + jid + ':' + digest(answer), row[0], payload, now)
