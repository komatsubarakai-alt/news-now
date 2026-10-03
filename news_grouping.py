import json
import re

with open("news.json", "r", encoding="utf-8") as f:
    news = json.load(f)

groups = {}

def get_keywords(title):
    words = re.findall(
        r"[一-龯]{2,}|[ぁ-ん]{2,}|[ァ-ヶ]{2,}",
        title
    )

    stopwords = {
        "札幌", "北海道", "ニュース", "について",
        "発表", "明らか", "可能性", "関係者",
        "など", "こと", "もの", "きょう",
        "今日", "今月", "今年"
    }

    return [
        word for word in words
        if word not in stopwords
    ]

for article in news:
    title = article.get("title", "")
    keywords = get_keywords(title)

    if not keywords:
        key = "その他"
    else:
        key = "-".join(keywords[:4])

    # 既存グループとの共通キーワードを確認
    best_group = None
    best_score = 0

    for group_key, group_articles in groups.items():
        group_keywords = set()

        for old_article in group_articles:
            old_title = old_article.get("title", "")
            group_keywords.update(get_keywords(old_title))

        score = len(set(keywords) & group_keywords)

        if score > best_score:
            best_score = score
            best_group = group_key

    # 2語以上共通している場合は同じグループにする
    if best_group and best_score >= 2:
        groups[best_group].append(article)
    else:
        groups[key] = [article]

with open("news_groups.json", "w", encoding="utf-8") as f:
    json.dump(
        groups,
        f,
        ensure_ascii=False,
        indent=2
    )

print(
    f"{len(news)}件のニュースを"
    f"{len(groups)}グループに整理しました"
)
