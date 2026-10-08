"""Conservative source-bound grouping. Missing cross-pair proof never joins clusters."""
import json
from collections import Counter
from merge_safety import UpdateHeld, review, save_reviews, validate_identity
from tracking_v1 import atomic_save


def parse_decision(result):
    raw = result.get('ai_result', '')
    return raw if isinstance(raw, dict) else json.loads(raw.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())


def negative_pairs(results):
    pairs = set()
    for result in results:
        try:
            decision = parse_decision(result)
            if decision.get('relation') in {'different', 'related'} and decision.get('confidence') == 'high':
                pairs.add(frozenset((result['article_a']['url'], result['article_b']['url'])))
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return pairs


def build_groups(results):
    articles, decisions, held = {}, {}, []
    blocked = negative_pairs(results)
    for result in results:
        a, b = result['article_a'], result['article_b']
        articles[a['url']], articles[b['url']] = a, b
        try:
            decision = parse_decision(result)
            if decision.get('relation') == 'same_event':
                if frozenset((a['url'], b['url'])) in blocked:
                    raise UpdateHeld('conflicting_pair_decisions')
                validate_identity([a], [b], decision)
                decisions[frozenset((a['url'], b['url']))] = decision
            elif decision.get('relation') not in {'different', 'related'} or decision.get('confidence') != 'high':
                held.append(review('article_grouping', 'uncertain_pair', result))
        except (ValueError, TypeError, AttributeError) as exc:
            held.append(review('article_grouping', str(exc), result))
    clusters = [{url} for url in articles]
    for pair, decision in decisions.items():
        left, right = tuple(pair)
        a = next(c for c in clusters if left in c)
        b = next(c for c in clusters if right in c)
        if a is b:
            continue
        if all(frozenset((x, y)) in decisions for x in a for y in b):
            a.update(b); clusters.remove(b)
        else:
            held.append(review('article_grouping', 'transitive_pair_unproved', {
                'articles': [articles[u] for u in sorted(a | b)]}))
    output = []
    for cluster in clusters:
        names = [d.get('event_name', '') for p, d in decisions.items() if p <= cluster and d.get('event_name')]
        output.append({'event_name': Counter(names).most_common(1)[0][0] if names else articles[next(iter(cluster))]['title'],
                       'articles': [articles[u] for u in sorted(cluster)], 'article_count': len(cluster)})
    return output, held


def main():
    with open('ai_results.json', encoding='utf-8') as f:
        results = json.load(f)
    groups, held = build_groups(results)
    atomic_save('news_groups_ai.json', groups)
    save_reviews(held)
    print(f'記事グループ {len(groups)}件 / 照合保留 {len(held)}件（単独記事も保持）')


if __name__ == '__main__':
    main()
