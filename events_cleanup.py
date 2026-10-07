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

# 長期追跡に不要なイベントを除外
cleaned_events = [
    event for event in cleaned_events
    if event.get("category") != "対象外"
    and event.get("tracking_value") != "low"
    and len(event.get("articles", [])) > 0
]

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

# 掃除後のイベント同士を比較して重複を探す
merge_candidates = []

for i in range(len(cleaned_events)):
    for j in range(i + 1, len(cleaned_events)):
        event_a = cleaned_events[i]
        event_b = cleaned_events[j]

        # カテゴリーが違うイベントは比較しない
        if event_a.get("category") != event_b.get("category"):
            continue

        merge_candidates.append({
            "event_a": event_a,
            "event_b": event_b
        })

print(
    f"イベント統合候補: {len(merge_candidates)}組"
)

def make_merge_prompt(event_a, event_b):
    return f"""
あなたは「ニュースの現在地」というニュース追跡サービスの編集者です。

次の2つの保存済みイベントが、
実際には同じ出来事・同じ長期追跡テーマなのか判定してください。

単に同じ地域・人物・分野というだけでは統合しないでください。

【イベントA】
名前: {event_a.get("event_name", "")}
現在地: {event_a.get("current_stage", "")}
要約: {event_a.get("summary", "")}

【イベントB】
名前: {event_b.get("event_name", "")}
現在地: {event_b.get("current_stage", "")}
要約: {event_b.get("summary", "")}

以下のJSONだけを返してください。

{{
  "relation": "same_event / different",
  "confidence": "high / medium / low"
}}
"""

def ask_merge_ai(event_a, event_b):
    prompt = make_merge_prompt(event_a, event_b)

    data = json.dumps({
        "model": "gpt-5.4-mini",
        "input": prompt
    }).encode("utf-8")

    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization":
                "Bearer " + os.environ["OPENAI_API_KEY"]
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

merge_results = []

for candidate in merge_candidates:
    event_a = candidate["event_a"]
    event_b = candidate["event_b"]

    try:
        result = ask_merge_ai(event_a, event_b)

        if (
            result.get("relation") == "same_event"
            and result.get("confidence") == "high"
        ):
            merge_results.append({
                "event_a_id": event_a.get("event_id", ""),
                "event_b_id": event_b.get("event_id", ""),
                "event_a_name": event_a.get("event_name", ""),
                "event_b_name": event_b.get("event_name", "")
            })

    except Exception as e:
        print("イベント統合判定エラー:", e)

print(
    f"統合候補として確定: {len(merge_results)}組"
)

with open(
    "event_merge_preview.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        merge_results,
        f,
        ensure_ascii=False,
        indent=2
    )

print(
    "統合候補を event_merge_preview.json "
    "に保存しました"
)

# 統合候補を反映した安全なプレビューを作る
def merge_event_articles(event_a, event_b):
    unique_articles = []
    seen_urls = set()
    seen_titles = []

    for article in (
        event_a.get("articles", []) +
        event_b.get("articles", [])
    ):
        url = article.get("url", "")
        title = normalize_title(
            article.get("title", "")
        )

        # 同じURLの記事は1つだけ残す
        if url and url in seen_urls:
            continue

        # タイトルがかなり似ている記事も1つだけ残す
        if title and any(
            title_similarity(
                title,
                seen_title
            ) >= 0.85
            for seen_title in seen_titles
        ):
            continue

        if url:
            seen_urls.add(url)

        if title:
            seen_titles.append(title)

        unique_articles.append(article)

    return unique_articles


events_by_id = {
    event.get("event_id", ""): event.copy()
    for event in cleaned_events
    if event.get("event_id")
}

removed_event_ids = set()

for result in merge_results:
    event_a_id = result.get("event_a_id", "")
    event_b_id = result.get("event_b_id", "")

    if (
        not event_a_id
        or not event_b_id
        or event_a_id in removed_event_ids
        or event_b_id in removed_event_ids
        or event_a_id not in events_by_id
        or event_b_id not in events_by_id
    ):
        continue

    event_a = events_by_id[event_a_id]
    event_b = events_by_id[event_b_id]

    # 古くから追跡している方のevent_idを残す
    a_created = event_a.get("created_at", "")
    b_created = event_b.get("created_at", "")

    if (
        b_created
        and (
            not a_created
            or b_created < a_created
        )
    ):
        event_a, event_b = event_b, event_a
        event_a_id, event_b_id = (
            event_b_id,
            event_a_id
        )

    merged_articles = merge_event_articles(
        event_a,
        event_b
    )

    # 要約などは、より新しく更新された側を優先
    a_updated = event_a.get("updated_at", "")
    b_updated = event_b.get("updated_at", "")

    newer_event = (
        event_b
        if b_updated > a_updated
        else event_a
    )

    merged_event = newer_event.copy()

    merged_event["event_id"] = (
        event_a.get("event_id", "")
    )

    merged_event["created_at"] = (
        event_a.get("created_at", "")
    )

    merged_event["updated_at"] = max(
        a_updated,
        b_updated
    )

    merged_event["articles"] = merged_articles

    merged_event["article_count"] = len(
        merged_articles
    )

    # 重複イベントなので報道数を単純加算しない
    merged_event["source_article_count"] = len(
        merged_articles
    )

    events_by_id[event_a_id] = merged_event
    removed_event_ids.add(event_b_id)


events_merged_preview = [
    event
    for event_id, event in events_by_id.items()
    if event_id not in removed_event_ids
]


with open(
    "events_merged_preview.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(
        events_merged_preview,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"統合後プレビュー: "
    f"{len(cleaned_events)}件 → "
    f"{len(events_merged_preview)}件"
)

print(
    "統合結果を events_merged_preview.json "
    "に保存しました"
)

# 統合後プレビューの安全チェック
validation_errors = []

event_ids = set()

for event in events_merged_preview:
    event_id = event.get("event_id", "")
    articles = event.get("articles", [])

    # event_idがない
    if not event_id:
        validation_errors.append(
            "event_idがないイベントがあります"
        )

    # event_idが重複している
    elif event_id in event_ids:
        validation_errors.append(
            f"event_id重複: {event_id}"
        )
    else:
        event_ids.add(event_id)

    # 記事が0件
    if len(articles) == 0:
        validation_errors.append(
            f"記事0件: {event_id}"
        )

    # 対象外が残っている
    if event.get("category") == "対象外":
        validation_errors.append(
            f"対象外イベントが残っています: {event_id}"
        )

    # lowが残っている
    if event.get("tracking_value") == "low":
        validation_errors.append(
            f"lowイベントが残っています: {event_id}"
        )

    # article_countと実際の記事数が違う
    if event.get("article_count") != len(articles):
        validation_errors.append(
            f"記事数不一致: {event_id}"
        )


if validation_errors:
    print("統合後データの安全チェックで問題を検出しました")

    for error in validation_errors:
        print("-", error)

    raise RuntimeError(
        "events_merged_preview.json の検証に失敗しました"
    )


print(
    f"安全チェック成功: "
    f"{len(events_merged_preview)}イベント"
)

print(
    "events.json はまだ変更していません"
)
