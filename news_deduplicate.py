"""Apply the same conservative source policy after grouping; keep all exclusions."""
import copy
import json
from pathlib import Path
from source_filter import prefilter, record_exclusions
from tracking_v1 import atomic_save


def deduplicate_groups(groups):
    output, exclusions = [], []
    for group in groups:
        articles = group.get('articles', [])
        kept, removed = prefilter(articles)
        result = copy.deepcopy(group)
        result['articles'] = kept
        result['article_count'] = len(kept)
        result['source_article_count'] = len(articles)
        output.append(result)
        exclusions.extend(removed)
    return output, exclusions


def main():
    groups = json.loads(Path('news_groups_merged.json').read_text(encoding='utf-8'))
    output, exclusions = deduplicate_groups(groups)
    record_exclusions(exclusions, 'post_grouping')
    atomic_save('news_groups_deduplicated.json', output)
    before = sum(len(group.get('articles', [])) for group in groups)
    after = sum(len(group.get('articles', [])) for group in output)
    print(f'重複整理: {before}記事 → {after}記事（原記事と除外理由を別途保持）')


if __name__ == '__main__':
    main()
