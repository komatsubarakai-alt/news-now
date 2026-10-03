import json
import re

with open("news.json", "r", encoding="utf-8") as f:
    news = json.load(f)

groups = {}

for article in news:
    title = article.get("title", "")

    words = re.findall(
        r"[一-龯]{2,}|[ぁ-ん]{2,}|[ァ-ヶ]{2,}",
        title
    )

　　key = "-".join(words[:3]) if words else "その他"

    if key not in groups:
        groups[key] = []

    groups[key].append(article)

with open("news_groups.json", "w", encoding="utf-8") as f:
    json.dump(
        groups,
        f,
        ensure_ascii=False,
        indent=2
    )

print(
    f"{len(news)}件のニュースを"
    f"{len(groups)}グループに整理しました"
)
