import json
import re

with open("news.json", "r", encoding="utf-8") as f:
    news = json.load(f)


def get_keywords(title):
    words = re.findall(
        r"[一-龯]{2,}|[ぁ-んァ-ヶ]{3,}",
        title
    )

    stopwords = {
        "札幌", "北海道",
        "ニュース", "ニュー",
        "について", "発表", "明らか",
        "可能性", "関係者",
        "など", "こと", "もの",
        "きょう", "今日", "今月", "今年",
        "市内", "道内", "全国",
        "男性", "女性",
        "人物", "場合",
        "記事", "映像", "写真",
        "問題", "情報"
    }

    keywords = []

    for word in words:
        if word in stopwords:
            continue

        # 2文字の漢字だけの一般語は除外
        if len(word) == 2 and re.fullmatch(r"[一-龯]+", word):
            continue

        keywords.append(word)

    return keywords


candidates = []

for i in range(len(news)):
    keywords_a = set(get_keywords(news[i].get("title", "")))

    if not keywords_a:
        continue

    for j in range(i + 1, len(news)):
        keywords_b = set(get_keywords(news[j].get("title", "")))

        if not keywords_b:
            continue

        common = keywords_a & keywords_b

        # 重要なキーワードが2つ以上共通する記事だけ
        if len(common) >= 2:
            candidates.append({
                "article_a": {
                    "title": news[i].get("title", ""),
                    "url": news[i].get("url", ""),
                    "published": news[i].get("published", "")
                },
                "article_b": {
                    "title": news[j].get("title", ""),
                    "url": news[j].get("url", ""),
                    "published": news[j].get("published", "")
                },
                "common_keywords": sorted(list(common))
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
