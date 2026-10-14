"""Conservative free triage and scheduling; priority never proves event identity."""
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tracking_v1 import CATEGORIES, atomic_save, date_time, safe_url
from pending_triage import group_key, fingerprint

# A safety/progress signal always overrides an entertainment/sports signal.
TRACK = re.compile(r'逮捕|起訴|裁判|判決|控訴|捜査|強盗|殺人|暴行|窃盗|詐欺|事故|火災|出火|死亡|死去|被害|不祥事|破産|倒産|廃止|閉鎖|撤去|違法|無許可|条例|制度|政策|規制|予算|支援|災害|地震|洪水|浸水|土砂|復旧|避難|減便|運休|再開発|建設|開業|施行|給食|医療|感染|食中毒|訴訟|虐待|労働|補償|中止')
SPORT = re.compile(r'コンサドーレ|ファイターズ|高校野球|甲子園|ボーイズ|全道大会|サッカー|プロ野球|バスケット|ラグビー|スポーツ')
SPORT_REPORT = re.compile(r'試合|対戦|[0-9０-９]+回戦|[0-9０-９]+強|リーグ|勝利|優勝|敗戦|登板|打席|打点|安打|適時|三塁打|突破|日本代表|アウェー|ホーム戦')
LIGHT = re.compile(r'グルメ|食べ歩き|スイーツ|新メニュー|ランチメニュー|ライブ開催|コンサート|音楽フェス|ファンミーティング|スタンプラリー')
PROGRESS = re.compile(r'起訴|判決|控訴|復旧|施行|開業|逮捕|捜査|被害|撤去|破産|倒産|沈没|強盗|殺人|洪水|浸水|避難|火災')


def article_fingerprint(article):
    data = {k: article.get(k, '') for k in ('url', 'title', 'description', 'published')}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pending_articles(group):
    """Only actual, well-formed queue sources can own an upstream revision."""
    articles = group.get('articles', []) if isinstance(group, dict) else []
    if not isinstance(articles, list) or not articles or any(
            not isinstance(a, dict) or not safe_url(a.get('url')) or not a.get('title') or
            any(not isinstance(a.get(k, ''), str) for k in ('title', 'description', 'published'))
            for a in articles):
        return []
    return articles


def valid_classification(group):
    return (bool(pending_articles(group)) and
            isinstance(group.get('event_name'), str) and bool(group['event_name'].strip()) and
            isinstance(group.get('category'), str) and
            isinstance(group.get('tracking_value'), str) and
            group.get('category') in CATEGORIES | {'対象外'} and
            group.get('tracking_value') in {'high', 'medium', 'low'})


def pending_reference(group, stage):
    return {'stage': stage, 'group_key': group_key(group),
            'source_fingerprint': fingerprint(group)}


def set_downstream_waiting(record, references):
    previous = record.get('downstream_waiting', [])
    if previous and previous != references:
        record.setdefault('downstream_history', []).append({
            'source_fingerprint': record.get('source_fingerprint'),
            'references': copy.deepcopy(previous)})
    if references:
        record['downstream_waiting'] = copy.deepcopy(references)
    else:
        record.pop('downstream_waiting', None)


def downstream_article_index(status_pending=(), tracking_pending=()):
    """References only; never move sources or prove that different articles match."""
    index = {}
    for stage, groups in (('status_generation', status_pending), ('tracking', tracking_pending)):
        for group in groups:
            if stage == 'tracking' and not valid_classification(group):
                continue
            for article in pending_articles(group):
                references = index.setdefault(article_fingerprint(article), [])
                reference = pending_reference(group, stage)
                if reference not in references:
                    references.append(reference)
    return index


def tracking_classification_index(groups):
    """Reuse only a whole unchanged group with the same name and unambiguous labels."""
    candidates = {}
    for group in groups:
        if valid_classification(group):
            key = (group['event_name'], fingerprint(group))
            candidates.setdefault(key, []).append(group)
    return {key: matches[0] for key, matches in candidates.items()
            if len({(g['category'], g['tracking_value']) for g in matches}) == 1}


def obvious_out_of_scope(article):
    # Only classify the headline; publisher names are not genre evidence.
    title = str(article.get('title', '')).split(' - ')[0]
    if TRACK.search(title):
        return None
    if SPORT.search(title) and SPORT_REPORT.search(title):
        return 'sports_result_or_fixture'
    if LIGHT.search(title):
        return 'entertainment_or_food_announcement'
    return None


