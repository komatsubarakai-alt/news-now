"""Hourly, API-free queue health, including periods when AI is paused."""
import os
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from queue_policy import read, article_fingerprint, pause_reason
from pending_triage import group_key, preflight
from publication_safety import load_corrections
from tracking_v1 import atomic_save, date_time
from ai_cost_control import periods


def build_health(now):
    collection = read('collection_triage.json', {'items': {}})['items']
    status_state = read('status_triage.json', {'items': {}})['items']
    tracking = read('tracking_triage.json', {'items': {}})['items']
    events = read('events.json', [])
    known = {a['url'] for e in events for a in e.get('articles', [])}
    registry = load_corrections()
    inbox = [a for a in read('news_inbox.json', [])
             if collection.get(a.get('url'), {}).get('status', 'queued') == 'queued']
    statuses = [g for g in read('status_pending.json', []) if not pause_reason(g, status_state)]
    incoming = [g for g in read('tracking_pending.json', []) if not preflight(g, known, registry, tracking)]
    stages = {'collection': [{'articles': [a]} for a in inbox],
              'status_generation': statuses, 'tracking': incoming}
    memory = read('queue_monitor_state.json', {'first_seen': {}, 'history': []})
    seen = memory['first_seen']
    stamps, all_sources, details = {}, {}, {}
    for name, groups in stages.items():
        sources = {}
        for group in groups:
            mark = group_key(group)
            entry = status_state.get(mark, {}) if name == 'status_generation' else tracking.get(mark, {}) if name == 'tracking' else {}
            for article in group.get('articles', []):
                key = article_fingerprint(article)
                firsts = [date_time(seen.get(key)), date_time(entry.get('first_seen_at')),
                          date_time(collection.get(article.get('url'), {}).get('first_seen_at'))]
                first = min([d for d in firsts if d] or [now])
                seen[key] = first.isoformat()
                stamps[key] = first
                sources[key] = article
                all_sources[key] = article
        details[name] = {'group_count': len(groups), 'article_count': len(sources),
                        'oldest_wait_hours': round(max([(now - stamps[k]).total_seconds() / 3600 for k in sources] or [0]), 2)}
    total = len(all_sources)
    oldest = min(stamps.values(), default=None)
    age = round(max(0, (now - oldest).total_seconds() / 3600), 2) if oldest else 0
    budget = read('ai_budget.json', {})
    day, month = periods(now)
    daily = budget.get('days', {}).get(day, {})
    monthly = budget.get('months', {}).get(month, {})
    reason = budget.get('billing_blocked', {}).get('code')
    alerts = []
    if total >= 100:
        alerts.append('waiting_article_count_at_least_100')
    if age >= 48:
        alerts.append('oldest_wait_at_least_48_hours')
    history = memory['history']
    if history and history[-1]['at'] == now.isoformat():
        history.pop()
    history.append({'at': now.isoformat(), 'waiting_article_count': total})
    history = history[-24:]
    if len(history) >= 4 and all(history[i]['waiting_article_count'] > history[i - 1]['waiting_article_count'] for i in range(len(history)-3, len(history))):
        alerts.append('waiting_increased_three_observations')
    manual = {'status_generation': sum(e.get('status') == 'manual_review' for e in status_state.values()),
              'tracking': sum(e.get('status') == 'manual_review' for e in tracking.values())}
    health = {'schema_version': 1, 'updated_at': now.isoformat(), 'ai_pause_reason': reason,
              'waiting_article_count': total, 'oldest_wait_at': oldest.isoformat() if oldest else None,
              'oldest_wait_hours': age, 'stages': details, 'manual_review_counts': manual,
              'collection_retained_counts': dict(Counter(e['status'] for e in collection.values())),
              'daily_accounted_usd': daily.get('reserved_micro_usd', 0) / 1_000_000,
              'monthly_accounted_usd': monthly.get('reserved_micro_usd', 0) / 1_000_000,
              'daily_network_calls': daily.get('calls', 0), 'alerts': alerts,
              'note': 'Unique article revisions across active stages; archive/manual review excluded. Spend covers this repository after cost-control deployment.'}
    atomic_save('queue_monitor_state.json', {'first_seen': seen, 'history': history})
    return health


if __name__ == '__main__':
    health = build_health(datetime.now(timezone.utc))
    atomic_save('queue_health.json', health)
    print('処理待ち:', health['waiting_article_count'], '記事 / 最長:', health['oldest_wait_hours'], '時間 / 要確認:', health['alerts'])

    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a', encoding='utf-8') as f:
            f.write(f"## 処理待ちの監視\n\n処理待ち: **{health['waiting_article_count']}記事** / 最長待ち: **{health['oldest_wait_hours']}時間**\n\n")
            f.write(f"API停止理由: `{health['ai_pause_reason'] or 'なし'}` / 本日の外部呼び出し: {health['daily_network_calls']}回\n\n")
            f.write("| 段階 | 待機グループ | 記事 | 最長待ち（時間） |\n|---|---:|---:|---:|\n")
            for stage, value in health['stages'].items():
                f.write(f"| {stage} | {value['group_count']} | {value['article_count']} | {value['oldest_wait_hours']} |\n")
            f.write(f"\n手動確認: {health['manual_review_counts']}\n\n監視フラグ: {', '.join(health['alerts']) or 'なし'}\n")
