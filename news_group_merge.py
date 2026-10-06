import json
import os
import urllib.request

# 最初のAIグループを読み込む
with open("news_groups_ai.json", "r", encoding="utf-8") as f:
    groups = json.load(f)

# グループが1個以下なら、そのまま保存
if len(groups) <= 1:
    with open("news_groups_merged.json", "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

    print(f"{len(groups)}個のニュースグループを保存しました")
    raise SystemExit

# AIに渡す情報を整理
group_summaries = []

for i, group in enumerate(groups):
    articles = []

    for article in group.get("articles", [])[:5]:
        articles.append({
            "title": article.get("title", ""),
            "published": article.get("published", "")
        })

    group_summaries.append({
        "group_id": i,
        "event_name": group.get("event_name", ""),
        "articles": articles
    })

prompt = f"""
以下は、すでに一度まとめられたニュースグループです。

これらの中に、
「別々のニュースに見えるが、実際には同じ出来事の続報」
になっているグループがあれば統合してください。

同じ追跡対象として統合する例:
・事件発生 → 逮捕 → 送検 → 起訴 → 裁判 → 判決
・事故発生 → 救助 → 被害判明 → 原因調査 → 復旧
・大雨警報 → 冠水・浸水 → 被害判明 → 避難 → 復旧
・政策発表 → 審議 → 決定 → 施行 → 制度変更
・企業の計画発表 → 着工 → 開業・完成

単に同じ地域、人物、テーマというだけでは統合しないでください。

ニュースグループ:
{json.dumps(group_summaries, ensure_ascii=False, indent=2)}

統合すべきグループだけを、次のJSON形式で返してください。

{{
  "merge_sets": [
    {{
      "group_ids": [0, 2],
      "event_name": "統合後の短く分かりやすい出来事名"
    }}
  ]
}}

統合するものがなければ、
{{"merge_sets":[]}}
と返してください。

JSON以外は返さないでください。
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

merge_sets = []

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        response_data = json.loads(response.read().decode("utf-8"))

    result_text = response_data["output"][0]["content"][0]["text"].strip()

    if result_text.startswith("```"):
        result_text = result_text.replace("```json", "")
        result_text = result_text.replace("```", "")
        result_text = result_text.strip()

    ai_data = json.loads(result_text)
    merge_sets = ai_data.get("merge_sets", [])

except Exception as e:
    print("グループ統合AI判定エラー:", e)

# Union-Findで統合
parent = list(range(len(groups)))

def find(x):
    if parent[x] != x:
        parent[x] = find(parent[x])
    return parent[x]

def union(a, b):
    root_a = find(a)
    root_b = find(b)

    if root_a != root_b:
        parent[root_b] = root_a

merged_names = {}

for merge_set in merge_sets:
    ids = merge_set.get("group_ids", [])
    event_name = merge_set.get("event_name", "")

    valid_ids = [
        i for i in ids
        if isinstance(i, int) and 0 <= i < len(groups)
    ]

    if len(valid_ids) < 2:
        continue

    first = valid_ids[0]

    for other in valid_ids[1:]:
        union(first, other)

    if event_name:
        merged_names[first] = event_name

# 統合後のグループを作る
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

# AIが付けた統合後の名前を反映
for key in list(merged.keys()):
    root = find(key)

    for original_id, name in merged_names.items():
        if find(original_id) == root:
            merged[key]["event_name"] = name

    merged[key]["article_count"] = len(merged[key]["articles"])

output = list(merged.values())

with open("news_groups_merged.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(
    f"{len(groups)}個のニュースグループを"
    f"{len(output)}個に整理しました"
)
