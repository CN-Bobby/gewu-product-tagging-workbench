#!/usr/bin/env python3
"""校验产品方向证据链。无假设可以通过；有假设必须可回溯。"""
import argparse
import json
import os
import re
import unicodedata


def normalize_key(text):
    """与 normalize_keywords.py 保持一致的匹配键：NFKC + 小写 + 标点转空格。"""
    text = unicodedata.normalize("NFKC", str(text)).lower().strip()
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def stem_token(tok):
    """极简英文词干（规则复数），与 normalize_keywords.py 一致，供词族匹配。"""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("es") and not tok.endswith("ses"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def family_guess(normalized):
    """与 normalize_keywords.py 一致：分词去复数后排序拼接。"""
    tokens = [stem_token(t) for t in normalized.replace("-", " ").split()]
    return " ".join(sorted(tokens))


def token_match_score(query_nk, cand_nk):
    """token 级相似度：(逐位相同 token 数, 首个不同位置靠后) 越大越优。"""
    tq = query_nk.split()
    tc = cand_nk.split()
    same = sum(1 for a, b in zip(tq, tc) if a == b)
    first_diff = next((i for i, (a, b) in enumerate(zip(tq, tc)) if a != b), len(tq))
    return same, first_diff


def best_family_match(query_nk, candidates):
    """候选唯一最优时返回它，否则返回 None（避免歧义误映射）。"""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    scored = [token_match_score(query_nk, normalize_key(c)) for c in candidates]
    best = max(scored)
    if scored.count(best) == 1:
        return candidates[scored.index(best)]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    cache = os.path.join(args.workdir, "cache")
    with open(os.path.join(cache, "keyword_labeled.json"), "r", encoding="utf-8") as f:
        labeled = json.load(f)
    with open(os.path.join(cache, "analysis.json"), "r", encoding="utf-8") as f:
        analysis = json.load(f)
    known = {r.get("keyword") for r in labeled.get("keywords") or [] if r.get("keyword")}
    by_family = {}
    for kw in known:
        by_family.setdefault(family_guess(normalize_key(kw)), set()).add(kw)
    errors = []
    warnings = []
    for i, row in enumerate(analysis.get("product_hypotheses") or [], 1):
        sources = row.get("source_keywords") or []
        if not sources:
            errors.append(f"第{i}行证据关键词缺失")
        else:
            for k in sources:
                if k in known:
                    continue
                candidates = list(by_family.get(family_guess(normalize_key(k))) or set())
                best = best_family_match(normalize_key(k), candidates)
                if best:
                    warnings.append(f"第{i}行关键词按词族匹配: {k} -> {best}")
                else:
                    errors.append(f"第{i}行证据关键词不在关键词池: {k}")
        if not isinstance(row.get("evidence"), dict) or not row.get("evidence"):
            errors.append(f"第{i}行缺数据证据")
        if row.get("confidence") not in {"high", "mid", "low"}:
            errors.append(f"第{i}行置信度非法")
        if row.get("inference_level") != "hypothesis":
            errors.append(f"第{i}行必须标记 hypothesis")
        if "假设" not in str(row.get("product_response") or ""):
            errors.append(f"第{i}行产品响应未标记假设")
        if not row.get("validation_needed"):
            errors.append(f"第{i}行缺待验证数据")
    for w in warnings:
        print(f"[WARN] {w}")
    if errors:
        print("\n".join(f"[FAIL] {e}" for e in errors))
        raise SystemExit(f"分析验收失败：{len(errors)} 项（首条: {errors[0]}）")
    print(f"OK 产品方向证据验收：{len(analysis.get('product_hypotheses') or [])} 行")


if __name__ == "__main__":
    main()
