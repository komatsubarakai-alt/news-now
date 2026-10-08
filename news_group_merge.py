"""Group merges need source evidence for every cross-pair, never transitive union alone."""
import copy
import json
from itertools import combinations
from merge_safety import IDENTITY_RULES, UpdateHeld, review, save_reviews, source_context, validate_identity, contradictory, article_content
from news_ai_grouping import negative_pairs
from tracking_sources import AI
from tracking_v1 import atomic_save, deduplicate


def group_text(group):
    return {'event_name': group.get('event_name', ''),
            'articles': [source_context(a) for a in group.get('articles', [])]}


def ask_ai(ai, group_a, group_b):
    return ai.ask('2グループは同一案件の直接続報か。単なる原因・背景・地域の一致は不可。'
                  'JSON {"relation":"same_event/different/uncertain","confidence":"high/medium/low",'
                  '"event_name":"案件名","identity_matches":[]}。グループAを既存、Bを新記事として照合。\n'
                  + IDENTITY_RULES + '\n' + json.dumps({'A': group_text(group_a), 'B': group_text(group_b)}, ensure_ascii=False))


def merge_groups(groups, judge, blocked_pairs=None):
    blocked_pairs = blocked_pairs or set()
    decisions, held = {}, []
    for i, j in combinations(range(len(groups)), 2):
        try:
            if any(frozenset((a['url'], b['url'])) in blocked_pairs for a in groups[i]['articles'] for b in groups[j]['articles']):
                raise UpdateHeld('explicit_pair_conflict')
            result = judge(groups[i], groups[j])
            if result.get('relation') == 'same_event':
                validate_identity(groups[i]['articles'], groups[j]['articles'], result)
                # No article may be admitted through a contaminated representative.
                for article in groups[j]['articles']:
                    if contradictory(groups[i].get('event_name', ''), article_content(article)):
                        raise UpdateHeld('group_name_article_contradiction')
                    for prior in groups[i]['articles']:
                        if contradictory(article_content(prior), article_content(article)):
                            raise UpdateHeld('group_article_contradiction')
                decisions[(i, j)] = result
            elif result.get('relation') != 'different' or result.get('confidence') != 'high':
                held.append(review('group_merge', 'uncertain_groups', {'groups': [groups[i], groups[j]]}))
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError) as exc:
            held.append(review('group_merge', str(exc), {'groups': [groups[i], groups[j]]}))
    clusters = [{i} for i in range(len(groups))]
    for (i, j), result in decisions.items():
        left = next(c for c in clusters if i in c); right = next(c for c in clusters if j in c)
        if left is right:
            continue
        if all(tuple(sorted((a, b))) in decisions for a in left for b in right):
            left.update(right); clusters.remove(right)
        else:
            held.append(review('group_merge', 'transitive_group_unproved', {'group_indexes': sorted(left | right)}))
    output = []
    for cluster in clusters:
        seed = min(cluster)
        # Preserve the established seed name; AI merge labels are not authoritative facts.
        combined = copy.deepcopy(groups[seed])
        combined['articles'] = deduplicate([a for i in sorted(cluster) for a in groups[i]['articles']])
        combined['article_count'] = len(combined['articles'])
        output.append(combined)
    return output, held


def main():
    with open('news_groups_ai.json', encoding='utf-8') as f:
        groups = json.load(f)
    # Bound this stage to 100 calls; the previous group merger was unbounded.
    ai = AI(limit=100)
    with open('ai_results.json', encoding='utf-8') as f:
        blocked = negative_pairs(json.load(f))
    output, held = merge_groups(groups, lambda a, b: ask_ai(ai, a, b), blocked)
    atomic_save('news_groups_merged.json', output)
    save_reviews(held)
    print(f'グループ {len(groups)}→{len(output)}件 / 統合保留 {len(held)}件')


if __name__ == '__main__':
    main()
