import json
import re
from difflib import SequenceMatcher

with open("news_groups_merged.json", "r", encoding="utf-8") as f:
    groups = json.load(f)


def normalize_title(title):
    title = title.lower()

    # 媒体名などが付きやすい末尾をある程度除去
    title = re.sub(r"\s*[-｜|]\s*yahoo!ニュース.*$", "", title)
    title = re.sub(r"\s*[-｜|]\s*dメニューニュース.*$", "", title)

    # 比較の邪魔になる記号や空白を除去
    title = re.sub(r"[【】「」『』（）()\[\]〈〉《》・：:！？!?、。，.\s]", "", title)

    return title


def is_duplicate(article_a, article_b):
    title_a = normalize_title(article_a.get("title", ""))
    title_b = normalize_title(article_b.get("title", ""))

    if not title_a or not title_b:
        return False

    # 完全一致
    if title_a == title_b:
        return True

    # ほぼ同じタイトル
    similarity = SequenceMatcher(
        None,
        title_a,
        title_b
    ).ratio()

    return similarity >= 0.90


output = []

for group in groups:
    articles = group.get("articles", [])
    unique_articles = []

    for article in articles:
        duplicate = False

        for existing in unique_articles:
            if is_duplicate(article, existing):
                duplicate = True
                break

        if not duplicate:
            unique_articles.append(article)

    new_group = dict(group)
    new_group["articles"] = unique_articles
    new_group["article_count"] = len(unique_articles)

    # 元記事数も残しておく
    new_group["source_article_count"] = len(articles)

    output.append(new_group)


with open("news_groups_deduplicated.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)


before = sum(len(group.get("articles", [])) for group in groups)
after = sum(len(group.get("articles", [])) for group in output)

print(
    f"重複整理: {before}記事 → {after}記事 "
    f"({before - after}件を重複として整理)"
)
