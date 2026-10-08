# Legacy writer: the scheduled workflow uses event_tracker.py instead.
# Keep the old source for review, but never execute an unguarded repair/merge.
raise RuntimeError(
    "旧データ更新・掃除経路は停止しています。既存データは変更していません。"
    "点検には python audit_merges.py を使い、付け替え・削除・統合は運営者の確認後に行ってください。"
)

import json
import os
import uuid
from datetime import datetime, timezone


STATUS_FILE = "news_status.json"
EVENTS_FILE = "events.json"


# 今回生成されたニュースを読み込む
with open(STATUS_FILE, "r", encoding="utf-8") as f:
    current_groups = json.load(f)


# 過去の長期追跡データを読み込む
if os.path.exists(EVENTS_FILE):
    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        events = json.load(f)
else:
    events = []


# すでに保存されている記事URL
known_urls = set()

for event in events:
    for article in event.get("articles", []):
        url = article.get("url", "")
        if url:
            known_urls.add(url)


now = datetime.now(timezone.utc).isoformat()


# 今回の出来事を長期保存
for group in current_groups:

    articles = group.get("articles", [])

    # すでに保存済みの記事だけなら今回は追加しない
    new_articles = [
        article
        for article in articles
        if article.get("url", "") not in known_urls
    ]

    if not new_articles:
        continue

    event = {
        "event_id": str(uuid.uuid4()),
        "event_name": group.get("event_name", "名称未設定"),
        "category": group.get("category", "対象外"),
        "tracking_value": group.get("tracking_value", "low"),
        "created_at": now,
        "updated_at": now,
        "current_stage": group.get("current_stage", ""),
        "summary": group.get("summary", ""),
        "latest": group.get("latest", ""),
        "next_watch": group.get("next_watch", ""),
        "article_count": len(articles),
        "source_article_count": group.get(
            "source_article_count",
            len(articles)
        ),
        "articles": articles
    }

    events.append(event)

    for article in articles:
        url = article.get("url", "")
        if url:
            known_urls.add(url)


with open(EVENTS_FILE, "w", encoding="utf-8") as f:
    json.dump(
        events,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"{len(events)}件の出来事を長期追跡データとして保存しました"
)
