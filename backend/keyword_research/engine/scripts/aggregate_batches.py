#!/usr/bin/env python3
"""aggregate_batches.py — 合并 traffic_extend 固定页数结果为关键词主表。

- 容错解析 MCP 返回（list 或嵌套 dict 中取关键词列表）
- 按 normalized 原词去重合并，保留来源 ASIN 追踪
- 数值字段取最新批次快照；批次间冲突记入 quality_notes，不静默覆盖
- 缺失字段保持 null，绝不填 0

用法: python aggregate_batches.py --workdir <任务目录>
"""
import argparse
import glob
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

# 关键词主表保留的数值/文本字段（缺失 -> None，不填 0）
VALUE_FIELDS = [
    "keywordCn", "searches", "purchases", "purchaseRate", "bid", "bidMin", "bidMax",
    "products", "latest1daysAds", "latest30daysAds", "supplyDemandRatio",
    "titleDensity", "avgPrice", "trafficPercentage", "monopolyClickRate",
    "top3ClickingRate", "top3ConversionRate", "relationVariationsItems",
]

KEYWORD_KEYS = ["keyword", "keywords", "word", "searchTerm", "search_term"]
LIST_HINT_KEYS = ["keyword", "searchTerm", "word"]


def normalize_key(text):
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.lower().strip()
    text = re.sub(r"[\u2018\u2019']", "'", text)
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def unwrap_tool_result(obj):
    """兼容直接 API JSON 与 MCP content[0].text 包装。

    MCP servers 通常把真正的返回 JSON 包在 result.content[0].text <str> 中；
    若落盘的原始返回保留了该包装，需要先解包再解析，否则会丢失全部关键词。
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


def find_keyword_list(obj):
    """在任意嵌套结构里找到第一个『元素为 dict 且含关键词字段』的 list。"""
    obj = unwrap_tool_result(obj)
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            keys = set().union(*(x.keys() for x in obj[:5]))
            if any(k in keys for k in LIST_HINT_KEYS):
                return obj
        for item in obj:
            found = find_keyword_list(item)
            if found:
                return found
    elif isinstance(obj, dict):
        for value in obj.values():
            found = find_keyword_list(value)
            if found:
                return found
    return None


def get_kw_text(item):
    for key in KEYWORD_KEYS:
        if isinstance(item.get(key), str) and item[key].strip():
            return item[key].strip()
    return None


def batch_page_key(path):
    name = os.path.basename(path)
    m = re.search(r"traffic_batch_(\d+)(?:_p(\d+))?\.json$", name, re.I)
    return (int(m.group(1)), int(m.group(2) or 1)) if m else (0, 1)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def conflict_value_summary(field, value):
    """避免把关联商品大对象整段塞进质量备注和Excel单元格。"""
    if field == "relationVariationsItems" and isinstance(value, list):
        return f"关联商品{len(value)}条"
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    raw_dir = os.path.join(args.workdir, "raw")
    cache_dir = os.path.join(args.workdir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    input_path = os.path.join(args.workdir, "input.json")
    input_cfg = load_json(input_path) if os.path.exists(input_path) else {}
    data_month = str((input_cfg.get("options") or {}).get("data_month") or datetime.now().strftime("%Y%m"))
    if not re.fullmatch(r"\d{6}", data_month):
        raise SystemExit("options.data_month 必须为 yyyyMM，例如 202607。")

    options = input_cfg.get("options") or {}
    explicit_pages = options.get("keyword_pool_pages")
    legacy_size = options.get("keyword_pool_size")
    if explicit_pages is not None:
        keyword_pool_pages = int(explicit_pages)
        if keyword_pool_pages != 3:
            raise SystemExit("options.keyword_pool_pages 当前标准只允许 3。")
        pool_config_source = "keyword_pool_pages"
    else:
        legacy_size = int(legacy_size or 100)
        if legacy_size not in (50, 100):
            raise SystemExit("旧配置 options.keyword_pool_size 只允许 50 或 100。")
        keyword_pool_pages = 1 if legacy_size == 50 else 3
        pool_config_source = "legacy_keyword_pool_size"

    all_batch_files = sorted(glob.glob(os.path.join(raw_dir, "traffic_batch_*.json")), key=batch_page_key)
    batch_files = [p for p in all_batch_files if batch_page_key(p)[1] <= keyword_pool_pages]
    if not batch_files:
        raise SystemExit(f"未找到 {raw_dir}/traffic_batch_*.json，请先执行 traffic_extend 并落盘原始返回。")

    master = {}  # normalized_key -> record
    conflicts = 0
    raw_rows = 0
    duplicate_rows = 0
    cross_page_duplicates = 0
    first_source = {}
    page_stats = []
    for path in batch_files:
        batch_no, source_page = batch_page_key(path)
        data = unwrap_tool_result(load_json(path))
        items = find_keyword_list(data)
        returned_count = len(items or [])
        page_stats.append({"batch": batch_no, "page": source_page,
                           "file": os.path.basename(path), "returned": returned_count,
                           "parsed": bool(items)})
        if not items:
            print(f"[warn] {os.path.basename(path)} 未解析到关键词列表，已跳过")
            continue
        # 尝试从文件级元数据取 ASIN 列表（由调用方落盘时附带 _meta 更佳）
        meta_asins = data.get("_meta", {}).get("asins", []) if isinstance(data, dict) else []
        for item in items:
            kw = get_kw_text(item)
            if not kw:
                continue
            key = normalize_key(kw)
            if not key:
                continue
            raw_rows += 1
            source_id = (batch_no, source_page)
            if key in master:
                duplicate_rows += 1
                if first_source.get(key) != source_id:
                    cross_page_duplicates += 1
            else:
                first_source[key] = source_id
            if key not in master:
                master[key] = {"keyword": kw, "normalized_key": key, "sources": [], "source_asins": [],
                               "snapshot_count": 0, "quality_notes": []}
                for f in VALUE_FIELDS:
                    master[key][f] = None
            rec = master[key]
            rec["snapshot_count"] += 1
            # 来源追踪：traffic_extend 的多ASIN批次不提供逐词来源ASIN。
            # 只有单条记录显式带 asin，或调用批次本身仅含1个ASIN时，才能写 source_asins；
            # 多ASIN批次只保存批次范围，禁止把整批ASIN伪装成每个词的实际覆盖。
            item_asin = item.get("asin") or item.get("ASIN")
            src = {"batch": batch_no, "page": source_page, "batch_asins": list(meta_asins)}
            if item_asin:
                src["asin"] = item_asin
                if item_asin not in rec["source_asins"]:
                    rec["source_asins"].append(item_asin)
            elif len(meta_asins) == 1:
                src["asin"] = meta_asins[0]
                if meta_asins[0] not in rec["source_asins"]:
                    rec["source_asins"].append(meta_asins[0])
            if item.get("trafficPercentage") is not None:
                src["trafficPercentage"] = item.get("trafficPercentage")
            rec["sources"].append(src)
            # 数值字段：最新批次优先；冲突记录不覆盖
            for f in VALUE_FIELDS:
                v = item.get(f)
                if v is None:
                    continue
                if rec[f] is not None and rec[f] != v:
                    conflicts += 1
                    rec["quality_notes"].append(
                        f"字段 {f} 批次冲突: 旧={conflict_value_summary(f, rec[f])} "
                        f"新={conflict_value_summary(f, v)}（batch {batch_no}，已采用新值）")
                rec[f] = v

    keywords = sorted(master.values(), key=lambda r: (r["searches"] or 0), reverse=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_month": data_month,
        "keyword_count": len(keywords),
        "keyword_pool_pages": keyword_pool_pages,
        "keyword_pool_page_size": 50,
        "keyword_pool_config_source": pool_config_source,
        "keyword_pool_actual": len(keywords),
        "keyword_pool_raw_rows": raw_rows,
        "keyword_pool_duplicate_rows": duplicate_rows,
        "keyword_pool_cross_page_duplicates": cross_page_duplicates,
        "keyword_pool_page_stats": page_stats,
        "keyword_pool_missing_pages": [
            {"batch": batch, "page": page}
            for batch in sorted({batch_page_key(p)[0] for p in batch_files})
            for page in range(1, keyword_pool_pages + 1)
            if not any(batch_page_key(p) == (batch, page) for p in batch_files)
        ],
        "batch_files": [os.path.basename(p) for p in batch_files],
        "conflict_count": conflicts,
        "keywords": keywords,
    }
    out_path = os.path.join(cache_dir, "keyword_master.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"OK 合并 {len(batch_files)} 个批次页、原始 {raw_rows} 行 -> {len(keywords)} 个去重关键词；"
          f"跨页/批次重复 {cross_page_duplicates} 行；字段冲突 {conflicts} 处")
    print(f"输出: {out_path}")


if __name__ == "__main__":
    main()
