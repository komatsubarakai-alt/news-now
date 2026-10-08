"""Editorial exclusions and conservative publication gate. No automatic deletion."""
import copy
import json
from itertools import combinations
from pathlib import Path
from merge_safety import article_content, contradictory, normalized, incident_conflict


def title_key(article):
    return normalized(article_content(article).split('\n', 1)[0]).strip()


def load_corrections(path='data_corrections.json'):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def excluded(article, registry):
    return any(d.get('status') == 'held' and (article['url'] == d['article']['url'] or
               title_key(article) == title_key(d['article'])) for d in registry.get('held_articles', []))


def protect_existing(events, registry):
    rules = {r['event_id']: r for r in registry.get('event_rules', [])}
    for event in events:
        rule = rules.get(event['event_id'])
        if not rule:
            issues = publication_issues(event)
            if issues:
                event['publication_status'] = 'held'
                event['publication_reason'] = ','.join(issues)
            continue
        if rule.get('publication_status') == 'held':
            event['publication_status'] = 'held'
        if rule.get('merged_into'):
            event['merged_into'] = rule['merged_into']
        blocked_urls = set(rule.get('blocked_urls', []))
        blocked_keys = {title_key({'title': t}) for t in rule.get('blocked_titles', [])}
        # Never silently discard unexpected later additions: quarantine whole update.
        if any(a['url'] in blocked_urls or title_key(a) in blocked_keys for a in event.get('articles', [])):
            event['publication_status'] = 'held'
            event['publication_reason'] = '訂正済み記事の再混入を検知。編集確認待ち。'
        event['article_count'] = len(event.get('articles', []))


def publication_issues(event):
    articles = event.get('articles', [])
    issues = []
    if event.get('article_count') != len(articles): issues.append('article_count_mismatch')
    if not articles: issues.append('empty_event')
    name = event.get('event_name', '')
    if any(contradictory(name, article_content(a)) for a in articles): issues.append('stored_article_conflict')
    metadata = '\n'.join(str(event.get(k, '')) for k in ('current_stage', 'summary', 'latest', 'next_watch'))
    if contradictory(name, metadata): issues.append('stored_metadata_conflict')
    if not event.get('source_scope') and any(incident_conflict(article_content(a), article_content(b)) for a,b in combinations(articles,2)):
        issues.append('stored_incident_conflict')
    if len({a['url'] for a in articles}) != len(articles): issues.append('duplicate_article_url')
    return issues


def public_events(events):
    result=[]
    for event in events:
        if event.get('publication_status') == 'held' or event.get('category') == '対象外': continue
        issues = publication_issues(event)
        if issues:
            event['publication_status'] = 'held'
            event['publication_reason'] = ','.join(issues)
            continue
        result.append(copy.deepcopy(event))
    return result


def write_public(events, registry=None):
    from tracking_v1 import atomic_save
    protect_existing(events, registry if registry is not None else load_corrections())
    public = public_events(events)
    atomic_save('public_events.json', public)
    return public


if __name__ == '__main__':
    events=json.loads(Path('events.json').read_text())
    write_public(events)
