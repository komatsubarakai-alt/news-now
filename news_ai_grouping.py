import json
from collections import Counter

with open("ai_results.json", "r", encoding="utf-8") as f:
    results = json.load(f)


parent = {}


def find(x):
    if parent[x] != x:
        parent[x] = find(parent[x])
    return parent[x]


def union(a, b):
    root_a = find(a)
    root_b = find(b)

    if root_a != root_b:
        parent[root_b] = root_a


# 記事を登録
for result in results:
    article_a = result["article_a"]
    article_b = result["article_b"]

    url_a = article_a["url"]
    url_b = article_b["url"]

    if url_a not in parent:
        parent[url_a] = url_a

    if url_b not in parent:
        parent[url_b] = url_b


# AIが同じ事件と判断した記事を結合
for result in results:
    article_a = result["article_a"]
    article_b = result["article_b"]

    url_a = article_a["url"]
    url_b = article_b["url"]

    ai_result = result.get("ai_result", "")

    if '"same_event"' in ai_result:
        union(url_a, url_b)


# グループを作成
groups = {}

for url in parent:
    root = find(url)

    if root not in groups:
        groups[root] = []

    groups[root].append(url)


output = []


for group_urls in groups.values():

    articles = []
    event_names = []

    for result in results:

        ai_result = result.get("ai_result", "")

        # AIが返したJSON文字列を読み取る
        try:
            cleaned = ai_result.strip()

            if cleaned.startswith("```"):
                cleaned = cleaned.replace("```json", "")
                cleaned = cleaned.replace("```", "")
                cleaned = cleaned.strip()

            ai_data = json.loads(cleaned)

            if ai_data.get("relation") == "same_event":
                event_name = ai_data.get("event_name", "")

                if event_name:
                    event_names.append(event_name)

        except Exception:
            pass


        for key in ["article_a", "article_b"]:

            article = result[key]

            if article["url"] in group_urls:
                if article not in articles:
                    articles.append(article)


    # もっとも多く出てきた事件名を採用
    if event_names:
        event_name = Counter(event_names).most_common(1)[0][0]
    else:
        event_name = "名称未設定"


    if len(articles) >= 2:
        output.append({
            "event_name": event_name,
            "article_count": len(articles),
            "articles": articles
        })


with open("news_groups_ai.json", "w", encoding="utf-8") as f:
    json.dump(
        output,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"{len(results)}件のAI判定結果から"
    f"{len(output)}個のニュースグループを作りました"
)
