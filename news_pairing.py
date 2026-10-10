import json
import os
from ai_cost_control import response, json_answer, BudgetExceeded, cached

with open("ai_candidates.json", "r", encoding="utf-8") as f:
    candidates = json.load(f)

from merge_safety import IDENTITY_RULES, source_context

results = []
new_calls = 0

for index, candidate in enumerate(candidates):
    prompt = f"""
次の2つのニュース記事が、同じ事件・出来事、または同じ出来事から直接続いている続報かを判定してください。

同じ追跡対象として扱う例：
・事件発生 → 逮捕 → 送検 → 起訴 → 裁判 → 判決
・事故発生 → 救助 → 被害判明 → 原因調査 → 復旧
・大雨警報 → 冠水・浸水 → 被害判明 → 避難 → 復旧
・政策発表 → 審議 → 決定 → 施行 → 制度変更
・企業の計画発表 → 着工 → 開業・完成

単に同じ地域・人物・テーマを扱っているだけの場合は、同じ追跡対象にしないでください。

【記事A】
タイトル: {candidate["article_a"]["title"]}
公開日時: {candidate["article_a"]["published"]}

【記事B】
タイトル: {candidate["article_b"]["title"]}
公開日時: {candidate["article_b"]["published"]}

以下のJSONだけを返してください。
{{
  "relation": "same_event / related / different",
  "event_name": "事件・出来事の短い名前",
  "confidence": "high / medium / low"
}}

same_event = 同じ事件・出来事、またはその出来事から直接続く続報。時間が経過していても、同じ案件の逮捕・起訴・裁判・被害判明・復旧などはsame_event
related = 同じテーマや地域などの関連はあるが、追跡すべき出来事としては別
different = 無関係
"""

    prompt += "\n" + IDENTITY_RULES + "\n" + json.dumps({
        "existing": source_context(candidate["article_a"]),
        "incoming": source_context(candidate["article_b"])
    }, ensure_ascii=False)

    try:
        hit = cached(prompt)
        response_data = response(prompt, allow_network=new_calls < int(os.environ.get('PAIR_CALL_LIMIT', '8')))
        if not hit:
            new_calls += 1
        result_text = json.dumps(json_answer(response_data), ensure_ascii=False)

        results.append({
            "article_a": candidate["article_a"],
            "article_b": candidate["article_b"],
            "ai_result": result_text
        })

    except Exception as e:
        results.append({
            "article_a": candidate["article_a"],
            "article_b": candidate["article_b"],
            "ai_result": json.dumps({"relation": "uncertain", "confidence": "low", "reason": str(e) if isinstance(e, BudgetExceeded) else type(e).__name__})
        })
        print("AI判定を保留:", type(e).__name__)

with open("ai_results.json", "w", encoding="utf-8") as f:
    json.dump(
        results,
        f,
        ensure_ascii=False,
        indent=2
    )

print(f"照合結果 {len(results)}組（キャッシュ・保留を含む） / 新規成功 {new_calls}組")
