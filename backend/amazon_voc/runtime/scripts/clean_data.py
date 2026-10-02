# -*- coding: utf-8 -*-
"""Clean and deduplicate reviews from the single uploaded workbook."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

UI_NOISE_LINES = {
    "why you might like this", "why you might like", "compare with similar",
    "compare with similar items", "more items to explore", "more to explore",
    "product description", "about this item", "frequently bought together",
    "related products", "questions and answers",
}

def clean_reviews(raw_rows):
    cleaned = []
    log = []
    seen_exact = {}
    dup_count = 0
    for i, row in enumerate(raw_rows):
        if not row["asin"] or not row["content"]:
            log.append({"action": "删除空文本", "asin": row["asin"], "reason": "ASIN或评论正文为空", "row": i})
            continue
        content = row["content"]
        # 完全重复（正文 + ASIN + 日期 + 评分）
        key = (row["asin"], content, row["review_date"], row["rating"])
        if key in seen_exact:
            dup_count += 1
            seen_exact[key] += 1
            continue
        seen_exact[key] = 1
        cleaned.append(row)
    if dup_count:
        log.append({"action": "删除完全重复", "reason": "正文+ASIN+日期+评分完全一致", "count": dup_count})

    # 稳定 ID：评{6位}
    reviews = []
    for idx, row in enumerate(cleaned, start=1):
        reviews.append({
            "review_id": f"评{idx:06d}",
            "asin": row["asin"], "title": row["title"], "rank": row["rank"],
            "url": row["url"], "sentiment": row["sentiment"], "rating": row["rating"],
            "review_title": row["review_title"], "content": row["content"],
            "variant": row["variant"], "review_date": row["review_date"],
        })
    return reviews, log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    raw_reviews = common.read_json(common.cache_path(args.workdir, "raw_reviews.json"))

    reviews, rlog = clean_reviews(raw_reviews["rows"])

    clean_log = {
        "summary": {
            "raw_review_count": len(raw_reviews["rows"]),
            "cleaned_review_count": len(reviews),
        },
        "entries": rlog,
    }

    common.write_json(common.cache_path(args.workdir, "cleaned_reviews.json"), {"rows": reviews})
    common.write_json(common.cache_path(args.workdir, "clean_log.json"), clean_log)

    print(f"[clean_data] 评论 {len(raw_reviews['rows'])} → {len(reviews)}（去重/去空）")


if __name__ == "__main__":
    main()
