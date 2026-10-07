import json
import os
import urllib.request
import re


# 現在保存されている長期追跡イベントを読み込む
with open("events.json", "r", encoding="utf-8") as f:
    events = json.load(f)


print(f"掃除対象のイベント: {len(events)}件")

def normalize_title(title):
    title = title.lower()

    # 記号や空白を取り除く
    title = re.sub(
        r"[\s　【】\[\]（）()「」『』・\-—―…!！?？:：]",
        "",
        title
    )

    # よく付く媒体表記などを除く
    words_to_remove = [
        "yahooニュース",
        "yahoo!ニュース",
        "速報",
    ]

    for word in words_to_remove:
        title = title.replace(word, "")

    return title

def title_similarity(title_a, title_b):
    a = set(normalize_title(title_a))
    b = set(normalize_title(title_b))

    if not a or not b:
        return 0

    common = len(a & b)
    base = min(len(a), len(b))

    return common / base

def make_event_context(event):
    return {
        "event_id": event.get("event_id", ""),
        "event_name": event.get("event_name", ""),
        "category": event.get("category", ""),
        "current_stage": event.get("current_stage", ""),
        "summary": event.get("summary", "")
    }

def make_cleanup_prompt(event, article):
    context = make_event_context(event)

    return f"""
あなたは「ニュースの現在地」というニュース追跡サービスの編集者です。

保存済みイベントに含まれている記事が、
本当にそのイベントそのもの、または直接の続報なのかを判定してください。

単に同じ地域、同じ時期、同じ人物、似たテーマというだけでは
同じイベントとして扱わないでください。

同じイベントとして残す例:
・事件発生 → 逮捕 → 送検 → 起訴 → 裁判 → 判決
・事故発生 → 被害判明 → 原因調査 → 復旧
・大雨警報 → 冠水・被害 → 復旧
・政策発表 → 審議 → 決定 → 施行
・施設計画 → 議論 → 着工 → 開業

【保存済みイベント】
{json.dumps(context, ensure_ascii=False)}

【判定する記事】
タイトル: {article.get("title", "")}
公開日時: {article.get("published", "")}

以下のJSONだけを返してください。

{{
  "relation": "keep / remove",
  "confidence": "high / medium / low"
}}

keep = このイベントそのもの、または直接の続報
remove = 別の出来事
"""

def ask_cleanup_ai(event, article):
    prompt = make_cleanup_prompt(event, article)

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

    with urllib.request.urlopen(
        request,
        timeout=60
    ) as response:
        response_data = json.loads(
            response.read().decode("utf-8")
        )

    result_text = (
        response_data["output"][0]
        ["content"][0]["text"]
    )

    cleaned = result_text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "")
        cleaned = cleaned.replace("```", "")
        cleaned = cleaned.strip()

    return json.loads(cleaned)

cleaned_events = []

for event in events:
    kept_articles = []
    removed_articles = []

    for article in event.get("articles", []):
        try:
            result = ask_cleanup_ai(event, article)

            relation = result.get("relation", "")
            confidence = result.get("confidence", "")

            if (
                relation == "remove"
                and confidence == "high"
            ):
                removed_articles.append(article)
            else:
                kept_articles.append(article)

        except Exception as e:
            print("記事判定エラー:", e)

            # エラー時は安全のため記事を残す
            kept_articles.append(article)

            # 同じURL・同じタイトルの記事を重複除去
    unique_articles = []
    seen_urls = set()
    seen_titles = set()

    for article in kept_articles:
        url = article.get("url", "")
        title = normalize_title(
            article.get("title", "")
        )

        # URLが同じなら重複
        if url and url in seen_urls:
            continue

        # タイトルが同じ、またはかなり似ているなら重複
        is_similar = False

        for seen_title in seen_titles:
        if title_similarity(title, seen_title) >= 0.85:
            is_similar = True
            break

        if title and is_similar:
        continue

        if url:
            seen_urls.add(url)

        if title:
            seen_titles.add(title)

        unique_articles.append(article)

    cleaned_event = event.copy()
    cleaned_event["articles"] = unique_articles
    cleaned_event["article_count"] = len(unique_articles)

    # 元報道数が整理後の記事数を下回らないようにする
    cleaned_event["source_article_count"] = len(unique_articles)

    cleaned_events.append(cleaned_event)

    print(
        event.get("event_name", "名称未設定"),
        "残す:",
        len(kept_articles),
        "除外:",
        len(removed_articles)
    )

with open(
    "events_cleaned_preview.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        cleaned_events,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    "掃除結果を events_cleaned_preview.json "
    "に保存しました"
)
