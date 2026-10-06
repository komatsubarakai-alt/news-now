import json
import os
import urllib.request


with open("news_groups_ai.json", "r", encoding="utf-8") as f:
    groups = json.load(f)


statuses = []


for group in groups:

    event_name = group.get("event_name", "名称未設定")
    articles = group.get("articles", [])

    article_text = ""

    for article in articles:
        article_text += (
            f"タイトル: {article.get('title', '')}\n"
            f"公開日時: {article.get('published', '')}\n\n"
        )

    prompt = f"""
あなたは「ニュースの現在地」というニュース追跡サービスの編集者です。

以下は同じ事件・出来事について報じたニュース記事です。
記事タイトルと公開日時から確認できる範囲だけを使って、
この出来事の現在の状況を整理してください。

出来事名:
{event_name}

関連記事:
{article_text}

推測で事実を補わないでください。
分からないことは断定しないでください。

以下のJSONだけを返してください。

{{
  "current_stage": "現在の段階を短い言葉で",
  "summary": "現在までに分かっていることを簡潔に",
  "latest": "最新の動き",
  "next_watch": "今後注目すべきこと"
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
            "Authorization":
                "Bearer " + os.environ["OPENAI_API_KEY"]
        },
        method="POST"
    )

    try:

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

        ai_status = json.loads(cleaned)

        statuses.append({
            "event_name": event_name,
            "article_count": len(articles),
            "current_stage":
                ai_status.get("current_stage", ""),
            "summary":
                ai_status.get("summary", ""),
            "latest":
                ai_status.get("latest", ""),
            "next_watch":
                ai_status.get("next_watch", ""),
            "articles": articles
        })

    except Exception as e:

        print(
            f"現在地生成エラー: {event_name}: {e}"
        )


with open(
    "news_status.json",
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        statuses,
        f,
        ensure_ascii=False,
        indent=2
    )


print(
    f"{len(statuses)}件のニュースの現在地を作りました"
)
