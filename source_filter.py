"""Lossless collection, conservative representative selection before any AI call.

RSS source is a distribution endpoint, not proof of the original producer.
No scraping, redirect guessing, fuzzy deletion, or event merging happens here.
"""
import argparse
import copy
import hashlib
import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from tracking_v1 import atomic_save

PORTALS = {'news.yahoo.co.jp', 'topics.smt.docomo.ne.jp', 'news.google.com'}
PRODUCERS = {
    '北海道ニュースUHB': 'uhb', 'UHB 北海道文化放送': 'uhb',
    'HTB北海道ニュース': 'htb', 'HTB北海道テレビ': 'htb', 'HTB 北海道テレビ': 'htb',
    'STVニュース北海道': 'stv', 'STV NEWS NNN': 'stv',
    '共同通信': 'kyodo', '朝日新聞': 'asahi', '毎日新聞': 'mainichi',
    'テレビ朝日系（ANN）': 'ann', 'テレビ北海道': 'tvh',
    'スポニチアネックス': 'sponichi', 'みんなの経済新聞ネットワーク': 'minkei',
    'デイリー新潮': 'shincho', 'LIMO': 'limo',
}
HOST_PRODUCERS = {'www.uhb.jp': 'uhb', 'www.htb.co.jp': 'htb', 'www.stv.jp': 'stv',
                  'www.asahi.com': 'asahi', 'mainichi.jp': 'mainichi'}


def host(url):
    try:
        parsed = urlsplit(url or '')
        return (parsed.hostname or '').lower() if parsed.scheme in {'http', 'https'} else ''
    except ValueError:
        return ''


def parse_rss(root):
    articles = []
    for item in root.findall('.//item'):
        source = item.find('source')
        articles.append({
            'title': html.unescape(item.findtext('title', '')),
            'url': item.findtext('link', ''),
            'published': item.findtext('pubDate', ''),
            'description': html.unescape(item.findtext('description', '')),
            'rss_source': {'name': html.unescape(source.text or '') if source is not None else '',
                           'url': source.get('url', '') if source is not None else ''},
        })
    return articles


def source(article):
    data = article.get('rss_source') or {}
    if not isinstance(data, dict):
        data = {}
    name, url = data.get('name', ''), data.get('url', '')
    # Legacy data: retain an explicit provenance label; never infer from a Google URL.
    if not name:
        match = re.search(r'\s+[-｜|]\s+([^｜|]+)$', article.get('title', ''))
        name = match.group(1).strip() if match else ''
    return str(name), host(url)


def yahoo(article):
    name, endpoint = source(article)
    return endpoint == 'news.yahoo.co.jp' or name.startswith('Yahoo!ニュース')


def original_yahoo(article):
    name, _ = source(article)
    return bool('オリジナル' in name or re.search(r'Yahoo!ニュース\s*オリジナル', article.get('title', ''))
                or article.get('content_origin') == 'yahoo_original')


def headline(article):
    title = unicodedata.normalize('NFKC', html.unescape(article.get('title', '')))
    name, _ = source(article)
    if name:
        # Only the actual endpoint suffix is removed; stage/date/numbers stay intact.
        title = re.sub(r'\s+[-｜|]\s*' + re.escape(unicodedata.normalize('NFKC', name)) + r'\s*$', '', title)
    producer = ''
    for label, code in PRODUCERS.items():
        normalized = unicodedata.normalize('NFKC', label)
        suffix = '(' + normalized + ')'
        if title.endswith(suffix):
            producer = code
            title = title[:-len(suffix)]
            break
    # NNN's own page wrapper has an explicit STV credit; do not strip arbitrary text.
    title = re.sub(r'\(\d{4}年\d{1,2}月\d{1,2}日掲載\)\s*[｜|]\s*STV NEWS NNN$', '', title)
    return re.sub(r'\s+', '', title).casefold(), producer


def publisher(article):
    name, endpoint = source(article)
    _, hint = headline(article)
    # FNN/NNN aggregate several stations: never assume every article is UHB/STV.
    return hint or PRODUCERS.get(name) or HOST_PRODUCERS.get(endpoint) or endpoint or name


def stamp(article):
    value = article.get('published', '')
    try:
        dt = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def close_publication(a, b):
    x, y = stamp(a), stamp(b)
    return bool(x and y and abs((x-y).total_seconds()) <= 24 * 3600)


def direct(article):
    name, endpoint = source(article)
    return bool(endpoint and endpoint not in PORTALS and not yahoo(article))


def substantive_text(article):
    # A Google RSS description normally repeats the heading and source only.
    # If a future feed provides actual text, do not suppress differing/unknown updates.
    if article.get('content'):
        return re.sub(r'\s+', '', str(article['content']))
    description = article.get('description', '')
    if '<a ' in description and '<font ' in description:
        return ''
    text = re.sub(r'<[^>]*>', '', html.unescape(description))
    text = re.sub(r'\s+', '', text)
    return text if len(text) >= 40 else ''


