#!/usr/bin/env python3
"""按跨品类标签优先级确定流量性质，并规范购买意图。"""
import argparse
import json
import os

AUDIENCE_PREFIXES = {"audience"}
ATTRIBUTE_PREFIXES = {"material", "function", "spec", "bundle", "accessory"}
SCENE_PREFIXES = {"scene", "gift"}
INTENT_CN = {"high": "高", "medium": "中", "mid": "中", "low": "低", "高": "高", "中": "中", "低": "低"}


def prefixes(tags):
    return {str(t).split(":", 1)[0] for t in (tags or [])}


def classify(rec):
    p = prefixes(rec.get("demand_tags"))
    relevance = str(rec.get("relevance") or "").upper()
    word_count = int(rec.get("word_count") or len(str(rec.get("keyword") or "").split()))
    if rec.get("is_brand") or "brand" in p:
        return "brand_word", "品牌标记优先"
    if rec.get("is_noise") or "noise" in p or relevance == "D":
        return "wrong_category", "错类或噪声标记优先"
    if "broad" in p or (relevance in {"B", "C"} and not (p & (AUDIENCE_PREFIXES | ATTRIBUTE_PREFIXES | SCENE_PREFIXES | {"product_form"}))):
        return "broad_traffic", "泛相关且缺少明确产品属性"
    if p & AUDIENCE_PREFIXES:
        return "audience_longtail", "包含明确目标人群；人群标签优先于场景和其他属性"
    if p & SCENE_PREFIXES:
        return "scene_longtail", "包含场景或礼品语义"
    if p & ATTRIBUTE_PREFIXES:
        return "attribute_longtail", "包含材料、功能、规格、套装或配件属性"
    if "product_form" in p:
        return "precise_category", "包含明确产品形态或子品类"
    if "core_category" in p and word_count <= 2:
        return "core_head", "无属性修饰的短品类头部词"
    if relevance == "A" and word_count >= 3:
        return "high_intent_longtail", "相关度A且为多词明确购买表达"
    if relevance == "A":
        return "precise_category", "相关度A的明确品类表达"
    return "broad_traffic", "缺少可验证的精准产品语义"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    path = os.path.join(args.workdir, "cache", "keyword_labeled.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for rec in data.get("keywords") or []:
        rec["traffic_type"], rec["traffic_type_reason"] = classify(rec)
        rec["purchase_intent"] = INTENT_CN.get(str(rec.get("purchase_intent") or "").lower(), rec.get("purchase_intent"))
    data["label_schema_version"] = 3
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"OK 分类 {len(data.get('keywords') or [])} 个关键词")


if __name__ == "__main__":
    main()
