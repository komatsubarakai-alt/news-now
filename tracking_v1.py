"""v1 tracking. Keep source articles; only confirmed changes advance an event."""
import calendar
import copy
import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

CATEGORIES = {'事件・事故', '災害', '政策・制度', '企業・組織', '地域・インフラ'}
UTC = timezone.utc


def scheduled_quote_plausible(item):
    if item.get('status', 'scheduled') != 'scheduled':
        return True
    text = unicodedata.normalize('NFKC', item.get('date_text', item.get('quote', '')))
    if not text:
        return True
    return bool(re.search(r'予定|計画|見込|目指|方針|来年|来月|来春|来夏|来秋|来冬|今春|今夏|今秋|今冬|翌年|翌月|開催決定|開業決定|\d{4}年|\d{1,2}月|\d{1,2}日|へ$', text))


def date_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def safe_url(value):
    return isinstance(value, str) and urlparse(value).scheme in {'http', 'https'} and bool(urlparse(value).netloc)


def deduplicate(articles):
    result, seen = [], set()
    for article in articles:
        url = article.get('url')
        if safe_url(url) and url not in seen and article.get('title'):
            result.append(copy.deepcopy(article))
            seen.add(url)
    return sorted(result, key=lambda a: date_time(a.get('published')) or datetime.min.replace(tzinfo=UTC))


