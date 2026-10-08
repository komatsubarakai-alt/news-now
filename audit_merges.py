"""Read-only audit. Findings are review candidates, never automatic deletions."""
import argparse
import json
from collections import defaultdict
from pathlib import Path
from merge_safety import article_content, contradictory


def audit(events):
    findings = []
    names = defaultdict(list)
    urls = defaultdict(list)
    for event in events:
        identifier = event.get('event_id', '')
        names[event.get('event_name', '')].append(identifier)
        for article in event.get('articles', []):
            urls[article['url']].append(identifier)
            if contradictory(event.get('event_name', ''), article_content(article)):
                findings.append({'type': 'content_conflict', 'event_id': identifier,
                                 'event_name': event.get('event_name'), 'article_title': article['title'],
                                 'article_url': article['url']})
    for name, identifiers in names.items():
        if len(identifiers) > 1:
            findings.append({'type': 'duplicate_name_needs_review', 'event_name': name, 'event_ids': identifiers})
    for url, identifiers in urls.items():
        if len(set(identifiers)) > 1:
            findings.append({'type': 'shared_article_needs_review', 'article_url': url, 'event_ids': sorted(set(identifiers))})
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path', nargs='?', default='events.json')
    args = parser.parse_args()
    events = json.loads(Path(args.path).read_text(encoding='utf-8'))
    print(json.dumps({'event_count': len(events), 'article_count': sum(len(e.get('articles', [])) for e in events),
                      'findings': audit(events), 'limitations': '限定した矛盾ルール。未検出は正常の証明ではありません。'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
