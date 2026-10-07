import json
import os
import urllib.request
from itertools import combinations

with open("news_groups_ai.json", "r", encoding="utf-8") as f:
    groups = json.load(f)

parent = list(range(len(groups)))
merge_names = {}


def find(x):
    if parent[x] != x:
        parent[x] = find(parent[x])
    return parent[x]


def union(a, b):
    root_a = find(a)
    root_b = find(b)

    if root_a != root_b:
        parent[root_b] = root_a


def group_text(group):
    articles = []

    for article in group.get("articles", [])[:5]:
        articles.append({
            "title": article.get("title", ""),
            "published": article.get("published", "")
        })

    return {
        "event_name": group.get("event_name", ""),
        "articles": articles
    }


def ask_ai(group_a, group_b):
    prompt = f"""
あなたは「ニュースの現在地」というニュース追跡サービスの編集者です。

次の2つのニュースグループが、
「同じ1つの出来事の進展・続報」なのかを厳しく判定してください。

【グループA】
{json.dumps(group_text(group_a), ensure_ascii=False, indent=2)}

【グループB】
{json.dumps(group_text(group_b), ensure_ascii=False, indent=2)}

判定ルール:

・同じ地域、同じ人物、日時が近い、同じ単語があるというだけでは統合しません。

・前の記事から後の記事へ、
同じ出来事そのものが進展している場合だけ same_event にしてください。

same_event の例:
・事件発生 → 逮捕 → 送検 → 起訴 → 裁判 → 判決
・事故発生 → 救助 → 原因調査 → 復旧
・大雨警報 → 同じ大雨による冠水・浸水 → 避難 → 復旧
・政策発表 → 審議 → 決定 → 施行
・施設計画 → 着工 → 完成 → 開業

different の例:
・札幌の大雨 と、札幌で起きた無関係な交通事故
・大雨の後に別の設備事故が新しく発生したケース
・同じ会社で起きた別々の問題
・同じ人物が関係する別々の事件

重要:
ある出来事が別の事件・事故の「原因」や「背景」になっていても、
新しい独立した事件・事故が発生した場合は different にしてください。

判断に迷う場合も different にしてください。

次のJSONだけを返してください。

{{
  "relation": "same_event または different",
  "event_name": "same_eventの場合のみ統合後の短い出来事名",
  "confidence": "high / medium / low"
}}
"""

    data = json.dumps({
        "model": "gpt-5.4-mini",
        "input": prompt
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_data = json.loads(
                response.read().decode("utf-8")
            )

        text = response_data["output"][0]["content"][0]["text"].strip()

        if text.startswith("```"):
            text = text.replace("```json", "")
            text = text.replace("```", "")
            text = text.strip()

        return json.loads(text)

    except Exception as e:
        print("グループ比較エラー:", e)
        return {
            "relation": "different",
            "event_name": "",
            "confidence": "low"
        }


# グループ同士を1対1で比較
for i, j in combinations(range(len(groups)), 2):

    result = ask_ai(groups[i], groups[j])

    # 誤統合を防ぐため high の same_event だけ統合
    if (
        result.get("relation") == "same_event"
        and result.get("confidence") == "high"
    ):
        union(i, j)

        event_name = result.get("event_name", "")

        if event_name:
            merge_names[i] = event_name


# 統合後のグループを作成
merged = {}

for i, group in enumerate(groups):
    root = find(i)

    if root not in merged:
        merged[root] = {
            "event_name": group.get("event_name", ""),
            "article_count": 0,
            "articles": []
        }

    for article in group.get("articles", []):
        url = article.get("url", "")

        if not any(
            existing.get("url", "") == url
            for existing in merged[root]["articles"]
        ):
            merged[root]["articles"].append(article)


# AIが付けた統合名を反映
for original_id, name in merge_names.items():
    root = find(original_id)

    if root in merged and name:
        merged[root]["event_name"] = name


output = list(merged.values())

for group in output:
    group["article_count"] = len(group["articles"])


with open("news_groups_merged.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)


print(
    f"{len(groups)}個のニュースグループを"
    f"{len(output)}個に整理しました"
)
