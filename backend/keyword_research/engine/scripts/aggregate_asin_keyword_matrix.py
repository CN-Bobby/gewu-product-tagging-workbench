#!/usr/bin/env python3
"""聚合当前 traffic_keyword 第1页为共享的关键词×ASIN矩阵缓存。

输入：raw/traffic_keyword_current_<ASIN>_p1.json
输出：cache/asin_keyword_matrix.json

每个直接输入 ASIN 只读取当前前100个流量词。自然位、广告位与流量占比
来自同一次响应；历史月份或旧版多页缓存不会被静默当作当前矩阵使用。
"""
import argparse
import glob
import json
import os
import re
import unicodedata
from datetime import datetime, timezone


PAGE_SIZE = 100
CURRENT_FILE_RE = re.compile(r"traffic_keyword_current_([A-Za-z0-9]+)_p1\.json$", re.I)
LEGACY_FILE_RE = re.compile(r"traffic_keyword_([A-Za-z0-9]+)_p\d+\.json$", re.I)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_key(text):
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).lower().strip()
    text = re.sub(r"[\u2018\u2019']", "'", text)
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def unwrap_tool_result(obj):
    """兼容直接 API JSON 与 MCP content[0].text 包装。"""
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


def find_items(obj):
    if isinstance(obj, dict):
        items = obj.get("items")
        if isinstance(items, list) and (not items or all(isinstance(x, dict) for x in items)):
            return items
        for value in obj.values():
            found = find_items(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_items(value)
            if found is not None:
                return found
    return None


def int_or_none(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rank_fields(value):
    if not isinstance(value, dict):
        return {"page": None, "index": None, "position": int_or_none(value)}
    return {
        "page": int_or_none(value.get("page")),
        "index": int_or_none(value.get("index")),
        "position": int_or_none(value.get("position")),
    }


def updated_time(item):
    candidates = [item.get("updatedTime")]
    for key in ("rankPosition", "adPosition"):
        if isinstance(item.get(key), dict):
            candidates.append(item[key].get("updatedTime"))
    numeric = []
    for value in candidates:
        try:
            numeric.append(int(value))
        except (TypeError, ValueError):
            pass
    return max(numeric) if numeric else None


def unique_order(values):
    out = []
    seen = set()
    for value in values:
        value = str(value).strip().upper()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def error_label(code):
    if str(code).upper() == "ERROR_UNAUTHORIZED":
        return "授权或额度拒绝（不能仅凭该错误判定密钥无权限）"
    return str(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    input_cfg = load_json(os.path.join(args.workdir, "input.json"))
    groups = input_cfg.get("groups") or []
    direct_asins = unique_order(a for g in groups for a in (g.get("asins") or []))
    marketplaces = unique_order(g.get("marketplace") for g in groups if g.get("marketplace"))
    marketplace = marketplaces[0] if marketplaces else ""

    raw_dir = os.path.join(args.workdir, "raw")
    cache_dir = os.path.join(args.workdir, "cache")
    os.makedirs(cache_dir, exist_ok=True)

    current_files = {}
    for path in glob.glob(os.path.join(raw_dir, "traffic_keyword_current_*_p1.json")):
        match = CURRENT_FILE_RE.search(os.path.basename(path))
        if match and match.group(1).upper() in direct_asins:
            current_files[match.group(1).upper()] = path

    legacy_asins = set()
    for path in glob.glob(os.path.join(raw_dir, "traffic_keyword_*_p*.json")):
        match = LEGACY_FILE_RE.search(os.path.basename(path))
        if match:
            legacy_asins.add(match.group(1).upper())

    records = []
    coverage = {}
    observed_times = []
    months = set()
    for asin in direct_asins:
        path = current_files.get(asin)
        notes = []
        failed = False
        raw_items = 0
        duplicate_items = 0
        by_keyword = {}
        observed_at = None
        month = None
        page_size = PAGE_SIZE

        if path:
            raw = load_json(path)
            raw_meta = raw.get("_meta", {}) if isinstance(raw, dict) else {}
            payload = unwrap_tool_result(raw)
            payload_meta = payload.get("_meta", {}) if isinstance(payload, dict) else {}
            meta = {**payload_meta, **raw_meta}
            month = meta.get("month")
            if month:
                months.add(str(month))
            observed_at = meta.get("observed_at") or meta.get("fetched_at")
            if observed_at:
                observed_times.append(str(observed_at))
            page = int_or_none(meta.get("page")) or 1
            page_size = int_or_none(meta.get("page_size") or meta.get("size")) or PAGE_SIZE
            if page != 1:
                failed = True
                notes.append(f"当前矩阵只接受第1页，实际页码={page}")
            elif isinstance(payload, dict) and payload.get("code") not in (None, "OK"):
                failed = True
                notes.append(f"接口状态={error_label(payload.get('code'))}")
            else:
                items = find_items(payload)
                if items is None:
                    failed = True
                    data_part = payload.get("data") if isinstance(payload, dict) else None
                    total = data_part.get("total") if isinstance(data_part, dict) else None
                    if total == 0:
                        notes.append(f"该ASIN该月无流量词（total=0），属服务端数据缺失")
                    else:
                        notes.append("未解析到 items")
                else:
                    raw_items = len(items)
                    for item in items:
                        keyword = item.get("keyword") or item.get("keywords") or item.get("searchTerm")
                        key = normalize_key(keyword)
                        if not key:
                            continue
                        natural = rank_fields(item.get("rankPosition"))
                        ad = rank_fields(item.get("adPosition"))
                        rec = {
                            "asin": asin,
                            "keyword": str(keyword).strip(),
                            "normalized_key": key,
                            "natural_page": natural["page"],
                            "natural_index": natural["index"],
                            "natural_rank": natural["position"],
                            "ad_page": ad["page"],
                            "ad_index": ad["index"],
                            "ad_rank": ad["position"],
                            "traffic_percentage": number(item.get("trafficPercentage")),
                            "updated_time": updated_time(item),
                            "source_page": 1,
                            "quality_notes": [],
                        }
                        previous = by_keyword.get(key)
                        if previous:
                            duplicate_items += 1
                            prev_ts = previous.get("updated_time")
                            new_ts = rec.get("updated_time")
                            use_new = new_ts is not None and (prev_ts is None or new_ts >= prev_ts)
                            chosen = rec if use_new else previous
                            chosen["quality_notes"] = list(chosen.get("quality_notes") or []) + [
                                f"同页重复，采用{'较新' if use_new else '原有'}快照"
                            ]
                            by_keyword[key] = chosen
                        else:
                            by_keyword[key] = rec
        else:
            failed = True
            notes.append("未找到当前 traffic_keyword 第1页原始文件")
            if asin in legacy_asins:
                notes.append("发现旧版历史/多页缓存，已忽略，未冒充当前矩阵")

        coverage[asin] = {
            "pages": [1] if path else [],
            "page_size": page_size,
            "page_limit": 1,
            "scope": "current_top100",
            "month": month if path else None,
            "raw_items": raw_items,
            "unique_keywords": len(by_keyword),
            "duplicate_items": duplicate_items,
            "request_success": bool(path) and not failed,
            "complete": bool(path) and not failed,
            "universe_complete": False,
            "observed_at": observed_at,
            "notes": notes,
        }
        records.extend(sorted(by_keyword.values(), key=lambda r: r["normalized_key"]))

    generated_at = datetime.now(timezone.utc).isoformat()
    matrix_month = months.pop() if len(months) == 1 else (sorted(months)[0] if months else None)
    out = {
        "schema_version": 2,
        "generated_at": generated_at,
        "observed_at": max(observed_times) if observed_times else generated_at,
        "marketplace": marketplace,
        "snapshot_type": "month" if matrix_month else "current",
        "month": matrix_month,
        "scope": f"month_{matrix_month}_top100" if matrix_month else "current_top100",
        "asins": direct_asins,
        "page_size": PAGE_SIZE,
        "page_limit": 1,
        "coverage": coverage,
        "records": records,
    }
    out_path = os.path.join(cache_dir, "asin_keyword_matrix.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    success_n = sum(1 for c in coverage.values() if c.get("request_success"))
    print(f"OK 当前ASIN矩阵缓存：{len(direct_asins)} 个 ASIN，{len(records)} 条记录，成功 {success_n}/{len(direct_asins)}")
    print(f"口径: {out.get('scope')} | 输出: {out_path}")


if __name__ == "__main__":
    main()
