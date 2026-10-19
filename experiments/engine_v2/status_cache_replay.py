"""Offline cache-first classification. No transport, event adoption or publisher.

Raw legacy Responses caches are deliberately NOT treated as version attestations.
Only captures with explicit request/article/context/validator bindings can run.
All payloads live in a temporary SQLite DB, never in diagnostics or stdout.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path

from ai_cost_control import json_answer
from engine import atomic, dumps, events
from failed_response import digest, event_conditions, validation_revision
from pending_triage import fingerprint, group_key
from queue_policy import already_saved_group, pause_reason, pending_articles
import queue_diagnostics as diagnostics

ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = {'事件・事故', '災害', '政策・制度', '企業・組織', '地域・インフラ', '対象外'}
TEXT_FIELDS = ('current_stage', 'summary', 'latest', 'next_watch')
TRIAGE_OBSERVATION_FIELDS = {'first_seen_at', 'last_seen_at', 'last_failure_at',
                            'last_decision_at', 'cached_rejection_waiting',
                            'cached_rejection_skips', 'last_cached_rejection_skip_at'}


def initialize(db):
    diagnostics.initialize(db)
    statements = (
        '''CREATE TABLE IF NOT EXISTS status_exact_cache(
          binding TEXT PRIMARY KEY, request_hash TEXT NOT NULL, article_hash TEXT NOT NULL,
          context_hash TEXT NOT NULL, validator_hash TEXT NOT NULL, answer_hash TEXT NOT NULL,
          payload TEXT NOT NULL, origin TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS status_cache_attempts(
          attempt TEXT PRIMARY KEY, group_key TEXT NOT NULL, outcome TEXT NOT NULL,
          cause TEXT NOT NULL, request_hash TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS status_shadow_results(
          item TEXT PRIMARY KEY, group_key TEXT NOT NULL, article_hash TEXT NOT NULL,
          context_hash TEXT NOT NULL, request_hash TEXT NOT NULL, payload TEXT NOT NULL,
          destination TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS status_scan_cursors(
          queue_hash TEXT PRIMARY KEY, position INTEGER NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS status_wait_reasons(
          item TEXT PRIMARY KEY, cause TEXT NOT NULL, basis TEXT NOT NULL,
          recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS status_cache_response_records(
          binding TEXT PRIMARY KEY, response_record_id TEXT NOT NULL)''',
    )
    with atomic(db):
        for statement in statements: db.execute(statement)


def request(group):
    """Extract just the existing prompt expression, without executing its script."""
    tree = ast.parse((ROOT / 'news_status.py').read_text())
    expression = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'prompt' for t in n.targets))
    text = ''.join(f"タイトル: {a.get('title', '')}\n公開日時: {a.get('published', '')}\n\n"
                   for a in group.get('articles', []))
    prompt = eval(compile(ast.Expression(expression), 'stored_status_prompt', 'eval'),
                  {'__builtins__': {}}, {'event_name': group.get('event_name', '名称未設定'), 'article_text': text})
    return {'model': 'gpt-5.4-mini', 'input': prompt, 'max_output_tokens': 1200}


def validator_hash():
    return digest({'existing_status_validator': validation_revision('status'),
                   'isolated_classifier': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   'completion_validation': hashlib.sha256(Path(diagnostics.__file__).read_bytes()).hexdigest(),
                   'response_provenance': hashlib.sha256(Path(__file__).with_name('response_provenance.py').read_bytes()).hexdigest()})


def binding(db, group, triage, registry):
    """Conservative: unrelated event changes may invalidate; never narrow safety."""
    validator = validator_hash()
    entry = triage.get(group_key(group), {})
    # Observation clocks/counters cannot change an evidence validation result.
    # Preserve all unknown fields, attempts, manual status and operator decisions.
    triage_context = {key: value for key, value in entry.items() if key not in TRIAGE_OBSERVATION_FIELDS}
    context = {'events': event_conditions(events(db)), 'registry': registry,
               'triage': triage_context, 'group': group,
               'validator': validator}
    return {'request_hash': digest(request(group)),
            # Full articles, including URL/body/description/publication and unknown fields.
            'article_hash': digest(group.get('articles', [])),
            'context_hash': digest(context), 'validator_hash': validator}


def capture(db, group, payload, *, triage=None, registry=None, origin='mock_response'):
    """Capture only at known input provenance. Never upgrade a raw legacy cache.

    This entry point records supplied offline fixtures. Production saved captures
    require a separately reviewed manifest importer; none is enabled here.
    """
    if origin != 'mock_response': raise ValueError('offline mock captures only')
    initialize(db)
    meta = binding(db, group, triage or {}, registry or {})
    key = digest(meta)
    with atomic(db):
        from datetime import datetime, timezone
        import response_provenance as provenance
        at = datetime.now(timezone.utc)
        record_id = None
        if pending_articles(group):
            capture_id = provenance.prepare_status(db, group, triage or {}, registry or {}, at)
            record_id = provenance.save(db, 'fixture:' + digest([meta, payload]), capture_id, payload, at)
        db.execute('INSERT OR REPLACE INTO status_exact_cache VALUES(?,?,?,?,?,?,?,?)',
                   (key, *meta.values(), digest(payload), dumps(payload), origin))
        if record_id:
            db.execute('INSERT OR REPLACE INTO status_cache_response_records VALUES(?,?)', (key, record_id))
    return key


def validate(group, payload):
    if not pending_articles(group) or not isinstance(group.get('event_name'), str) or not group['event_name'].strip():
        raise ValueError('invalid_source_group')
    if diagnostics.completion(payload) != 'completed': raise ValueError('cache_incomplete')
    answer = json_answer(payload)
    # Reuse the current inline category/value guard rather than relaxing it.
    tree = ast.parse((ROOT / 'news_status.py').read_text())
    # Select the innermost guard, not a future enclosing loop/if.
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(isinstance(z, ast.Constant) and z.value == 'invalid_category_or_tracking_value'
                      for z in ast.walk(n))]
    guard = min(guards, key=lambda n: len(list(ast.walk(n))))
    exec(compile(ast.Module(body=[guard], type_ignores=[]), 'current_status_validation', 'exec'),
         {'ai_status': answer, 'VALID_CATEGORIES': CATEGORIES, 'ValueError': ValueError})
    if any(not isinstance(answer.get(key, ''), str) for key in TEXT_FIELDS):
        raise ValueError('invalid_status_text')
    result = copy.deepcopy(group)
    result.update(category=answer['category'], tracking_value=answer['tracking_value'],
                  **{key: answer.get(key, '') for key in TEXT_FIELDS})
    return result


class MockResponses:
    """Finite saved fixtures, not an arbitrary callable or network client."""
    def __init__(self, answers):
        self.answers = copy.deepcopy(answers)
        self.requests = 0


def answer_identity(payload):
    """Request IDs/usage timestamps cannot reopen the same semantic failure."""
    try:
        answer = json_answer(payload)
    except (ValueError, TypeError, KeyError, AttributeError):
        # Hash unparseable text; never log it. Completion changes may allow retry.
        answer = ''.join(c.get('text', '') for o in payload.get('output', [])
                         for c in o.get('content', []) if c.get('type') in (None, 'output_text')) if isinstance(payload, dict) else payload
    return digest({'completion': diagnostics.completion(payload), 'answer': answer})


def response_hash(db, meta, *, mock=None, legacy=None):
    legacy = legacy or {}
    row = db.execute('SELECT payload,answer_hash FROM status_exact_cache WHERE binding=?', (digest(meta),)).fetchone()
    if row:
        payload = json.loads(row[0])
        if digest(payload) != row[1]: return digest({'bad_checksum': digest(payload)})
        return digest({'answer': answer_identity(payload), 'bound': True})
    if mock and meta['request_hash'] in mock.answers:
        return digest({'answer': answer_identity(mock.answers[meta['request_hash']]), 'bound': True})
    if meta['request_hash'] in legacy:
        return digest({'answer': answer_identity(legacy[meta['request_hash']]), 'bound': False})
    return None

def run(db, groups, now, *, triage=None, registry=None, raw_legacy_cache=None,
        cache_first=True, cache_limit=16, scan_limit=2000, processing_limit=4,
        mock=None, fail_save=False, observed_ledger=None):
    """Keep given priority order. Cache allowance is separate from mock slots.

    Without explicit mock fixtures this cannot make a fresh judgment. On a real
    snapshot it records unknown legacy provenance and leaves every source intact.
    """
    if not (type(cache_limit) is int and 0 <= cache_limit <= 16 and
            type(scan_limit) is int and 1 <= scan_limit <= 2000 and
            type(processing_limit) is int and 0 <= processing_limit <= 4):
        raise ValueError('isolated limits may not exceed existing processing bounds')
    if mock is not None and type(mock) is not MockResponses:
        raise ValueError('offline fixture transport required')
    initialize(db)
    triage, registry, legacy = triage or {}, registry or {}, raw_legacy_cache or {}
    queue = [copy.deepcopy(g) for g in groups]

    metas = [binding(db, g, triage, registry) for g in queue]
    keys = [digest({'binding': meta, 'answer_hash': response_hash(db, meta, mock=mock, legacy=legacy)}) for meta in metas]
    if len(keys) != len(set(keys)): raise ValueError('duplicate queue entry')
    metrics = dict(cache_checked=0, cache_validated=0, rejected=0, legacy_unknown=0,
                   processing_used=0, mock_requests=0, new_results=0)

    def done(key):
        return bool(db.execute('SELECT 1 FROM status_shadow_results WHERE item=?', (key,)).fetchone())

    def remember(key, group, meta, outcome, cause):
        db.execute('INSERT OR IGNORE INTO status_cache_attempts VALUES(?,?,?,?,?,?)',
                   (key, group_key(group), outcome, cause, meta['request_hash'], now.isoformat()))

    def wait(key, cause, basis):
        db.execute('INSERT OR REPLACE INTO status_wait_reasons VALUES(?,?,?,?)',
                   (key, cause, basis, now.isoformat()))

    def save_reference(key, group, meta, payload):
        classified = validate(group, payload)
        destination = 'out_of_scope' if classified['category'] == '対象外' or classified['tracking_value'] == 'low' else 'tracking'
        db.execute('INSERT INTO status_shadow_results VALUES(?,?,?,?,?,?,?,?)',
                   (key, group_key(group), meta['article_hash'], meta['context_hash'], meta['request_hash'],
                    dumps(classified), destination, now.isoformat()))
        remember(key, group, meta, 'validated_reference_only', 'unknown')
        db.execute('DELETE FROM status_wait_reasons WHERE item=?', (key,))
        if fail_save: raise RuntimeError('simulation_save_failure')

    # Cursor concerns only bounded cache scanning. Fresh mock order is unchanged.
    qhash = digest(keys)
    cursor_row = db.execute('SELECT position FROM status_scan_cursors WHERE queue_hash=?', (qhash,)).fetchone()
    start = cursor_row[0] if cursor_row else 0
    visited = 0
    if cache_first and cache_limit:
        for offset in range(min(scan_limit, len(queue))):
            i = (start + offset) % len(queue)
            visited = offset + 1
            group, key = queue[i], keys[i]
            meta = metas[i]
            if done(key) or db.execute('SELECT 1 FROM status_cache_attempts WHERE attempt=?', (key,)).fetchone(): continue
            if pause_reason(group, triage) or already_saved_group(group, events(db)): continue
            row = db.execute('''SELECT payload,answer_hash,request_hash,article_hash,
              context_hash,validator_hash,origin FROM status_exact_cache WHERE binding=?''', (digest(meta),)).fetchone()
            if row is None and meta['request_hash'] not in legacy: continue
            metrics['cache_checked'] += 1
            if row is None:
                with atomic(db):
                    remember(key, group, meta, 'cache_provenance_missing', 'unknown')
                    wait(key, 'unknown', 'legacy_provenance_missing')
                diagnostics.failure(db, 'status_cache_lookup', ValueError('cache_provenance_missing'),
                                    meta['request_hash'], legacy[meta['request_hash']], now)
                metrics['legacy_unknown'] += 1
            else:
                payload = None
                try:
                    payload = json.loads(row[0])
                    if digest(payload) != row[1]: raise ValueError('cache_payload_changed')
                    if tuple(meta.values()) != row[2:6] or row[6] not in ('mock_response', 'provenance_response'):
                        raise ValueError('cache_context_mismatch')
                    proof = db.execute('SELECT response_record_id FROM status_cache_response_records WHERE binding=?', (digest(meta),)).fetchone()
                    if not proof: raise ValueError('cache_provenance_missing')
                    import response_provenance as provenance
                    # Invalid answers are still validated below and memoized once.
                    source_record = db.execute('SELECT capture_id,payload_hash FROM response_records WHERE record_id=?', proof).fetchone()
                    if not source_record: raise ValueError('cache_provenance_missing')
                    if source_record[1] != row[1]: raise ValueError('cache_payload_changed')
                    if not provenance.current(db, source_record[0],
                        request(group), group['articles'], {'group': group, 'binding': meta}, meta['validator_hash']):
                        raise ValueError('cache_context_mismatch')
                    qualified = provenance.reusable(db, proof[0], request(group), group['articles'],
                                 {'group': group, 'binding': meta}, meta['validator_hash'])
                    if not qualified['allowed']: raise ValueError('cache_context_mismatch')
                    with atomic(db): save_reference(key, group, meta, payload)
                    metrics['cache_validated'] += 1
                    metrics['new_results'] += 1
                except RuntimeError:
                    # Save interruption must propagate; no rejection/receipt is committed.
                    raise
                except (ValueError, TypeError, KeyError) as exc:
                    with atomic(db):
                        remember(key, group, meta, 'rejected', 'unknown')
                        wait(key, 'unknown', 'current_validation_failure')
                    diagnostics.failure(db, 'status_validation', exc, meta['request_hash'], payload, now)
                    metrics['rejected'] += 1
            if metrics['cache_checked'] >= cache_limit: break
        with atomic(db):
            db.execute('INSERT OR REPLACE INTO status_scan_cursors VALUES(?,?)',
                       (qhash, (start + visited) % max(len(queue), 1)))

    for group, key, meta in zip(queue, keys, metas):
        if done(key) or pause_reason(group, triage) or already_saved_group(group, events(db)): continue
        # A current rejected answer never consumes a fresh mock/communication slot.
        previous = db.execute('SELECT outcome FROM status_cache_attempts WHERE attempt=?', (key,)).fetchone()
        if previous and previous[0] == 'rejected':
            with atomic(db): wait(key, 'rejected_unchanged', 'current_rejection_receipt')
            continue
        if metrics['processing_used'] >= processing_limit:
            with atomic(db): wait(key, 'processing_slots', 'observed_this_pass')
            continue
        if observed_ledger is not None:
            causes = diagnostics.budget_causes(observed_ledger, now,
                     input_bytes=len(request(group)['input'].encode()),
                     processing_used=metrics['processing_used'], processing_limit=processing_limit)
            if causes:
                # Multiple constraints are diagnostics; do not overwrite historical causes.
                for cause in causes:
                    diagnostics.failure(db, 'budget_gate', RuntimeError('budget_gate'),
                                        meta['request_hash'], None, now, cause)
                with atomic(db): wait(key, causes[0], 'snapshot_observation_not_history')
                continue
        if mock is None:
            # Observing an old queue is not permission to re-evaluate it.
            if previous is None:
                cause = diagnostics.legacy_cause(triage.get(group_key(group), {}).get('reason'))
                with atomic(db): wait(key, cause, 'legacy_record')
            continue
        metrics['processing_used'] += 1
        payload = mock.answers.get(meta['request_hash'])
        if payload is None: continue
        mock.requests += 1
        metrics['mock_requests'] += 1
        # Capture the finite fixture before validation/save, like staged.py.
        # A failed save can retry from this exact response without another slot.
        capture(db, group, payload, triage=triage, registry=registry)
        try:
            with atomic(db): save_reference(key, group, meta, payload)
            metrics['new_results'] += 1
        except RuntimeError: raise
        except (ValueError, TypeError, KeyError) as exc:
            with atomic(db):
                remember(key, group, meta, 'rejected', 'unknown')
                wait(key, 'unknown', 'current_validation_failure')
            diagnostics.failure(db, 'status_validation', exc, meta['request_hash'], payload, now)
            metrics['rejected'] += 1
    valid = set(keys)
    records = [(key, destination) for key, destination in db.execute('SELECT item,destination FROM status_shadow_results') if key in valid]
    metrics.update(status_remaining=len(queue) - len(records), downstream=sum(dest == 'tracking' for _, dest in records),
                   out_of_scope=sum(dest == 'out_of_scope' for _, dest in records),
                   total_waiting=len(queue) - sum(dest == 'out_of_scope' for _, dest in records),
                   cache_cursor=(start + visited) % max(len(queue), 1), can_publish=False)
    metrics['wait_counts'] = {}
    for key, cause in db.execute('SELECT item,cause FROM status_wait_reasons'):
        if key in valid: metrics['wait_counts'][cause] = metrics['wait_counts'].get(cause, 0) + 1
    return metrics
