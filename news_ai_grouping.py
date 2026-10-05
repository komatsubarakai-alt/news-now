import json

with open("ai_results.json", "r", encoding="utf-8") as f:
    results = json.load(f)


# 記事をグループ化するための仕組み
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


# AIが「同じ事件」と判断した記事を結合
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


# 分かりやすい形に整理
output = []

for group_urls in groups.values():

    articles = []

    for result in results:
        for key in ["article_a", "article_b"]:

            article = result[key]

            if article["url"] in group_urls:
                if article not in articles:
                    articles.append(article)

    if len(articles) >= 2:
        output.append({
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
