import json
import os
import urllib.request
from datetime import datetime, timezone


# 過去から保存している出来事
with open("events.json", "r", encoding="utf-8") as f:
    events = json.load(f)


# 今回のニュース収集で作られた出来事
with open("news_status.json", "r", encoding="utf-8") as f:
    new_groups = json.load(f)


print(f"保存済みイベント: {len(events)}件")
print(f"今回のイベント: {len(new_groups)}件")

def make_event_text(event):
    titles = []

    for article in event.get("articles", [])[:5]:
        title = article.get("title", "")
        if title:
            titles.append(title)

    return {
        "event_id": event.get("event_id", ""),
        "event_name": event.get("event_name", ""),
        "category": event.get("category", ""),
        "current_stage": event.get("current_stage", ""),
        "summary": event.get("summary", ""),
        "article_titles": titles
    }


saved_event_data = [
    make_event_text(event)
    for event in events
]

print(
    f"続報照合用に{len(saved_event_data)}件の"
    "保存済みイベントを準備しました"
)

def make_tracking_prompt(new_group):
    new_titles = []

    for article in new_group.get("articles", [])[:5]:
        title = article.get("title", "")
        if title:
            new_titles.append(title)

    return f"""
あなたは「ニュースの現在地」というニュース追跡サービスの編集者です。

今回見つかった出来事が、保存済みイベントの続報なのか、
それとも新しい別の出来事なのかを判定してください。

単に同じ地域・人物・テーマというだけでは続報にしないでください。

続報として扱う例:
・事件発生 → 逮捕 → 送検 → 起訴 → 裁判 → 判決
・事故発生 → 被害判明 → 原因調査 → 復旧
・大雨警報 → 冠水・被害 → 復旧
・政策発表 → 審議 → 決定 → 施行
・施設計画 → 議論 → 着工 → 開業

【保存済みイベント】
{json.dumps(saved_event_data, ensure_ascii=False)}

【今回の出来事】
出来事名: {new_group.get("event_name", "")}
カテゴリー: {new_group.get("category", "")}
現在地: {new_group.get("current_stage", "")}
要約: {new_group.get("summary", "")}
記事タイトル:
{json.dumps(new_titles, ensure_ascii=False)}

以下のJSONだけを返してください。

{{
  "relation": "follow_up / new_event",
  "event_id": "続報の場合は保存済みevent_id、新規なら空文字",
  "confidence": "high / medium / low"
}}
"""

def ask_ai(new_group):
    prompt = make_tracking_prompt(new_group)

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

now = datetime.now(timezone.utc).isoformat()

event_by_id = {
    event.get("event_id"): event
    for event in events
    if event.get("event_id")
}


for new_group in new_groups:

    # すでに保存済みの記事しかない場合はAI判定しない
    saved_urls = {
        article.get("url", "")
        for event in events
        for article in event.get("articles", [])
        if article.get("url")
    }

    new_articles = [
        article
        for article in new_group.get("articles", [])
        if article.get("url", "") not in saved_urls
    ]

    if not new_articles:
        continue

    try:
        result = ask_ai(new_group)
    except Exception as e:
        print("続報判定エラー:", e)
        continue

    relation = result.get("relation", "")
    event_id = result.get("event_id", "")
    confidence = result.get("confidence", "")

    # high confidence の続報だけ既存イベントへ追加
    if (
        relation == "follow_up"
        and confidence == "high"
        and event_id in event_by_id
    ):
        event = event_by_id[event_id]

        existing_urls = {
            article.get("url", "")
            for article in event.get("articles", [])
        }

        for article in new_articles:
            if article.get("url", "") not in existing_urls:
                event.setdefault("articles", []).append(article)

        # 現在地を最新情報へ更新
        event["event_name"] = new_group.get(
            "event_name",
            event.get("event_name", "")
        )
        event["category"] = new_group.get(
            "category",
            event.get("category", "")
        )
        event["tracking_value"] = new_group.get(
            "tracking_value",
            event.get("tracking_value", "")
        )
        event["current_stage"] = new_group.get(
            "current_stage",
            event.get("current_stage", "")
        )
        event["summary"] = new_group.get(
            "summary",
            event.get("summary", "")
        )
        event["latest"] = new_group.get(
            "latest",
            event.get("latest", "")
        )
        event["next_watch"] = new_group.get(
            "next_watch",
            event.get("next_watch", "")
        )

        event["article_count"] = len(
            event.get("articles", [])
        )
               
        event["source_article_count"] = max(
            event.get("source_article_count", 0),
            event["article_count"]
                )
        
        event["updated_at"] = now

        print(
            "続報を追加:",
            event.get("event_name", "")
        )

    else:
        # 続報と確信できない場合は、新しいイベントとして登録
        import uuid

        new_event = {
            "event_id": str(uuid.uuid4()),
            "event_name": new_group.get(
                "event_name",
                "名称未設定"
            ),
            "category": new_group.get(
                "category",
                "対象外"
            ),
            "tracking_value": new_group.get(
                "tracking_value",
                "low"
            ),
            "created_at": now,
            "updated_at": now,
            "current_stage": new_group.get(
                "current_stage",
                ""
            ),
            "summary": new_group.get(
                "summary",
                ""
            ),
            "latest": new_group.get(
                "latest",
                ""
            ),
            "next_watch": new_group.get(
                "next_watch",
                ""
            ),
            "article_count": len(new_articles),
            "source_article_count": new_group.get(
                "source_article_count",
                len(new_articles)
            ),
            "articles": new_articles
        }

        events.append(new_event)
        event_by_id[new_event["event_id"]] = new_event

        print(
            "新しいイベントを登録:",
            new_event["event_name"]
        )


# 更新した長期追跡データを保存
with open("events.json", "w", encoding="utf-8") as f:
    json.dump(
        events,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"長期追跡データを更新しました: {len(events)}件"
)
