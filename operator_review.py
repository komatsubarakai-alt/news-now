"""Local operator review: export/import decisions; normal tracker keeps all guards.

No API calls, direct event edits, tokens or public admin endpoint.
"""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pending_triage import fingerprint, group_key, preflight, current_groups
from publication_safety import load_corrections
from tracking_v1 import atomic_save

ACTIONS = {'same_event', 'new_event', 'exclude'}
RETRY = {'queued', 'needs_evidence', 'retry_api', 'retry_format', 'retry_system'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def target_digest(event):
    return digest({k: event.get(k) for k in
                   ('event_id', 'event_name', 'category', 'publication_status', 'articles')})


def review_revision(item):
    return digest({'status': item.get('status'), 'reason': item.get('reason'),
                   'attempts': item.get('decision_attempts'),
                   'previous_decision_id': item.get('operator_review', {}).get('decision_id')})


def read(root, name, default):
    path = Path(root) / name
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def build_snapshot(root, now=None):
    from review_inventory import build_inventory
    root = Path(root)
    inventory = build_inventory(root)
    state = read(root, 'tracking_triage.json', {})['items']
    events = read(root, 'events.json', [])
    eligible = [e for e in events if e.get('publication_status') != 'held'
                and not e.get('merged_into') and e.get('category') != '対象外']
    diagnostics = {}
    for entry in read(root, 'merge_review.json', []):
        group = entry.get('data', {}).get('group') or entry.get('payload', {}).get('group')
        if group:
            diagnostics[group_key(group)] = entry.get('diagnostics', {})
    rows = []
    for row in inventory['manual_reviews']:
        item = state[row['review_key']]
        rows.append({**row, 'group_hash': digest(item['group']),
                     'review_revision': review_revision(item),
                     'source_fingerprint': fingerprint(item['group']),
                     'event_name': item['group'].get('event_name', ''),
                     'category': item['group'].get('category', ''),
                     'summary': item['group'].get('summary', ''),
                     'first_seen_at': item.get('first_seen_at'),
                     'last_seen_at': item.get('last_seen_at'),
                     'previous_review': item.get('operator_review'),
                     'diagnostics': diagnostics.get(row['review_key'], {})})
    result = {'schema_version': 1, 'repository': 'komatsubarakai-alt/news-now',
              'generated_at': (now or datetime.now(timezone.utc)).isoformat(),
              'reviews': rows,
              'events': [{'event_id': e['event_id'], 'event_name': e['event_name'],
                          'category': e.get('category'), 'summary': e.get('summary', ''),
                          'current_stage': e.get('current_stage', ''),
                          'target_hash': target_digest(e),
                          'articles': copy.deepcopy(e.get('articles', []))} for e in eligible]}
    result['snapshot_id'] = digest(result)
    return result


def validate_decisions(document, events, state, registry, require_manual=True):
    """Validate the whole import before any writes; stale inputs fail closed."""
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise ValueError('判断ファイルの形式が違います')
    if document.get('repository') != 'komatsubarakai-alt/news-now':
        raise ValueError('対象リポジトリが違います')
    rows = document.get('decisions')
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError('判断が空、または件数が不正です')
    known = {a['url']: a for e in events for a in e.get('articles', [])}
    seen, validated = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('判断の形式が違います')
        key = row.get('review_key')
        if not isinstance(key, str) or key in seen:
            raise ValueError('確認キーが不正または重複しています')
        seen.add(key)
        item = state.get(key)
        if not item or (require_manual and item.get('status') != 'manual_review'):
            raise ValueError(f'{key}: 手動確認待ちではありません。画面を作り直してください')
        if row.get('review_revision') != review_revision(item):
            raise ValueError(f'{key}: 保留の状態が変わっています。画面を作り直してください')
        group = item.get('group', {})
        if group_key(group) != key or row.get('source_fingerprint') != fingerprint(group) or row.get('group_hash') != digest(group):
            raise ValueError(f'{key}: 記事が更新されています。再確認してください')
        action = row.get('action')
        note = row.get('note')
        if action not in ACTIONS or not isinstance(note, str) or not 3 <= len(note.strip()) <= 1000:
            raise ValueError(f'{key}: 振り分けと3〜1000文字の判断理由が必要です')
        blocked = preflight(group, known, registry, {})
        if blocked and action != 'exclude':
            raise ValueError(f'{key}: {blocked} のため再処理できません')
        target = None
        if action == 'same_event':
            target = next((e for e in events if e['event_id'] == row.get('event_id')), None)
            if not target or target.get('publication_status') == 'held' or target.get('merged_into') or target.get('category') == '対象外':
                raise ValueError(f'{key}: 公開可能な既存イベントを選んでください')
            if row.get('target_hash') != target_digest(target):
                raise ValueError(f'{key}: 既存イベントが更新されています。再確認してください')
        elif row.get('event_id') or row.get('target_hash'):
            raise ValueError(f'{key}: 既存イベント以外の判断に対象IDは指定できません')
        result = {k: row.get(k) for k in
                  ('review_key', 'source_fingerprint', 'group_hash', 'review_revision', 'action', 'event_id', 'target_hash')}
        result['note'] = note.strip()
        result['decision_id'] = digest(result)
        validated.append(result)
    return validated


def import_decisions(root, document, apply=False, now=None):
    root = Path(root)
    events = read(root, 'events.json', [])
    state = read(root, 'tracking_triage.json', {}).get('items', {})
    validated = validate_decisions(document, events, state, load_corrections(root / 'data_corrections.json'))
    saved = read(root, 'operator_review_decisions.json', {'schema_version': 1, 'repository': document['repository'], 'decisions': []})
    prior = {row['review_key']: row for row in saved['decisions']}
    for row in validated:
        previous = prior.get(row['review_key'])
        if previous and previous.get('decision_id') == row['decision_id']:
            continue
        row['submitted_at'] = (now or datetime.now(timezone.utc)).isoformat()
        row['history'] = (previous.get('history', []) + [{k: v for k, v in previous.items() if k != 'history'}]) if previous else []
        prior[row['review_key']] = row
    if apply:
        saved['decisions'] = list(prior.values())
        atomic_save(root / 'operator_review_decisions.json', saved)
    return {'validated_count': len(validated), 'written': apply,
            'message': 'イベント・進展数は変更していません。取り込み後も通常AI処理と安全検証を通します。'}


def prepare_reviews(events, groups, state, document, registry, now):
    """Consume each instruction once, during the existing six-hour AI run only."""
    additions = []
    latest = {group_key(g): g for g in groups}
    for row in document.get('decisions', []):
        key = row.get('review_key')
        item = state.get(key)
        if not item:
            continue
        prior = item.get('operator_review', {})
        if prior.get('decision_id') != row.get('decision_id'):
            try:
                if key in latest and digest(latest[key]) != row.get('group_hash'):
                    raise ValueError('現在の収集記事が更新されています。再確認してください')
                validated = validate_decisions({**document, 'decisions': [row]}, events, state, registry)[0]
                if validated['decision_id'] != row.get('decision_id'):
                    raise ValueError('判断IDが一致しません')
            except (ValueError, TypeError, KeyError) as exc:
                item['operator_review_rejection'] = {'decision_id': row.get('decision_id'), 'reason': str(exc), 'at': now.isoformat()}
                continue
            if prior:
                item.setdefault('operator_review_history', []).append(copy.deepcopy(prior))
            item['operator_review'] = {**validated, 'submitted_at': row.get('submitted_at'), 'applied_at': now.isoformat(),
                                       'before': {k: copy.deepcopy(item.get(k)) for k in
                                                  ('status', 'reason', 'decision_attempts', 'source_fingerprint')}}
            item.pop('operator_review_rejection', None)
            item['decision_attempts'] = 0
            item['status'] = 'operator_excluded' if row['action'] == 'exclude' else 'queued'
            item['reason'] = 'operator_excluded' if row['action'] == 'exclude' else 'operator_requested_review'
        if item.get('status') in RETRY and fingerprint(item['group']) == row.get('source_fingerprint') and digest(item['group']) == row.get('group_hash'):
            additions.append(item['group'])
    return current_groups(additions, groups)


def active_selection(group, state):
    item = state.get(group_key(group), {})
    selection = item.get('operator_review', {})
    if selection.get('action') in {'same_event', 'new_event'} and selection.get('source_fingerprint') == fingerprint(group) and selection.get('group_hash') == digest(group):
        return selection
    return None


def render_page(snapshot):
    # Never interpolate source content into markup or executable JS.
    data = json.dumps(snapshot, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    template = (Path(__file__).parent / 'operator_review_template.html').read_text(encoding='utf-8')
    return template.replace('<!-- REVIEW_SNAPSHOT -->', data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build')
    build.add_argument('--output', required=True)
    ingest = commands.add_parser('import')
    ingest.add_argument('file')
    ingest.add_argument('--apply', action='store_true', help='判断ファイルだけを保存（イベントの直接編集はしない）')
    args = parser.parse_args()
    if args.command == 'build':
        snapshot = build_snapshot(args.root)
        target = Path(args.output)
        # Keep operator snapshots out of the public site/repository.
        if Path(args.root).resolve() in target.resolve().parents:
            parser.error('出力は公開リポジトリの外に指定してください')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_page(snapshot), encoding='utf-8')
        print(f'確認画面を作成: {len(snapshot["reviews"])}件 / {target}')
    else:
        result = import_decisions(args.root, json.loads(Path(args.file).read_text(encoding='utf-8')), args.apply)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
