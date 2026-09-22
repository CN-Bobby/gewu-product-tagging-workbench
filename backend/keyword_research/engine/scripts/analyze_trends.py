#!/usr/bin/env python3
"""analyze_trends.py — 24 个月趋势计算（口径见 references/metric-definitions.md §3/§4）

读取 cache/trend_keywords.json + raw/trend_*.json（aba_research_trend 原始返回），
以所有入选词共同的最新完整月为终点截取最近 24 个月，计算全部趋势指标。
缺月保留 null，不补 0、不插值。

用法: python analyze_trends.py --workdir <任务目录> [--months 24]
"""
import argparse
import glob
import json
import os
import re
from statistics import mean, median, pstdev

# 实测：SellerSprite aba_research_trend 月度点的月份字段为 label（如 "2024-08"）
MONTH_KEYS = ["month", "label", "date", "yearMonth", "monthDate", "statMonth", "year_month"]
SEARCH_KEYS = ["searches", "searchVolume", "search_volume", "sv", "searchs"]
RANK_KEYS = ["rank", "abaRank", "aba_rank", "searchRank"]


def slugify(kw):
    return re.sub(r"[^a-z0-9]+", "_", kw.lower()).strip("_")


def normalize_key(text):
    """与 normalize_keywords.py 保持一致的匹配键：NFKC + 小写 + 标点转空格。"""
    import unicodedata
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


def normalize_hypothesis_keywords(workdir):
    """幂等规整层：把 product_hypotheses.source_keywords 的变体映射回词池精确词。

    背景：LLM 生成 product_hypotheses 时可能改写引用词（如单复数
    'girls swimsuit size 10-12' vs 词池 'girls swimsuits size 10-12'），
    validate_analysis.py 按关键词总表精确匹配，变体会导致误杀。
    此处用 family_guess 词族做唯一命中映射；无法唯一命中的保留原样，交给验收报错。
    """
    cache_dir = os.path.join(workdir, "cache")
    analysis_path = os.path.join(cache_dir, "analysis.json")
    labeled_path = os.path.join(cache_dir, "keyword_labeled.json")
    if not os.path.exists(analysis_path) or not os.path.exists(labeled_path):
        return

    with open(labeled_path, "r", encoding="utf-8") as f:
        labeled = json.load(f)
    with open(analysis_path, "r", encoding="utf-8") as f:
        analysis = json.load(f)

    records = labeled.get("keywords") or []
    known = {r.get("keyword") for r in records if r.get("keyword")}
    by_family = {}
    for kw in known:
        by_family.setdefault(family_guess(normalize_key(kw)), []).append(kw)

    replaced = unresolved = 0
    for row in analysis.get("product_hypotheses") or []:
        sources = row.get("source_keywords") or []
        fixed = []
        for kw in sources:
            if kw in known:
                fixed.append(kw)
                continue
            candidates = by_family.get(family_guess(normalize_key(kw))) or []
            best = best_family_match(normalize_key(kw), candidates)
            if best:
                fixed.append(best)
                replaced += 1
            else:
                fixed.append(kw)
                unresolved += 1
        row["source_keywords"] = fixed

    if replaced or unresolved:
        with open(analysis_path, "w", encoding="utf-8") as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
    print(f"[normalize_analysis] source_keywords 归一词池: 替换 {replaced} 个，未解决 {unresolved} 个 -> {analysis_path}")


def unwrap_tool_result(obj):
    """兼容直接 API JSON 与 MCP content[0].text 包装。

    MCP servers 通常把真正的返回 JSON 包在 result.content[0].text <str> 中；
    若落盘的原始返回保留了该包装，需要先解包再解析，否则会找不到月度序列。
    """
    if not isinstance(obj, dict):
        return obj
    content = obj.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, (dict, list)):
                    if "_meta" in obj and isinstance(parsed, dict) and "_meta" not in parsed:
                        parsed["_meta"] = obj["_meta"]
                    return parsed
    return obj


