"""Private SQLite diagnostics. Free text is NEVER a loggable error message."""
import json
import re

from engine import atomic
from ai_cost_control import periods

LABELS = {
    'amount_daily': '日額上限', 'amount_monthly': '月額上限',
    'calls_daily': '日次通信回数上限', 'calls_run': '実行単位通信回数上限',
    'processing_slots': '処理枠上限', 'evidence': '根拠不足',
    'rejected_unchanged': '同一不成立回答の再判定抑止',
    'unknown': '原因未確認',
}
LEGACY = {
    'daily_budget_deferred': 'amount_daily', 'monthly_budget_deferred': 'amount_monthly',
    'article_identity_unproved': 'evidence', 'event_name_unproved': 'evidence',
    'uncertain_or_invalid_evidence': 'evidence', 'progress_quote_missing': 'evidence',
    'stage_not_supported_by_quote': 'evidence', 'uncertain_relation': 'evidence',
    'schedule_identity_uncertain': 'evidence', 'search_identity_uncertain': 'evidence',
    'same_rejected_answer': 'rejected_unchanged',
}
STAGES = {'budget_gate', 'processing_gate', 'status_cache_lookup', 'status_validation',
          'schedule_selection', 'schedule_analysis', 'schedule_validation', 'persistence'}
ERRORS = {'incomplete_response', 'JSON object required', 'matches must be a list',
          'invalid_category_or_tracking_value', 'invalid_status_text',
          'cache_provenance_missing', 'cache_context_mismatch', 'cache_payload_changed',
          'cache_incomplete', 'saved_mock_missing', 'simulation_save_failure', 'budget_gate',
          'invalid_source_group'}
EXCEPTIONS = {'ValueError', 'TypeError', 'KeyError', 'JSONDecodeError', 'BudgetExceeded',
              'UpdateHeld', 'RuntimeError', 'OSError', 'TimeoutError'}
COMPLETION = {'completed', 'incomplete', 'failed', 'cancelled', 'in_progress', 'unknown'}


def initialize(db):
    with atomic(db):
        db.execute('''CREATE TABLE IF NOT EXISTS queue_diagnostics(
          id INTEGER PRIMARY KEY, stage TEXT NOT NULL, exception_type TEXT,
          safe_error TEXT NOT NULL, request_hash TEXT, completion TEXT NOT NULL,
          cause TEXT NOT NULL, recorded_at TEXT NOT NULL)''')


def legacy_cause(reason):
    # budget_deferred/call_budget_deferred/stage_budget_deferred are ambiguous.
    return LEGACY.get(str(reason).split(':', 1)[0], 'unknown')


def completion(payload):
    value = payload.get('status') if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value in COMPLETION else 'unknown'


def failure(db, stage, exc, request_hash, payload, now, cause='unknown'):
    """Allowlist, not regex-based PII guessing; never persist str(exc) verbatim."""
    initialize(db)
    name = type(exc).__name__
    message = str(exc)
    safe = message if message in ERRORS else (
        'invalid_json' if name == 'JSONDecodeError' else 'redacted_unclassified_error')
    key = request_hash if isinstance(request_hash, str) and re.fullmatch('[0-9a-f]{64}', request_hash) else None
    row = dict(stage=stage if stage in STAGES else 'unknown',
               exception_type=name if name in EXCEPTIONS else 'Exception',
               safe_error=safe, request_hash=key, completion=completion(payload),
               cause=cause if cause in LABELS else 'unknown', recorded_at=now.isoformat())
    with atomic(db):
        db.execute('''INSERT INTO queue_diagnostics(stage,exception_type,safe_error,
          request_hash,completion,cause,recorded_at) VALUES(?,?,?,?,?,?,?)''', tuple(row.values()))
    return row


def budget_causes(ledger, now, *, input_bytes, processing_used=0, processing_limit=4):
    """Snapshot observations, NOT historical attribution or a budget reservation.

    Constants match the existing workflow/model. No ledger or env writes. Multiple
    simultaneous limits are retained rather than inventing one primary cause.
    """
    day, month = periods(now)
    daily = ledger.get('days', {}).get(day, {})
    monthly = ledger.get('months', {}).get(month, {})
    run = ledger.get('run', {})
    amount = ((input_bytes + 2048) * 3 + 3) // 4 + 1200 * 4500 // 1000
    causes = []
    if daily.get('reserved_micro_usd', 0) + amount > 200000: causes.append('amount_daily')
    if monthly.get('reserved_micro_usd', 0) + amount > 5000000: causes.append('amount_monthly')
    if daily.get('calls', 0) >= 40: causes.append('calls_daily')
    if run.get('calls', 0) >= 24: causes.append('calls_run')
    if processing_used >= processing_limit: causes.append('processing_slots')
    return causes


def schedule_replay(db, event, selected_articles, selection_answer, analysis_payload, now):
    """Offline recorded/mock answer probe through the EXISTING lookup guards.

    No finder/client injection is accepted. This never adopts the returned event.
    Missing selection/payload stays unknown. Original pending data stays intact.
    """
    import copy
    from event_tracker import lookup_event
    from ai_cost_control import json_answer
    from failed_response import digest
    current = {'stage': 'schedule_selection', 'hash': None, 'payload': None, 'calls': 0}

    class SavedOnly:
        def ask(self, prompt):
            current['calls'] += 1
            current['hash'] = digest({'model': 'gpt-5.4-mini', 'input': prompt, 'max_output_tokens': 1200})
            if current['calls'] == 1:
                if not isinstance(selection_answer, dict): raise ValueError('saved_mock_missing')
                return copy.deepcopy(selection_answer)
            current['stage'] = 'schedule_analysis'
            current['payload'] = analysis_payload
            if not isinstance(analysis_payload, dict): raise ValueError('saved_mock_missing')
            if completion(analysis_payload) == 'incomplete': raise ValueError('incomplete_response')
            if completion(analysis_payload) != 'completed': raise ValueError('cache_incomplete')
            answer = json_answer(analysis_payload)
            current['stage'] = 'schedule_validation'
            return answer

    candidate = copy.deepcopy(event)
    try:
        lookup_event(candidate, SavedOnly(), 'offline stored candidates', now,
                     finder=lambda _: copy.deepcopy(selected_articles))
    except Exception as exc:
        return failure(db, current['stage'], exc, current['hash'], current['payload'], now,
                       cause=legacy_cause(str(exc)))
    return {'status': 'validated_reference_only', 'can_publish': False}
