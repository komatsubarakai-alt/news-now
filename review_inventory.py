"""Read-only operator inventory. Never clears holds or rewrites production data."""
import argparse
import copy
import json
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone
from pending_triage import preflight, summary
from publication_safety import load_corrections
from schedule_evidence import apply_verified_dates
from tracking_v1 import migrate
from source_filter import headline


def build_inventory(root):
    root = Path(root)
    read = lambda name: json.loads((root / name).read_text())
    events = read('events.json')
    pending = read('tracking_pending.json')
    triage = read('tracking_triage.json')['items']
    known = {a['url']: a for e in events for a in e.get('articles', [])}
    registry = load_corrections(root / 'data_corrections.json')
    headings = {}
    for event in events:
        if event.get('publication_status') == 'held':
            continue
        for article in event.get('articles', []):
            key = headline(article)[0]
            if len(key) >= 28:
                headings.setdefault(key, set()).add(event['event_id'])
    manual = []
    for key, item in triage.items():
        if item.get('status') != 'manual_review':
            continue
        status = preflight(item['group'], known, registry, {})
        action = ('保存済みの照合。元の記録を残して整理候補' if status == 'already_saved' else
                  '編集除外を維持' if status == 'editorial_hold' else
                  '対象外・低優先度の整理候補' if status in {'out_of_scope', 'low_priority'} else
                  '元記事本文・日時・固有情報の確認が必要。自動再公開しない')
        candidates = sorted({eid for a in item['group'].get('articles', [])
                             for eid in headings.get(headline(a)[0], set())})
        manual.append({'review_key': key, 'reason': item.get('reason'), 'decision_attempts': item.get('decision_attempts'),
                       'triage_suggestion': status or 'needs_source_review', 'action': action,
                       'exact_heading_candidate_event_ids': candidates,
                       'articles': copy.deepcopy(item['group'].get('articles', []))})
    # A copy only: show how reviewed dates would apply at the next normal run.
    preview = copy.deepcopy(events)
    now = datetime.now(timezone.utc)
    for event in preview:
        migrate(event, now)
    apply_verified_dates(preview, now, root / 'verified_schedule_dates.json')
    schedules = [{'event_id': e['event_id'], 'event_name': e['event_name'], 'schedules': e['schedules']}
                 for e in preview if e.get('schedules')]
    return {'read_only': True, 'manual_count': len(manual),
            'manual_reason_counts': dict(Counter(x['reason'].split(':', 1)[0] for x in manual)),
            'manual_suggestion_counts': dict(Counter(x['triage_suggestion'] for x in manual)),
            'manual_exact_heading_candidates': sum(bool(x['exact_heading_candidate_event_ids']) for x in manual),
            'pending_metrics_preview': summary(triage, pending),
            'manual_reviews': manual, 'schedule_preview': schedules,
            'backfill_review_events': [{'event_id': e['event_id'], 'event_name': e['event_name'], 'backfill': e['backfill']}
                for e in events if e.get('backfill', {}).get('status') == 'needs_review']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='.')
    args = parser.parse_args()
    print(json.dumps(build_inventory(args.root), ensure_ascii=False, indent=2))