def duplicate_reason(a, representative):
    if a.get('url') and a['url'] == representative.get('url'):
        return 'same_url'
    if original_yahoo(a) or original_yahoo(representative):
        return ''
    content_a, content_b = substantive_text(a), substantive_text(representative)
    if (content_a or content_b) and content_a != content_b:
        return ''
    x, hint = headline(a)
    y, _ = headline(representative)
    if not x or x != y or len(x) < 28 or not close_publication(a, representative):
        return ''
    if yahoo(a) and direct(representative):
        # A credited producer must agree. Unknown producer is explicitly not called verified.
        if hint and hint != publisher(representative):
            return ''
        return 'exact_headline_original_available' if not hint else 'credited_reprint_original_available'
    if source(a) == source(representative) and all(source(a)):
        return 'same_endpoint_exact_headline'
    return ''


def prefilter(articles):
    kept, excluded = [], []
    ordered = sorted(enumerate(articles), key=lambda pair: (not direct(pair[1]), yahoo(pair[1]), pair[0]))
    for index, raw in ordered:
        article = copy.deepcopy(raw)
        match = next(((representative, duplicate_reason(article, representative))
                      for _, representative in kept if duplicate_reason(article, representative)), None)
        if match:
            representative, reason = match
            excluded.append({'article': article, 'representative_url': representative['url'],
                             'reason': reason, 'producer_verified': reason == 'credited_reprint_original_available'})
            continue
        if yahoo(article):
            article['collection_role'] = 'yahoo_original' if original_yahoo(article) else 'supplementary'
            article['collection_reason'] = 'original_content' if original_yahoo(article) else 'original_unconfirmed_or_unavailable'
        else:
            article['collection_role'] = 'primary' if direct(article) else 'source_unconfirmed'
        kept.append((index, article))
    kept.sort(key=lambda pair: pair[0])
    return [article for _, article in kept], excluded


def record_exclusions(exclusions, scope, path='source_exclusions.json'):
    archive = json.loads(Path(path).read_text()) if Path(path).exists() else []
    known = {(x['article'].get('url'), x.get('representative_url'), x.get('scope')) for x in archive}
    now = datetime.now(timezone.utc).isoformat()
    for entry in exclusions:
        key = (entry['article'].get('url'), entry.get('representative_url'), scope)
        if key not in known:
            archive.append(dict(entry, scope=scope, recorded_at=now))
            known.add(key)
    atomic_save(path, archive)


def measure(raw, kept, excluded):
    from ai_candidates import build_candidates
    before, after = len(build_candidates(raw)), len(build_candidates(kept))
    return {'raw_article_count': len(raw), 'yahoo_article_count': sum(map(yahoo, raw)),
            'yahoo_share_percent': round(100 * sum(map(yahoo, raw)) / max(1, len(raw)), 2),
            'ai_input_article_count': len(kept), 'excluded_article_count': len(excluded),
            'excluded_yahoo_count': sum(yahoo(x['article']) for x in excluded),
            'supplementary_yahoo_count': sum(a.get('collection_role') == 'supplementary' for a in kept),
            'candidate_pairs_before': before, 'candidate_pairs_after': after,
            'first_stage_call_limit_before': min(100, before), 'first_stage_call_limit_after': min(100, after),
            'estimated_first_stage_calls_saved': max(0, min(100, before)-min(100, after)),
            'cost_note': 'Call capacity only, not actual tokens/cost; later-stage calls and retries are excluded.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rss', default='sapporo_news.xml')
    args = parser.parse_args()
    raw = parse_rss(ET.parse(args.rss).getroot())
    inbox_path = Path('news_inbox.json')
    inbox = json.loads(inbox_path.read_text(encoding='utf-8')) if inbox_path.exists() else []
    by_url = {a['url']: a for a in inbox + raw}
    inbox = list(by_url.values())
    atomic_save('news_inbox.json', inbox)
    kept, excluded = prefilter(inbox)
    from queue_policy import read, triage_articles
    collection_state = read('collection_triage.json', {'items': {}})
    source_kept = kept
    kept = triage_articles(kept, read('events.json', []), read('news_status.json', []),
                          collection_state['items'], datetime.now(timezone.utc))
    for entry in excluded:
        a = entry['article']
        collection_state['items'][a['url']] = {
            'article': a, 'status': 'duplicate_source',
            'reason': entry.get('reason', 'verified_reprint'),
            'representative_url': entry.get('representative_url')}
    atomic_save('collection_triage.json', collection_state)
    atomic_save('news.json', raw)
    atomic_save('news_filtered.json', kept)
    record_exclusions(excluded, 'primary_rss')
    metrics = measure(inbox, kept, excluded)
    metrics.update(measured_at=datetime.now(timezone.utc).isoformat(),
                   input_sha256=hashlib.sha256(Path(args.rss).read_bytes()).hexdigest())
    atomic_save('news_source_metrics.json', metrics)
    metrics['source_input_before_queue_triage'] = len(source_kept)
    metrics['queue_triage_skipped_count'] = len(source_kept) - len(kept)
    metrics['pair_call_capacity_after'] = min(int(__import__('os').environ.get('PAIR_CALL_LIMIT', '4')), len(__import__('ai_candidates').build_candidates(kept)))
    atomic_save('news_source_metrics.json', metrics)
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == '__main__':
    main()
