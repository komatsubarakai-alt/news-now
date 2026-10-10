"""Persistent, shared API budget and exact-content response cache.

Reserve a conservative maximum BEFORE every network call. Never release a
reservation on failure/unknown usage. Files are private operational metadata.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from contextlib import contextmanager
import fcntl
import urllib.request
import urllib.error

JST = timezone(timedelta(hours=9))
MODEL = 'gpt-5.4-mini'
# micro USD per token, official standard text pricing (2026-10-10).
INPUT_RATE = Decimal('0.75')
OUTPUT_RATE = Decimal('4.50')

class BudgetExceeded(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc)


def atomic(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def read(path, default):
    path = Path(path)
    # Malformed accounting must fail closed, never silently reset spend.
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


@contextmanager
def ledger():
    with open('ai_budget.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read('ai_budget.json', {'schema_version': 1, 'days': {}, 'months': {}})
        yield state
        atomic('ai_budget.json', state)


def usd_limit(name, default):
    value = Decimal(os.environ.get(name, default))
    if not value.is_finite() or value < 0:
        raise BudgetExceeded('invalid_budget_configuration')
    return int(value * 1_000_000)


def periods(at):
    date = at.astimezone(JST).date().isoformat()
    return date, date[:7]


def reserve(prompt, model, output_limit):
    if model != MODEL:
        raise BudgetExceeded('unpriced_model: ' + model)
    # Tokens cannot exceed UTF-8 bytes; allowance covers API framing.
    upper_input = len(prompt.encode('utf-8')) + 2048
    amount = int((upper_input * INPUT_RATE + output_limit * OUTPUT_RATE).to_integral_value(rounding='ROUND_CEILING'))
    date, month = periods(now())
    with ledger() as state:
        day = state['days'].setdefault(date, {'reserved_micro_usd': 0, 'calls': 0})
        monthly = state['months'].setdefault(month, {'reserved_micro_usd': 0, 'calls': 0})
        if state.get('billing_blocked'):
            raise BudgetExceeded('billing_blocked: replenish credit and clear ai_budget.json billing_blocked')
        run = state.setdefault('run', {'id': os.environ.get('GITHUB_RUN_ID', 'local'), 'calls': 0})
        if run['id'] != os.environ.get('GITHUB_RUN_ID', 'local'):
            run.clear()
            run.update(id=os.environ.get('GITHUB_RUN_ID', 'local'), calls=0)
        if run.get('rate_blocked'):
            raise BudgetExceeded('rate_limit_deferred')
        if day['calls'] >= int(os.environ.get('AI_DAILY_CALL_LIMIT', '40')) or run['calls'] >= int(os.environ.get('AI_RUN_CALL_LIMIT', '24')):
            raise BudgetExceeded('call_budget_deferred')
        if day['reserved_micro_usd'] + amount > usd_limit('AI_DAILY_USD_LIMIT', '0.20'):
            raise BudgetExceeded('daily_budget_deferred')
        if monthly['reserved_micro_usd'] + amount > usd_limit('AI_MONTHLY_USD_LIMIT', '5.00'):
            raise BudgetExceeded('monthly_budget_deferred')
        for record in (day, monthly):
            record['reserved_micro_usd'] += amount
            record['calls'] += 1
        run['calls'] += 1
        state['last_request_at'] = now().isoformat()
    return date, month, amount


def settle(reservation, payload):
    usage = payload.get('usage', {})
    inp, out = usage.get('input_tokens'), usage.get('output_tokens')
    if not isinstance(inp, int) or not isinstance(out, int) or inp < 0 or out < 0:
        return  # unknown usage: keep full reserved cost
    actual = int((inp * INPUT_RATE + out * OUTPUT_RATE).to_integral_value(rounding='ROUND_CEILING'))
    date, month, reserved = reservation
    with ledger() as state:
        for record in (state['days'][date], state['months'][month]):
            record['reserved_micro_usd'] += actual - reserved
        if actual > reserved:
            state['billing_blocked'] = {'code': 'reservation_overrun', 'at': now().isoformat()}


def json_answer(payload):
    text = ''.join(c.get('text', '') for o in payload.get('output', [])
                   for c in o.get('content', []) if c.get('type') in (None, 'output_text'))
    if text.strip().startswith('```'):
        text = text.strip().split('\n', 1)[1].rsplit('```', 1)[0]
    try:
        answer = json.loads(text)
    except (ValueError, TypeError) as exc:
        exc.ai_response_excerpt = text[:8000]
        raise
    if not isinstance(answer, dict):
        raise ValueError('JSON object required')
    return answer


def cached(prompt, model=MODEL):
    body = {'model': model, 'input': prompt,
            'max_output_tokens': int(os.environ.get('AI_MAX_OUTPUT_TOKENS', '1200'))}
    key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return key in read('ai_response_cache.json', {})


def response(prompt, model=MODEL, allow_network=True):
    output_limit = int(os.environ.get('AI_MAX_OUTPUT_TOKENS', '1200'))
    if not 1 <= output_limit <= 3500:
        raise BudgetExceeded('invalid_output_limit')
    body = {'model': model, 'input': prompt, 'max_output_tokens': output_limit}
    key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    cache = read('ai_response_cache.json', {})
    if key in cache:
        return copy.deepcopy(cache[key])
    if not allow_network:
        raise BudgetExceeded('stage_budget_deferred')
    reservation = reserve(prompt, model, output_limit)
    request = urllib.request.Request('https://api.openai.com/v1/responses',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as stream:
            payload = json.load(stream)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(8192).decode()).get('error', {})
        except (ValueError, UnicodeError):
            error = {}
        code = str(error.get('code') or error.get('type') or 'http_' + str(exc.code))
        exc.api_error_code = code
        if code in {'insufficient_quota', 'credit_balance_exhausted', 'organization_spend_limit_exceeded',
                    'project_spend_limit_exceeded', 'organization_usage_limit_exceeded'}:
            with ledger() as state:
                state['billing_blocked'] = {'code': code, 'at': now().isoformat()}
        if exc.code == 429:
            with ledger() as state:
                state['run']['rate_blocked'] = True
        print('API request failed:', exc.code, code)
        raise
    settle(reservation, payload)
    if payload.get('status') == 'incomplete':
        raise ValueError('incomplete_response')
    json_answer(payload)  # cache only complete, valid JSON objects
    cache[key] = payload
    # Bounded on-disk storage; old decisions may be recomputed after eviction.
    cache = dict(list(cache.items())[-2000:])
    atomic('ai_response_cache.json', cache)
    return payload


def should_run():
    """Claim one AI run every six hours, independently of hourly RSS collection."""
    with ledger() as state:
        at = now()
        if os.environ.get('AI_RESUME_AFTER_TOPUP') == 'true':
            state.pop('billing_blocked', None)
            state.pop('last_ai_run_at', None)
        last = state.get('last_ai_run_at')
        if state.get('billing_blocked') or (last and at - datetime.fromisoformat(last) < timedelta(hours=6)):
            return False
        state['last_ai_run_at'] = at.isoformat()
        return True


if __name__ == '__main__':
    enabled = should_run()
    print('AI processing:', 'enabled' if enabled else 'deferred')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write('enabled=' + str(enabled).lower() + '\n')
