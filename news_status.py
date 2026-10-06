import json
import os
import urllib.request


with open("news_groups_ai.json", "r", encoding="utf-8") as f:
    groups = json.load(f)


statuses = []
VALID_CATEGORIES = {
    "事件・事故",
    "災害",
    "政策・制度",
    "企業・組織",
    "地域・インフラ",
    "対象外"
}


def normalize_category(category):
    category = category.strip()

    if category in VALID_CATEGORIES:
        return category

    if "インフラ" in category or "都市" in category or "再開発" in category:
        return "地域・インフラ"

    if "政策" in category or "制度" in category or "行政" in category:
        return "政策・制度"

    if "企業" in category or "組織" in category:
        return "企業・組織"

    if "災害" in category:
        return "災害"

    if "事件" in category or "事故" in category:
        return "事件・事故"

    return "対象外"


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

カテゴリーは必ず次の6つのうち1つだけを選んでください。

・事件・事故
  犯罪、逮捕、捜査、裁判、交通事故、火災など

・災害
  地震、津波、大雨、洪水、土砂災害、大雪など

・政策・制度
  法律、条例、行政制度、予算、支援策、規制など
  政治家個人ではなく、政策や制度そのものを対象にする

・企業・組織
  企業や団体の重要な動き、不祥事、経営、事業など

・地域・インフラ
  再開発、交通、道路、鉄道、公共施設、都市計画など

・対象外
  芸能、スポーツ、イベント、グルメなど、
  基本的に継続追跡する必要がないニュース

追跡価値は次の3段階で判定してください。

high = 今後重要な続報が出る可能性が高い
medium = 続報が出る可能性がある
low = 単発ニュースで終わる可能性が高い

「次に注目」は、記事から合理的に予想できる
具体的な次の動きがある場合だけ書いてください。
無理に予想せず、分からない場合は
「現時点で明確な次の動きは確認できません」
としてください。

{{
  "category": "事件・事故 / 災害 / 政策・制度 / 企業・組織 / 地域・インフラ / 対象外",
  "tracking_value": "high / medium / low",
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
                        "category":
    normalize_category(
        ai_status.get("category", "対象外")
    ),
            "tracking_value":
                ai_status.get("tracking_value", "low"),
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
