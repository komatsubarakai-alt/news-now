import json
import re
from itertools import combinations

with open("news.json", "r", encoding="utf-8") as f:
    news = json.load(f)


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
    if score < 2:
        continue

    candidates.append({
        "article_a": a["article"],
        "article_b": b["article"],
        "common_keywords": sorted(common),
        "score": score
    })


# AIに送る候補を重要度順に並べる
candidates.sort(
    key=lambda x: x["score"],
    reverse=True
)


# 多すぎる場合は上位100組まで
MAX_CANDIDATES = 100

candidates = candidates[:MAX_CANDIDATES]


with open("ai_candidates.json", "w", encoding="utf-8") as f:
    json.dump(
        candidates,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"{len(news)}件のニュースから"
    f"{len(candidates)}組のAI判定候補を作りました"
)
