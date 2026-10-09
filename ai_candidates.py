import json
import re
from itertools import combinations

from pathlib import Path


# よく出てくるだけで、事件の判定にはあまり役立たない言葉
STOPWORDS = {
    "札幌", "北海道", "ニュース", "発表", "明らか",
    "関係者", "きょう", "今日", "昨日", "今月", "今年",
    "市内", "道内", "市", "道", "県",
    "男性", "女性", "人", "方", "問題",
    "について", "めぐり", "など", "こと"
}


def get_keywords(title):
    words = re.findall(
        r"[一-龯]{2,}|[ぁ-ん]{2,}|[ァ-ヶ]{2,}|[A-Za-z0-9]{2,}",
        title
    )

    return [
        word for word in words
        if word not in STOPWORDS
    ]


def build_candidates(news):
    # 各記事のキーワードを作る
    article_keywords = []

    for article in news:
        title = article.get("title", "")
        keywords = get_keywords(title)

        article_keywords.append({
            "article": article,
            "keywords": set(keywords)
        })


    candidates = []


    # 記事同士を比較
    for i, j in combinations(range(len(article_keywords)), 2):

        a = article_keywords[i]
        b = article_keywords[j]

        common = a["keywords"] & b["keywords"]

        # 共通キーワードがない場合は比較しない
        if not common:
            continue

        # 共通キーワードの重要度を計算
        score = 0

        for word in common:
            # 長い言葉ほど重要と判断
            if len(word) >= 5:
                score += 3
            elif len(word) >= 4:
                score += 2
            else:
                score += 1

        # 共通語が1つだけでも、長い固有性の高い言葉なら候補にする
        if score < 1:
            continue

        candidates.append({
            "article_a": a["article"],
            "article_b": b["article"],
            "common_keywords": sorted(common),
            "score": score
        })


    # 各記事から有力な候補を均等に残す
    candidates_by_article = {}

    for candidate in candidates:
        url_a = candidate["article_a"].get("url", "")
        url_b = candidate["article_b"].get("url", "")

        candidates_by_article.setdefault(url_a, []).append(candidate)
        candidates_by_article.setdefault(url_b, []).append(candidate)


    selected = []
    seen_pairs = set()

    # 各記事につき上位3候補まで残す
    for article_candidates in candidates_by_article.values():

        article_candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        for candidate in article_candidates[:3]:

            url_a = candidate["article_a"].get("url", "")
            url_b = candidate["article_b"].get("url", "")

            pair_key = tuple(sorted([url_a, url_b]))

            if pair_key not in seen_pairs:
                seen_pairs.add(pair_key)
                selected.append(candidate)


    # 最終的な候補数を抑える
    selected.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    MAX_CANDIDATES = 150

    candidates = selected[:MAX_CANDIDATES]
    return candidates




def main():
    # Raw collection is retained; only this run's verified representatives enter AI.
    path = Path('news_filtered.json') if Path('news_filtered.json').exists() else Path('news.json')
    news = json.loads(path.read_text(encoding='utf-8'))
    candidates = build_candidates(news)
    Path('ai_candidates.json').write_text(json.dumps(candidates, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(f'{len(news)}件のニュースから{len(candidates)}組のAI判定候補を作りました')


if __name__ == '__main__':
    main()
