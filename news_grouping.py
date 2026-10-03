import json
import re

with open("news.json", "r", encoding="utf-8") as f:
    news = json.load(f)


def get_keywords(title):
    words = re.findall(
        r"[一-龯]{2,}|[ぁ-ん]{2,}|[ァ-ヶ]{2,}",
        title
    )

    stopwords = {
        "札幌", "北海道", "ニュース", "について",
        "発表", "明らか", "可能性", "関係者",
        "など", "こと", "もの",
        "きょう", "今日", "今月", "今年"
    }

    return [
        word for word in words
        if word not in stopwords
    ]


# AIに確認してもらう候補を作る
candidates = []

for i in range(len(news)):
    for j in range(i + 1, len(news)):

        title_a = news[i].get("title", "")
        title_b = news[j].get("title", "")

        keywords_a = set(get_keywords(title_a))
        keywords_b = set(get_keywords(title_b))

        common = keywords_a & keywords_b

        # 共通キーワードが1つ以上ある記事だけ
        # AIによる判定候補にする
        if len(common) >= 1:
            candidates.append({
                "article_a": {
                    "title": title_a,
                    "url": news[i].get("url", ""),
                    "published": news[i].get("published", "")
                },
                "article_b": {
                    "title": title_b,
                    "url": news[j].get("url", ""),
                    "published": news[j].get("published", "")
                },
                "common_keywords": list(common)
            })


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
