#!/usr/bin/env python3
"""normalize_keywords.py — 关键词清洗与标准化（就地更新 keyword_master.json）

完成：Unicode NFKC、匹配键、词数、单复数/连写变体的词族猜测（family_guess）、
品牌词候选标记（与 asin_pool.json 中品牌比对）、展示原词保留。
同义词族的最终确认与语义标签由 LLM 在 keyword_labeled.json 阶段完成。

用法: python normalize_keywords.py --workdir <任务目录>
"""
import argparse
import json
import os
import re
import unicodedata

STOP_INFLECT = {"s", "es"}


def normalize_key(text):
    text = unicodedata.normalize("NFKC", str(text)).lower().strip()
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def stem_token(tok):
    """极简英文词干：仅处理规则复数，供词族猜测用（德语等交由 LLM 确认）。"""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("es") and not tok.endswith("ses"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def family_guess(normalized):
    tokens = [stem_token(t) for t in normalized.replace("-", " ").split()]
    return " ".join(sorted(tokens))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    cache = os.path.join(args.workdir, "cache")
    master_path = os.path.join(cache, "keyword_master.json")
    with open(master_path, "r", encoding="utf-8") as f:
        master = json.load(f)

    # 品牌词候选：来自 asin_pool 的品牌集合
    brands = set()
    pool_path = os.path.join(cache, "asin_pool.json")
    if os.path.exists(pool_path):
        with open(pool_path, "r", encoding="utf-8") as f:
            pool = json.load(f)
        for a in pool.get("asins", []):
            b = (a.get("brand") or "").strip().lower()
            if b:
                brands.add(b)

    n_brand = 0
    for rec in master["keywords"]:
        key = rec.get("normalized_key") or normalize_key(rec["keyword"])
        rec["normalized_key"] = key
        rec["word_count"] = len(key.split())
        rec["family_guess"] = family_guess(key)
        toks = set(key.split())
        rec["is_brand_candidate"] = any(b in key or b in toks for b in brands) if brands else False
        if rec["is_brand_candidate"]:
            n_brand += 1
        # 预留 LLM 字段位（不覆盖已有人工/LLM 填写）
        rec.setdefault("family", rec["family_guess"])
        rec.setdefault("demand_tags", [])
        rec.setdefault("traffic_type", None)
        rec.setdefault("relevance", None)
        rec.setdefault("relevance_reason", None)
        rec.setdefault("purchase_intent", None)
        rec.setdefault("translation_fixed", None)
        rec.setdefault("translation_reason", None)
        rec.setdefault("translation_confidence", None)
        rec.setdefault("is_brand", rec["is_brand_candidate"])
        rec.setdefault("is_noise", False)

    master["normalized"] = True
    with open(master_path, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)

    # 同时导出一份待标注模板，便于 LLM 分批填写后另存为 keyword_labeled.json
    labeled_path = os.path.join(cache, "keyword_labeled.json")
    if not os.path.exists(labeled_path):
        with open(labeled_path, "w", encoding="utf-8") as f:
            json.dump(master, f, ensure_ascii=False, indent=2)
        print(f"已生成待标注文件: {labeled_path}（LLM 完成翻译与标签后保存）")
    print(f"OK 标准化 {master['keyword_count']} 词；品牌词候选 {n_brand} 个")


if __name__ == "__main__":
    main()
