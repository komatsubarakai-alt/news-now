"""Remember rejected cached answers. Never approve an answer or send a request."""
import hashlib
import json
import os
from pathlib import Path

from ai_cost_control import MODEL, BudgetExceeded, json_answer, read


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cached_answer(prompt, model=MODEL):
    output_limit = int(os.environ.get('AI_MAX_OUTPUT_TOKENS', '1200'))
    if not 1 <= output_limit <= 3500:
        raise BudgetExceeded('invalid_output_limit')
    request_key = digest({'model': model, 'input': prompt, 'max_output_tokens': output_limit})
    cache = read('ai_response_cache.json', {})
    if request_key not in cache:
        return None
    answer = json_answer(cache[request_key])
    return {'request_key': request_key, 'answer_hash': digest(answer), 'answer': answer}


def validation_revision(stage):
    common = ('failed_response.py', 'ai_cost_control.py', 'merge_safety.py', 'tracking_v1.py',
              'publication_safety.py', 'pending_triage.py', 'queue_policy.py')
    paths = {'tracking': ('event_tracker.py', 'tracking_sources.py', 'operator_review.py'),
             'status': ('news_status.py',)}[stage]
    root = Path(__file__).parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in common + paths})


def rejection_signature(snapshot, stage, conditions, answer=None):
    if snapshot is None or (answer is not None and digest(answer) != snapshot['answer_hash']):
        return None
    signature = {'request_key': snapshot['request_key'], 'answer_hash': snapshot['answer_hash'],
                 'conditions_hash': digest(conditions), 'validation_revision': validation_revision(stage)}
    return {**signature, 'key': digest(signature)}


def safe_signature(peek, prompt, stage, conditions, answer):
    # Optional bookkeeping must never replace an existing validation failure.
    try:
        return rejection_signature(peek(prompt), stage, conditions, answer=answer)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
        return None


def already_rejected(record, signature):
    if signature is None:
        return False
    memo = record.get('rejected_responses', {})
    item = memo.get(signature['key'], {}) if isinstance(memo, dict) else {}
    return isinstance(item, dict) and item.get('signature') == signature


def remember_rejection(record, signature, reason, now):
    if signature is not None and not already_rejected(record, signature):
        record.setdefault('rejected_responses', {})[signature['key']] = {
            'signature': signature, 'reason': reason, 'rejected_at': now.isoformat()}


def note_repeat(record, now):
    record['cached_rejection_waiting'] = True
    record['cached_rejection_skips'] = record.get('cached_rejection_skips', 0) + 1
    record['last_cached_rejection_skip_at'] = now.isoformat()


def source_revision(record, source_fingerprint):
    # Reopening a changed revision must not match an earlier observation cycle.
    return len(record.get('source_history', [])) + int(
        record.get('source_fingerprint') not in (None, source_fingerprint))


def event_conditions(events):
    # These bookkeeping fields cannot make an identity/progress rejection valid.
    operational = {'backfill', 'search_query', 'last_schedule_search_at', 'v1_enriched'}
    return [{k: v for k, v in event.items() if k not in operational} for event in events]