def read(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def triage_articles(articles, events, statuses, state, now, *,
                    status_pending=(), tracking_pending=()):
    """Return AI input while retaining every source, reason and changed revision."""
    saved = {article_fingerprint(a) for e in events for a in e.get('articles', [])}
    classified = {article_fingerprint(a): s for s in statuses for a in s.get('articles', [])}
    downstream = downstream_article_index(status_pending, tracking_pending)
    accepted = []
    for a in articles:
        key = a.get('url') or article_fingerprint(a)
        mark = article_fingerprint(a)
        prior = state.setdefault(key, {'first_seen_at': now.isoformat(), 'source_history': []})
        if prior.get('source_fingerprint') not in (None, mark):
            prior.setdefault('source_history', []).append({'article': copy.deepcopy(prior['article']),
                'status': prior['status'], 'reason': prior['reason'], 'last_seen_at': prior['last_seen_at'],
                'downstream_waiting': copy.deepcopy(prior.get('downstream_waiting', []))})
            prior['first_seen_at'] = now.isoformat()
        references = []
        status, reason = 'queued', 'needs_classification'
        if mark in saved:
            status, reason = 'already_saved', 'unchanged_saved_source'
        elif mark in classified:
            s = classified[mark]
            if s.get('category') == '対象外' or s.get('tracking_value') == 'low':
                status, reason = 'out_of_scope', 'previous_ai_out_of_scope_or_low_value'
            else:
                status, reason = 'classified_waiting', 'status_ready_for_tracking'
        elif mark in downstream:
            status, reason = 'downstream_waiting', 'unchanged_downstream_source'
            references = downstream[mark]
        elif obvious_out_of_scope(a):
            status, reason = 'out_of_scope', obvious_out_of_scope(a)
        set_downstream_waiting(prior, references)
        prior.update(article=copy.deepcopy(a), source_fingerprint=mark, status=status,
                     reason=reason, last_seen_at=now.isoformat())
        if status == 'queued':
            accepted.append(a)
    return prioritize_articles(accepted, state, now)


def prioritize_articles(articles, state, now):
    groups = [{'articles': [a]} for a in articles]
    mapped = {group_key(g): state.get(g['articles'][0].get('url'), {}) for g in groups}
    return [g['articles'][0] for g in prioritize_groups(groups, mapped, now)]


def prioritize_groups(groups, state, now):
    """Urgent progress, new sources, then evidence retries; oldest gets one in four."""
    def first(g):
        entry = state.get(group_key(g), {})
        return date_time(entry.get('first_seen_at')) or now
    def rank(g):
        entry = state.get(group_key(g), {})
        text = ' '.join(a.get('title', '') for a in g.get('articles', []))
        waiting = (now - first(g)).total_seconds()
        # Source updates reopen even a formerly paused record.
        unchanged = entry.get('source_fingerprint') in (None, fingerprint(g))
        retry = unchanged and entry.get('status') in {'needs_evidence', 'retry_format', 'retry_system', 'retry_api'}
        return (0 if waiting >= 48 * 3600 else 3 if retry else 1 if PROGRESS.search(text) else 2, first(g))
    ranked = sorted(enumerate(groups), key=lambda pair: (rank(pair[1]), pair[0]))
    result = []
    while ranked:
        if len(result) % 4 == 3:
            oldest = min(range(len(ranked)), key=lambda i: (first(ranked[i][1]), ranked[i][0]))
            result.append(ranked.pop(oldest)[1])
        else:
            result.append(ranked.pop(0)[1])
    return result


def retain_group(state, group, status, reason, now, decision=False, *, downstream_waiting=()):
    """Separate JSON/evidence failure attempts from transport/credit failures."""
    key = group_key(group)
    entry = state.setdefault(key, {'first_seen_at': now.isoformat(), 'decision_attempts': 0, 'source_history': []})
    entry.pop('cached_rejection_waiting', None)
    mark = fingerprint(group)
    if entry.get('source_fingerprint') not in (None, mark):
        entry.setdefault('source_history', []).append({'group': copy.deepcopy(entry['group']),
            'status': entry['status'], 'reason': entry['reason'],
            'downstream_waiting': copy.deepcopy(entry.get('downstream_waiting', []))})
        entry['decision_attempts'] = 0
        entry['first_seen_at'] = now.isoformat()
    set_downstream_waiting(entry, list(downstream_waiting))
    entry.update(group=copy.deepcopy(group), source_fingerprint=mark, status=status,
                 reason=reason, last_seen_at=now.isoformat())
    if decision:
        entry['decision_attempts'] += 1
        if entry['decision_attempts'] >= 3:
            entry['status'] = 'manual_review'
    return entry


def pause_reason(group, state):
    entry = state.get(group_key(group), {})
    if entry.get('status') == 'manual_review' and entry.get('source_fingerprint') == fingerprint(group):
        return 'manual_review'
    reasons = [obvious_out_of_scope(a) for a in group.get('articles', [])]
    if reasons and all(reasons):
        return 'obvious_out_of_scope'
    return None


def already_saved_group(group, events):
    saved = {article_fingerprint(a) for e in events for a in e.get("articles", [])}
    articles = group.get("articles", [])
    return bool(articles) and all(article_fingerprint(a) in saved for a in articles)
