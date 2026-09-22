#!/usr/bin/env python3
"""select_trend_keywords.py — 选取核心/长尾趋势词（口径见 references/keyword-taxonomy.md §7）

输入 cache/keyword_labeled.json（须已完成 LLM 标签：relevance/demand_tags/traffic_type 等）
输出 cache/trend_keywords.json（含入选原因，供报告与趋势调用使用）

用法: python select_trend_keywords.py --workdir <任务目录> [--core 5] [--longtail 5] [--cap 20] [--min-searches 0]
"""
import argparse
import json
import math
import os
from collections import defaultdict
from statistics import median

EXCLUDE_TRAFFIC_TYPES = {"broad_traffic", "brand_word", "wrong_category"}
LONGTAIL_ATTR_TAGS = {"material", "function", "spec", "scene", "audience", "bundle", "accessory", "gift"}


def tag_prefixes(tags):
    """需求标签簇名：取标签的簇前缀（如 'material:rattan' -> 'material'）。"""
    prefixes = set()
    for t in tags or []:
        prefixes.add(str(t).split(":")[0])
    return prefixes


def eligible(rec):
    if rec.get("relevance") != "A":
        return False
    if not rec.get("traffic_type"):  # 未打流量性质标签的词不得进入候选
        return False
    if rec.get("is_brand") or rec.get("is_noise"):
        return False
    if rec.get("traffic_type") in EXCLUDE_TRAFFIC_TYPES:
        return False
    if not rec.get("searches"):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--core", type=int, default=5)
    ap.add_argument("--longtail", type=int, default=5)
    ap.add_argument("--cap", type=int, default=20)
    ap.add_argument("--min-searches", type=int, default=0)
    args = ap.parse_args()

    cache = os.path.join(args.workdir, "cache")
    labeled_path = os.path.join(cache, "keyword_labeled.json")
    if not os.path.exists(labeled_path):
        raise SystemExit("缺少 cache/keyword_labeled.json：请先由 LLM 完成翻译与多标签分类。")
    with open(labeled_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 多ASIN traffic_extend 不提供逐词来源，不能把整批ASIN当作覆盖证据。
    # 当前前100矩阵来自逐ASIN调用，可作为覆盖度与流量贡献的次级排序证据，
    # 但不会把矩阵中的新词扩入历史月份关键词池。
    matrix_coverage = defaultdict(set)
    matrix_traffic = defaultdict(float)
    matrix_path = os.path.join(cache, "asin_keyword_matrix.json")
    if os.path.exists(matrix_path):
        with open(matrix_path, "r", encoding="utf-8") as f:
            matrix = json.load(f)
        if matrix.get("snapshot_type") == "current" and matrix.get("scope") == "current_top100":
            for rec in matrix.get("records") or []:
                key = rec.get("normalized_key")
                asin = rec.get("asin")
                if not key or not asin:
                    continue
                if any(rec.get(field) is not None for field in
                       ("natural_page", "ad_page", "traffic_percentage")):
                    matrix_coverage[key].add(str(asin).upper())
                if rec.get("traffic_percentage") is not None:
                    matrix_traffic[key] += float(rec["traffic_percentage"])

    # 标签完整性闸门：>10% 关键词缺 relevance/traffic_type 视为标签步骤未完成，拒绝继续
    kws = data["keywords"]
    unlabeled = [r["keyword"] for r in kws if not r.get("relevance") or not r.get("traffic_type")]
    if kws and len(unlabeled) / len(kws) > 0.10:
        raise SystemExit(
            f"标签步骤未完成：{len(unlabeled)}/{len(kws)} 个关键词缺 relevance 或 traffic_type"
            f"（如前: {unlabeled[:5]}）。请先由 LLM 完成多标签分类再运行本脚本。")

    pool = [r for r in kws if eligible(r) and (r.get("searches") or 0) >= args.min_searches]
    if not pool:
        raise SystemExit("没有满足候选条件（相关度A/非品牌/非噪声/有搜索量）的关键词，请检查标签质量。")

    # ---------- 核心词：相关度×搜索量右上象限，词族去重 ----------
    for rec in pool:
        key = rec.get("normalized_key") or str(rec.get("keyword") or "").lower().strip()
        rec["_matrix_coverage"] = len(matrix_coverage.get(key, set()))
        rec["_matrix_traffic_sum"] = matrix_traffic.get(key, 0.0)

    core_sorted = sorted(
        pool,
        key=lambda r: ((r.get("searches") or 0), r.get("_matrix_coverage", 0),
                       r.get("_matrix_traffic_sum", 0), r.get("purchaseRate") or 0),
        reverse=True,
    )
    core, used_families = [], set()
    for rec in core_sorted:
        fam = rec.get("family") or rec.get("family_guess")
        if fam in used_families:
            continue
        used_families.add(fam)
        core.append({
            "keyword": rec["keyword"], "type": "core",
            "reason": (f"相关度A；指定月份月搜索量 {rec.get('searches')}；"
                       f"当前前100流量词覆盖 {rec.get('_matrix_coverage', 0)} 个ASIN；"
                       f"当前流量贡献合计 {round(rec.get('_matrix_traffic_sum', 0), 6)}；"
                       f"购买率 {rec.get('purchaseRate')}；词族 {fam}"),
        })
        if len(core) >= args.core:
            break

    # ---------- 长尾词：≥3词或带属性标签；购买率高于中位数；每簇≤2 ----------
    longtail_candidates = []
    for rec in pool:
        tags = tag_prefixes(rec.get("demand_tags"))
        is_long = (rec.get("word_count") or 0) >= 3 or bool(tags & LONGTAIL_ATTR_TAGS)
        if is_long:
            longtail_candidates.append((rec, tags))
    rates = [r.get("purchaseRate") for r, _ in longtail_candidates if r.get("purchaseRate") is not None]
    rate_median = median(rates) if rates else 0
    longtail_candidates = [(r, t) for r, t in longtail_candidates
                           if (r.get("purchaseRate") or 0) >= rate_median]
    longtail_candidates.sort(
        key=lambda rt: ((rt[0].get("purchaseRate") or 0) * math.log10((rt[0].get("searches") or 0) + 10),
                        rt[0].get("_matrix_coverage", 0), rt[0].get("_matrix_traffic_sum", 0)),
        reverse=True)

    longtail, cluster_count = [], {}
    core_fams = {c["keyword"] for c in core}
    used_families_lt = set()
    # core_category 是品类归属标签而非需求方向簇，不参与"每簇≤2"限制
    # （否则品类归属簇占满2个名额后，同品类所有长尾候选都会被拒，见 V1.3.2 实测）
    for rec, tags in longtail_candidates:
        fam = rec.get("family") or rec.get("family_guess")
        # 长尾可以属于已入选核心词的同一基础品类；只禁止与核心词完全相同，
        # 并在长尾内部按词族去重。否则 audience/material/function 等修饰词
        # 会因为共享基础品类词族而被全部排除。
        if rec["keyword"] in core_fams or fam in used_families_lt:
            continue
        clusters = (tags or {"untagged"}) - {"core_category"}
        if not clusters:
            clusters = {"untagged"}
        if any(cluster_count.get(c, 0) >= 2 for c in clusters):
            continue
        for c in clusters:
            cluster_count[c] = cluster_count.get(c, 0) + 1
        used_families_lt.add(fam)
        longtail.append({
            "keyword": rec["keyword"], "type": "longtail",
            "reason": (f"相关度A长尾；购买率 {rec.get('purchaseRate')}（≥中位数 {round(rate_median, 4)}）；"
                       f"月搜索量 {rec.get('searches')}；标签 {rec.get('demand_tags')}；词数 {rec.get('word_count')}"),
        })
        if len(longtail) >= args.longtail:
            break

    selected = core + longtail
    if len(selected) > args.cap:
        selected = selected[:args.cap]

    out = {
        "core_count": len(core), "longtail_count": len(longtail),
        "purchase_rate_median_longtail_pool": rate_median,
        "keywords": selected,
        "note": "每词每站点仅调用一次 aba_research_trend；原始返回存 raw/trend_<slug>.json",
    }
    out_path = os.path.join(cache, "trend_keywords.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"OK 核心词 {len(core)} 个，长尾词 {len(longtail)} 个 -> {out_path}")
    for k in selected:
        print(f"  [{k['type']}] {k['keyword']}")


if __name__ == "__main__":
    main()
