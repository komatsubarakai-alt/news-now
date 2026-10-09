"""Queue bookkeeping only: never relax identity guards or delete source groups."""
import copy
import hashlib
import json
from collections import Counter
from tracking_v1 import CATEGORIES

ARCHIVED = {'already_saved', 'out_of_scope', 'low_priority', 'editorial_hold',
            'manual_review', 'invalid_source'}


def group_key(group):
    urls = sorted({a.get('url', '') for a in group.get('articles', [])})
    value = urls if urls else group
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def fingerprint(group):
    sources = [{k: a.get(k, '') for k in ('url', 'title', 'description', 'published')}
               for a in group.get('articles', [])]
    sources.sort(key=lambda a: a['url'])
    return hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def current_groups(pending, incoming):
    """Preserve queue order while using the latest observed source content."""
    unique, positions = [], {}
    for group in pending + incoming:
        key = group_key(group)
        if key in positions:
            unique[positions[key]] = group
        else:
            positions[key] = len(unique)
            unique.append(group)
    return unique


def observe(state, group, status, reason, now, decision=False):
    key = group_key(group)
    item = state.setdefault(key, {'first_seen_at': now.isoformat(), 'decision_attempts': 0})
    if item.get('source_fingerprint') not in (None, fingerprint(group)):
        item.setdefault('source_history', []).append({
            'group': copy.deepcopy(item['group']), 'status': item['status'],
            'reason': item['reason'], 'last_seen_at': item['last_seen_at']})
        item['decision_attempts'] = 0
    item.update(group=copy.deepcopy(group), status=status, reason=reason,
                last_seen_at=now.isoformat(), source_fingerprint=fingerprint(group))
    if status in {'needs_evidence', 'retry_api', 'retry_format', 'retry_system'}:
        item.update(last_failure_reason=reason, last_failure_at=now.isoformat())
    if decision:
        item['decision_attempts'] += 1
        item['last_decision_at'] = now.isoformat()
        if item['decision_attempts'] >= 3:
            item['status'] = 'manual_review'
    return item


def preflight(group, known, registry, state):
    from publication_safety import excluded
    from tracking_v1 import safe_url
    sources = group.get('articles', [])
    if not sources or any(not safe_url(a.get('url')) or not a.get('title') for a in sources):
        return 'invalid_source'
    if all(a['url'] in known for a in sources):
        return 'already_saved'
    if any(excluded(a, registry) for a in sources if a['url'] not in known):
        return 'editorial_hold'
    if group.get('category') not in CATEGORIES:
        return 'out_of_scope'
    if group.get('tracking_value') == 'low':
        return 'low_priority'
    prior = state.get(group_key(group), {})
    if prior.get('status') == 'manual_review' and prior.get('source_fingerprint') == fingerprint(group):
        return 'manual_review'
    return None


def failure_status(reason):
    reason = reason.split(':', 1)[0]
    if reason == 'editorial_article_hold':
        return 'editorial_hold'
    if reason in {'HTTPError', 'URLError', 'TimeoutError', 'ConnectionError', 'BudgetExceeded'}:
        return 'retry_api'
    if reason in {'JSONDecodeError', 'ValueError'}:
        return 'retry_format'
    if reason in {'article_identity_unproved', 'event_name_unproved',
                  'uncertain_or_invalid_evidence', 'uncertain_relation',
                  'identity_confidence', 'progress_quote_missing', 'stage_not_supported_by_quote'}:
        return 'needs_evidence'
    return 'retry_system'


def summary(state, pending):
    active_keys = {group_key(g) for g in pending}
    counts = Counter(state[k]['status'] for k in active_keys if k in state)
    return {'queue_counts': dict(counts), 'queue_count': len(pending),
            'retained_group_count': len(state),
            'retained_counts': dict(Counter(x['status'] for x in state.values()))}