def atomic_save(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        temporary = f.name
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(temporary, path)


def migrate(event, now):
    event.setdefault('lifecycle', 'active')
    event.setdefault('progress_count', 0)
    event.setdefault('progress_history', [])
    event.setdefault('schedules', [])
    event['schedules'] = [s for s in event['schedules'] if scheduled_quote_plausible(s)]
    event.setdefault('last_progress_at', event.get('updated_at', event.get('created_at', now.isoformat())))
    event['articles'] = deduplicate(event.get('articles', []))
    event['article_count'] = len(event['articles'])
    event.setdefault('last_progress_source_at', max((date_time(a.get('published')) for a in event['articles'] if date_time(a.get('published'))), default=now).isoformat())
    return event


def event_text(event):
    return {key: event.get(key) for key in ('event_id', 'event_name', 'category', 'current_stage', 'summary', 'lifecycle') } | {
        'articles': [{'title': a['title'], 'url': a['url'], 'published': a.get('published', '')}
                     for a in event.get('articles', [])[-12:]],
        'schedules': event.get('schedules', [])}


def evidence(result, articles):
    allowed = {a['url'] for a in articles}
    urls = result.get('evidence_urls', [])
    return isinstance(urls, list) and bool(urls) and all(url in allowed for url in urls)


def completion_verified(result, articles):
    if result.get('completed') is not True or result.get('confidence') != 'high':
        return False
    quote = result.get('completion_quote', '')
    if not isinstance(quote, str) or not quote:
        return False
    # A verdict or an announcement alone is not a confirmed conclusion.
    text = unicodedata.normalize('NFKC', quote)
    if re.search(r'未|予定|見込み|目指|へ$|か[？?]?$|検討|せず|していない|しておらず', text):
        return False
    if not re.search(r'判決.{0,8}確定|刑.{0,4}確定|復旧.{0,6}完了|全面.{0,4}復旧|施行された|施行開始|運用開始|開業した|開業しました|正式開業|開業、|開業\s|事業.{0,4}完了|調査.{0,4}終了', text):
        return False
    return any(quote in a.get('title', '') and a['url'] in result.get('evidence_urls', []) and
               not re.search(r'未確定|未完了|予定|見込み|目指|確定へ|完了へ|せず|していない|しておらず', a['title'])
               for a in articles)


def schedule_bounds(quote, published):
    """Only explicit dates. Preserve month/year precision instead of inventing a day."""
    text = unicodedata.normalize('NFKC', quote)
    if '年度' in text:
        return None, None, 'unknown'
    match = re.search(r'(?:(\d{4})年)?(\d{1,2})月(?:(\d{1,2})日)?', text)
    if match:
        year, month, day = match.groups()
        # Without a year keep the date unresolved: year-crossing plans are ambiguous.
        if not year:
            return None, None, 'unknown'
        try:
            year, month = int(year), int(month)
            start = datetime(year, month, int(day or 1), tzinfo=UTC)
            end = start if day else start.replace(day=calendar.monthrange(year, month)[1])
            return start.date().isoformat(), end.date().isoformat(), 'day' if day else 'month'
        except ValueError:
            return None, None, 'unknown'
    match = re.search(r'(\d{4})年', text)
    if match and not re.search(r'春|夏|秋|冬|上旬|中旬|下旬', text):
        year = int(match.group(1))
        if 1 <= year <= 9999:
            return f'{year:04d}-01-01', f'{year:04d}-12-31', 'year'
    return None, None, 'unknown'


def merge_schedules(event, items, articles):
    sources = {a['url']: a for a in articles}
    schedules = event.setdefault('schedules', [])
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url, quote, label = item.get('source_url'), item.get('quote'), item.get('label')
        if url not in sources or not isinstance(quote, str) or not quote or quote not in sources[url]['title'] or not isinstance(label, str) or not label:
            continue
        if not scheduled_quote_plausible(item):
            continue
        start, end, precision = schedule_bounds(quote, sources[url].get('published'))
        # Stable ID allows an explicitly rescheduled/cancelled plan to replace the old one.
        sid = hashlib.sha256(label.encode()).hexdigest()[:16]
        status = item.get('status', 'scheduled')
        if status not in {'scheduled', 'completed', 'cancelled'}:
            status = 'scheduled'
        if status == 'cancelled' and not re.search(r'中止|取り消|取消|撤回', quote):
            status = 'scheduled'
        if status == 'completed' and not re.search(r'開始|開業|施行|完了|開催された|実施された', quote):
            status = 'scheduled'
        record = {'schedule_id': sid, 'label': label[:120], 'date_text': quote,
                  'due_start': start, 'due_end': end, 'precision': precision,
                  'status': status, 'source_url': url, 'published': sources[url].get('published', '')}
        old = next((s for s in schedules if s['schedule_id'] == sid), None)
        if old:
            if (date_time(old.get('published')) or datetime.min.replace(tzinfo=UTC)) > (date_time(record.get('published')) or datetime.min.replace(tzinfo=UTC)):
                continue
            record['history'] = old.get('history', [])[:]
            if old.get('date_text') != quote:
                record['history'].append({k: old.get(k) for k in ('date_text', 'source_url', 'status')})
            schedules[schedules.index(old)] = record
        else:
            schedules.append(record)


def apply_report(event, articles, report, now, initial=False):
    """Attach reporting without changing current stage on duplicates or older backfill."""
    migrate(event, now)
    before_urls = {a['url'] for a in event['articles']}
    additions = [a for a in deduplicate(articles) if a['url'] not in before_urls]
    prior_latest = date_time(event.get('last_progress_source_at')) if event['articles'] else None
    event['articles'] = deduplicate(event['articles'] + articles)
    event['article_count'] = len(event['articles'])
    event['source_article_count'] = max(event.get('source_article_count', 0), event['article_count'])
    if additions:
        event['last_article_added_at'] = now.isoformat()
    trusted = report.get('confidence') == 'high' and evidence(report, articles)
    fresh_evidence = [a for a in articles if a['url'] in report.get('evidence_urls', []) and
                      date_time(a.get('published')) and (prior_latest is None or date_time(a['published']) >= prior_latest)]
    progress = trusted and report.get('meaningful_change') is True and bool(fresh_evidence) and bool(additions)
    if initial or progress:
        for field in ('current_stage', 'summary', 'latest', 'next_watch'):
            if isinstance(report.get(field), str) and report[field].strip():
                event[field] = report[field].strip()
        event['updated_at'] = now.isoformat()
        event['last_progress_at'] = now.isoformat()
        event['last_progress_source_at'] = max((date_time(a.get('published')) for a in articles if date_time(a.get('published'))), default=now).isoformat()
        if progress and not initial:
            event['progress_count'] += 1
            event['progress_history'].append({'at': now.isoformat(), 'stage': event.get('current_stage', ''),
                                              'latest': event.get('latest', ''), 'evidence_urls': report['evidence_urls']})
        if event['lifecycle'] != 'active':
            event.setdefault('lifecycle_history', []).append({'at': now.isoformat(), 'from': event['lifecycle'], 'to': 'active'})
        event['lifecycle'] = 'active'
        event.pop('completed_at', None)
        event.pop('dormant_at', None)
    if trusted:
        merge_schedules(event, report.get('schedules', []), articles)
    if (initial or progress) and completion_verified(report, articles):
        event['lifecycle'] = 'completed'
        event['completed_at'] = now.isoformat()
        event['completion_evidence'] = {'quote': report['completion_quote'], 'urls': report['evidence_urls']}
    return event


def update_lifecycle(events, now, dormant_days=180):
    for event in events:
        migrate(event, now)
        last = date_time(event.get('last_progress_at'))
        if event['lifecycle'] == 'active' and last and (now - last).days >= dormant_days:
            upcoming = any(s.get('status') == 'scheduled' and date_time(s.get('due_end')) and
                           date_time(s['due_end']) >= now - timedelta(days=14) for s in event['schedules'])
            if not upcoming:
                event['lifecycle'] = 'dormant'
                event['dormant_at'] = now.isoformat()


def scheduled_search_due(event, now):
    if event.get('lifecycle') == 'completed':
        return False
    last = date_time(event.get('last_schedule_search_at'))
    for schedule in event.get('schedules', []):
        if schedule.get('status') != 'scheduled':
            continue
        start, end = date_time(schedule.get('due_start')), date_time(schedule.get('due_end'))
        if start and end and start - timedelta(days=7) <= now:
            interval = 1 if now <= end + timedelta(days=14) else 7
            if not last or now - last >= timedelta(days=interval):
                return True
    return False
