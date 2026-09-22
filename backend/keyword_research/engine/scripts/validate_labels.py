#!/usr/bin/env python3
"""校验关键词标签契约，阻止属性词被归为核心大词。"""
import argparse
import json
import os
import sys

from classify_keywords import AUDIENCE_PREFIXES, ATTRIBUTE_PREFIXES, SCENE_PREFIXES, classify, prefixes

TRAFFIC = {"core_head", "precise_category", "high_intent_longtail", "audience_longtail",
           "attribute_longtail", "scene_longtail", "broad_traffic", "brand_word", "wrong_category"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    path = os.path.join(args.workdir, "cache", "keyword_labeled.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    errors = []
    for rec in data.get("keywords") or []:
        kw = rec.get("keyword") or "<空词>"
        p = prefixes(rec.get("demand_tags"))
        required = (rec.get("relevance"), rec.get("relevance_reason"), rec.get("purchase_intent"), rec.get("traffic_type_reason"))
        if any(v in (None, "") for v in required):
            errors.append(f"{kw}: 标签/理由不完整")
        if rec.get("traffic_type") not in TRAFFIC:
            errors.append(f"{kw}: 非法流量性质 {rec.get('traffic_type')}")
        expected, _ = classify(rec)
        if rec.get("traffic_type") != expected:
            errors.append(f"{kw}: 流量性质应为 {expected}，实际 {rec.get('traffic_type')}")
        if rec.get("traffic_type") == "core_head" and p & (AUDIENCE_PREFIXES | ATTRIBUTE_PREFIXES | SCENE_PREFIXES):
            errors.append(f"{kw}: 核心大词与具体属性/场景标签冲突")
        if "audience" in p and not (rec.get("is_brand") or rec.get("is_noise") or "brand" in p
                                     or "noise" in p or "broad" in p
                                     or str(rec.get("relevance") or "").upper() == "D"):
            if rec.get("traffic_type") != "audience_longtail":
                errors.append(f"{kw}: 含人群标签时必须归为 audience_longtail")
        if str(rec.get("relevance") or "").upper() == "A" and not p:
            errors.append(f"{kw}: 相关度A但无需求标签")
        if rec.get("purchase_intent") not in {"高", "中", "低"}:
            errors.append(f"{kw}: 购买意图必须为高/中/低")
    if errors:
        print("\n".join(f"[FAIL] {e}" for e in errors[:50]))
        raise SystemExit(f"标签验收失败：{len(errors)} 项")
    print(f"OK 标签验收：{len(data.get('keywords') or [])} 个关键词")


if __name__ == "__main__":
    main()