def find_series(obj):
    """容错查找月度序列：元素为 dict 且含月份字段的 list。"""
    obj = unwrap_tool_result(obj)
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            keys = set().union(*(x.keys() for x in obj[:5]))
            if any(k in keys for k in MONTH_KEYS):
                return obj
        for item in obj:
            found = find_series(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for value in obj.values():
            found = find_series(value)
            if found:
                return found
    return None


def pick(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def norm_month(raw):
    """把 2025-01 / 2025/01 / 202501 / 2025-01-01 统一为 YYYY-MM。"""
    s = str(raw).strip()
    m = re.search(r"(\d{4})\D?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def month_sequence(end_month, count):
    """生成以 end_month 结束的连续自然月序列，不因数据缺月而压缩横轴。"""
    year, month = map(int, end_month.split("-"))
    out = []
    for offset in range(count - 1, -1, -1):
        absolute = year * 12 + (month - 1) - offset
        y, m0 = divmod(absolute, 12)
        out.append(f"{y:04d}-{m0 + 1:02d}")
    return out


def load_series(workdir, keyword):
    raw_dir = os.path.join(workdir, "raw")
    candidates = [os.path.join(raw_dir, f"trend_{slugify(keyword)}.json")]
    candidates += [p for p in glob.glob(os.path.join(raw_dir, "trend_*.json")) if p not in candidates]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        data = unwrap_tool_result(data)
        # 文件可能含 keyword 字段用于归属校验
        if isinstance(data, dict):
            file_kw = data.get("keyword") or (data.get("_meta") or {}).get("keyword")
            if file_kw and file_kw.strip().lower() != keyword.strip().lower():
                continue
        series = find_series(data)
        if series:
            return series, os.path.basename(path)
    return None, None


def extract_points(series):
    """-> {month: {'searches': x, 'rank': y}}"""
    points = {}
    for item in series:
        m = norm_month(pick(item, MONTH_KEYS))
        if not m:
            continue
        sv = pick(item, SEARCH_KEYS)
        rk = pick(item, RANK_KEYS)
        try:
            sv = float(sv) if sv is not None else None
        except (TypeError, ValueError):
            sv = None
        points[m] = {"searches": sv, "rank": rk}
    return points


def safe_div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def compute_stats(months, values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {}
    latest = next((values[i] for i in range(len(values) - 1, -1, -1) if values[i] is not None), None)
    latest_idx = max(i for i, v in enumerate(values) if v is not None)
    prev = next((values[i] for i in range(latest_idx - 1, -1, -1) if values[i] is not None), None)
    yoy_base = values[latest_idx - 12] if latest_idx >= 12 else None
    avg = mean(vals)
    peak_i = max(range(len(values)), key=lambda i: (values[i] is not None, values[i] or -1))
    trough_vals = [(v, i) for i, v in enumerate(values) if v is not None]
    trough_v, trough_i = min(trough_vals)

    def growth(n):
        if latest_idx - n + 1 < 0:
            return None
        base = next((values[i] for i in range(latest_idx - n + 1, -1, -1) if values[i] is not None), None)
        g = safe_div(latest, base)
        return round(g - 1, 4) if g is not None else None

    last12 = [v for v in values[-12:] if v is not None]
    prev12 = [v for v in values[-24:-12] if v is not None]
    cumulative = None
    if last12 and prev12 and sum(prev12) != 0:
        cumulative = round(sum(last12) / sum(prev12) - 1, 4)

    consec = 0
    for i in range(latest_idx, 0, -1):
        if values[i] is not None and values[i - 1] is not None and values[i] > values[i - 1]:
            consec += 1
        else:
            break

    inflection = None
    for i in range(3, len(values) - 1):
        window = values[i - 3:i + 1]
        if any(v is None for v in window):
            continue
        pre = values[i - 3:i]
        if values[i] > values[i - 1] and values[i + 1] is not None and values[i + 1] > values[i]:
            if max(pre) <= values[i]:
                inflection = months[i]
                break

    seasonality = [round(v / avg, 4) if v is not None and avg else None for v in values]
    # 两年重复性：同月季节指数符号一致率
    repeat = None
    if len(values) >= 24:
        agree = total = 0
        for i in range(12):
            a, b = seasonality[i], seasonality[i + 12]
            if a is None or b is None:
                continue
            total += 1
            if (a >= 1) == (b >= 1):
                agree += 1
        repeat = round(agree / total, 4) if total else None

    sd = pstdev(vals) if len(vals) > 1 else 0
    return {
        "latest_month": months[latest_idx], "latest_searches": latest,
        "avg_24m": round(avg, 2), "median_24m": round(median(vals), 2),
        "mom": round(safe_div(latest, prev) - 1, 4) if safe_div(latest, prev) is not None else None,
        "yoy": round(safe_div(latest, yoy_base) - 1, 4) if safe_div(latest, yoy_base) is not None else None,
        "growth_3m": growth(3), "growth_6m": growth(6),
        "yoy_cumulative_12m": cumulative,
        "std": round(sd, 2), "cv": round(sd / avg, 4) if avg else None,
        "peak_month": months[peak_i], "peak_value": values[peak_i],
        "trough_month": months[trough_i], "trough_value": trough_v,
        "peak_trough_ratio": round(safe_div(values[peak_i], trough_v), 4) if safe_div(values[peak_i], trough_v) else None,
        "consecutive_growth_months": consec,
        "first_growth_inflection": inflection,
        "two_year_repeat_score": repeat,
        "missing_months": [months[i] for i, v in enumerate(values) if v is None],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--months", type=int, default=24)
    args = ap.parse_args()

    cache = os.path.join(args.workdir, "cache")
    with open(os.path.join(cache, "trend_keywords.json"), "r", encoding="utf-8") as f:
        trend_kw = json.load(f)

    per_kw, latest_months = {}, []
    missing_files = []
    for entry in trend_kw["keywords"]:
        kw = entry["keyword"]
        series, fname = load_series(args.workdir, kw)
        if not series:
            missing_files.append(kw)
            continue
        points = extract_points(series)
        per_kw[kw] = {"points": points, "file": fname, "type": entry["type"], "reason": entry.get("reason")}
        if points:
            latest_months.append(max(points.keys()))

    if missing_files:
        print(f"[warn] 以下词未找到趋势文件 raw/trend_<slug>.json: {missing_files}")
    if not per_kw:
        raise SystemExit("没有任何可用的趋势数据，请先调用 aba_research_trend 并落盘。")

    # 共同最新完整月：取各成功关键词最新月中的最早者；窗口保持连续自然月。
    # 内部缺月只在对应词上记 null，不能从所有词横轴删除，否则 MoM/YoY 月距会错位。
    end_month = min(latest_months)
    months = month_sequence(end_month, args.months)

    results = {}
    for kw, rec in per_kw.items():
        values = [rec["points"].get(m, {}).get("searches") for m in months]
        ranks = [rec["points"].get(m, {}).get("rank") for m in months]
        stats = compute_stats(months, values)
        avg = stats.get("avg_24m")
        series_out = []
        for i, m in enumerate(months):
            v, prev_v = values[i], values[i - 1] if i > 0 else None
            yoy_v = values[i - 12] if i >= 12 else None
            series_out.append({
                "month": m, "searches": v, "aba_rank": ranks[i],
                "mom": round(v / prev_v - 1, 4) if v is not None and prev_v not in (None, 0) else None,
                "yoy": round(v / yoy_v - 1, 4) if v is not None and yoy_v not in (None, 0) else None,
                "seasonality_index": round(v / avg, 4) if v is not None and avg else None,
            })
        results[kw] = {"type": rec["type"], "reason": rec["reason"], "source_file": rec["file"],
                       "stats": stats, "series": series_out}

    out = {"months": months, "end_month": end_month, "window": args.months, "keywords": results}
    out_path = os.path.join(cache, "trend_analysis.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"OK {len(results)} 个词，窗口 {months[0]} ~ {months[-1]} -> {out_path}")

    # 分析结果规整：LLM 引用词变体映射回词池精确词，保证后续 validate_analysis 严格验收通过。
    normalize_hypothesis_keywords(args.workdir)


if __name__ == "__main__":
    main()
