#!/usr/bin/env python3
"""build_excel_report.py — 生成 13 Sheet 的关键词市场调研工作簿（结构见 references/report-schema.md）

options.matrix_enabled 为假时不产出 08/09 两张 ASIN 矩阵表（共 11 Sheet，顺序不变），
矩阵相关口径文案如实标注「本次未勾选矩阵，未调用 traffic_keyword」；字段缺失时按勾选处理。

读取 <workdir>/cache 下的 keyword_labeled.json / trend_analysis.json / analysis.json /
call_log.json 以及 input.json、plan.json，产出 output/{站点}_{品类标签}_{口径}_{日期}.xlsx。

硬约束：
- 数据表不合并单元格；启用筛选；冻结首行（宽表另冻结关键词列）；
- 百分比/PPC 为数值格式；缺失留空白；原始关键词不被替换；
- 流量性质/生命周期/象限/机会类型等枚举一律脚本侧映射为中文（不依赖 LLM 输出语言）；
- 看板固定 4 张图（搜索量折线/ABA Rank 折线/柱状/散点），由本脚本一次生成。

用法:
  python build_excel_report.py --workdir <任务目录> [--template assets/keyword_market_report_template.xlsx]

注意：多站点调研请按站点分别建 workdir 运行全流程（站点数据不得混算）。
"""
import argparse
import json
import math
import os
import re
import unicodedata
from datetime import datetime
from statistics import median

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference, ScatterChart, Series
from openpyxl.chart.data_source import AxDataSource, StrRef
from openpyxl.chart.marker import Marker
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FILL_CORE = PatternFill("solid", fgColor="DDEBF7")      # 核心词-浅蓝
FILL_LONGTAIL = PatternFill("solid", fgColor="FCE4D6")  # 长尾词-浅橙
FILL_GOOD = PatternFill("solid", fgColor="C6EFCE")      # 高机会-绿
FILL_BAD = PatternFill("solid", fgColor="FFC7CE")       # 高壁垒-红
FILL_WARN = PatternFill("solid", fgColor="FFEB9C")      # 提示-黄
FILL_MISSING = PatternFill("solid", fgColor="D9D9D9")   # 缺失-灰
FILL_HEADER = PatternFill("solid", fgColor="1F4E79")
FONT_HEADER = Font(color="FFFFFF", bold=True)
PCT_FMT = "0.0%"
NUM_FMT = "#,##0"
DEC_FMT = "0.00"

CURRENCY = {"US": '"$"#,##0.00', "DE": '"€"#,##0.00', "FR": '"€"#,##0.00', "IT": '"€"#,##0.00',
            "ES": '"€"#,##0.00', "NL": '"€"#,##0.00', "UK": '"£"#,##0.00', "JP": '"¥"#,##0',
            "CA": '"C$"#,##0.00', "MX": '"MX$"#,##0.00', "AU": '"A$"#,##0.00'}

# ---------- 枚举 → 中文固定映射（脚本侧收口，不依赖 LLM 语言） ----------
TRAFFIC_TYPE_CN = {"core_head": "核心大词", "precise_category": "精准品类词",
                   "high_intent_longtail": "高意图长尾", "audience_longtail": "人群长尾",
                   "attribute_longtail": "属性长尾",
                   "scene_longtail": "场景长尾", "broad_traffic": "泛流量词",
                   "brand_word": "品牌词", "wrong_category": "错类词"}
LIFECYCLE_CN = {"growth": "结构性增长", "structural_growth": "结构性增长", "mature": "稳定成熟",
                "seasonal": "强季节性", "strong_seasonal": "强季节性", "spike": "短期异动",
                "migration": "需求迁移", "decline": "衰退", "unknown": "待验证"}
QUADRANT_CN = {"high-high": "高点击-高转化", "high-low": "高点击-低转化",
               "low-high": "低点击-高转化", "low-low": "低点击-低转化",
               "高-高": "高点击-高转化", "高-低": "高点击-低转化",
               "低-高": "低点击-高转化", "低-低": "低点击-低转化"}
OPPORTUNITY_CN = {"core": "核心机会", "precise": "精准机会", "longtail": "长尾机会",
                  "differentiation": "差异化机会", "cost_pressure": "成本压力词",
                  "monopoly": "头部垄断词", "trap": "泛流量陷阱", "declining": "衰退词",
                  "seasonal": "季节性机会"}
TAG_PREFIX_CN = {"core_category": "核心品类", "core": "核心品类", "product_form": "产品形态",
                 "form": "形态", "material": "材料", "function": "功能", "spec": "规格",
                 "scene": "场景", "audience": "人群", "bundle": "套装", "accessory": "配件",
                 "gift": "礼品节日", "brand": "品牌竞品", "broad": "泛相关", "noise": "噪声",
                 "theme": "主题"}
CONFIDENCE_CN = {"high": "高", "mid": "中", "medium": "中", "low": "低"}
MODE_INFO = {  # 输入模式：口径中文名 / 文件名口径段
    "M1": ("市场+本品对比（ASIN+Node）", "ASIN+Node对比"),
    "M2": ("人工细分调研（多ASIN）", "细分ASIN调研"),
    "M3": ("市场环境（Node榜单）", "Node榜单"),
    "M4": ("单ASIN快速诊断", "单ASIN诊断"),
}


def bind_string_categories(series, reference):
    """Bind an explicit text category range to one chart series.

    ChartBase.set_categories() only updates series that already exist.  The
    trend charts create explicitly named series one by one, so assigning the
    category source directly prevents Excel from falling back to 1..N.
    """
    series.cat = AxDataSource(strRef=StrRef(f=str(reference)))

SHEET_NAMES = ["00_市场看板", "01_关键词总表", "02_需求分类汇总", "03_24月搜索量_宽表",
               "04_24月搜索量_长表", "05_趋势与季节性", "06_集中度与机会矩阵",
               "07_产品方向", "08_ASIN自然广告位矩阵", "09_ASIN流量占比矩阵",
               "10_市场进入与风险", "11_调研范围与调用", "12_数据质量"]
MATRIX_SHEETS = ["08_ASIN自然广告位矩阵", "09_ASIN流量占比矩阵"]


def active_sheet_names(matrix_enabled):
    """产出哪些 Sheet：未勾选矩阵时两张 ASIN 矩阵表不产出，其余顺序不变。"""
    return list(SHEET_NAMES) if matrix_enabled else [n for n in SHEET_NAMES
                                                     if n not in MATRIX_SHEETS]

LINE_COLORS = ["4472C4", "ED7D31", "70AD47", "A5A5A5", "FFC000", "5B9BD5",
               "C00000", "7030A0", "00B0F0", "92D050", "264478", "9E480E",
               "43682B", "636363", "997300", "255E91", "7F0000", "4C2060",
               "0080A0", "548235"]


def detect_mode(group):
    """M1=ASIN+Node / M2=多ASIN / M3=纯Node / M4=单ASIN"""
    asins, nodes = group.get("asins") or [], group.get("node_ids") or []
    if asins and nodes:
        return "M1"
    if len(asins) >= 2:
        return "M2"
    if nodes:
        return "M3"
    return "M4"


def cn_lifecycle(text):
    """把 'growth + strong_seasonal' 这类混合写法逐段映射为中文。"""
    if not text:
        return ""
    parts = re.split(r"[+/,，、;；]+|\s{2,}", str(text))
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        key = p.lower().replace(" ", "_").replace("（", "(").split("(")[0]
        out.append(LIFECYCLE_CN.get(key, p))
    # 去重保序
    seen = []
    for o in out:
        if o not in seen:
            seen.append(o)
    return " + ".join(seen)


def cn_tag(tag):
    """'material:rattan' -> '材料:rattan'；无法识别的前缀保留原文。"""
    if not tag:
        return ""
    s = str(tag)
    if ":" in s:
        pre, val = s.split(":", 1)
        return f"{TAG_PREFIX_CN.get(pre, pre)}:{val}"
    return TAG_PREFIX_CN.get(s, s)


def sanitize_filename(s):
    return re.sub(r'[\\/:*?"<>|]+', "-", str(s)).strip() or "未分类"


def normalize_key(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower().strip()
    text = re.sub(r"[^\w\s'\-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def rank_slot(rec, prefix):
    """只用接口给出的页码和页内位；不从总排名反推页码。"""
    page = rec.get(f"{prefix}_page")
    index = rec.get(f"{prefix}_index")
    if page in (None, "") or index in (None, ""):
        return ""
    try:
        return f"P{int(page)}-{int(index)}"
    except (TypeError, ValueError):
        return ""


def combined_rank(rec):
    natural = rank_slot(rec, "natural")
    ad = rank_slot(rec, "ad")
    if not natural and not ad:
        return None
    return f"{natural}/{ad}"


def dynamic_percent_axis(values, padding=0.03, step=0.05, min_span=0.15):
    """按数据范围生成可读的百分比轴：外扩、5个百分点取整、最小15个百分点。"""
    vals = [min(1.0, max(0.0, float(v))) for v in values if isinstance(v, (int, float))]
    if not vals:
        return 0.0, 1.0
    lower_target = max(0.0, min(vals) - padding)
    upper_target = min(1.0, max(vals) + padding)
    lower = max(0.0, math.floor((lower_target + 1e-12) / step) * step)
    upper = min(1.0, math.ceil((upper_target - 1e-12) / step) * step)
    while upper - lower < min_span - 1e-12:
        if lower > 0:
            lower = max(0.0, lower - step)
        elif upper < 1:
            upper = min(1.0, upper + step)
        else:
            break
        if upper - lower < min_span - 1e-12 and upper < 1:
            upper = min(1.0, upper + step)
    return round(lower, 10), round(upper, 10)


def load_json(path, required=True, default=None):
    if not os.path.exists(path):
        if required:
            raise SystemExit(f"缺少必需文件: {path}")
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def blank(v):
    return "" if v is None else v


def style_header(ws, ncols, row=1):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = FILL_HEADER
        cell.font = FONT_HEADER
        cell.alignment = Alignment(vertical="center")


def finalize_data_sheet(ws, ncols, nrows, freeze="A2"):
    ws.freeze_panes = freeze
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{max(nrows, 1)}"
    for c in range(1, ncols + 1):
        ws.column_dimensions[get_column_letter(c)].width = 16


def put_rows(ws, header, rows, formats=None):
    ws.append(header)
    style_header(ws, len(header))
    for r in rows:
        ws.append([blank(v) for v in r])
    if formats:
        for col_idx, fmt in formats.items():
            for row in range(2, ws.max_row + 1):
                cell = ws.cell(row=row, column=col_idx)
                if cell.value != "" and cell.value is not None:
                    cell.number_format = fmt
    finalize_data_sheet(ws, len(header), ws.max_row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--template", default=None)
    args = ap.parse_args()

    wd = args.workdir
    cache = os.path.join(wd, "cache")
    input_cfg = load_json(os.path.join(wd, "input.json"))
    plan = load_json(os.path.join(wd, "plan.json"), required=False, default={})
    labeled = load_json(os.path.join(cache, "keyword_labeled.json"))
    trends = load_json(os.path.join(cache, "trend_analysis.json"))
    analysis = load_json(os.path.join(cache, "analysis.json"))
    call_log = load_json(os.path.join(cache, "call_log.json"), required=False, default={"calls": []})
    pool = load_json(os.path.join(cache, "asin_pool.json"), required=False, default=None)
    asin_matrix = load_json(os.path.join(cache, "asin_keyword_matrix.json"), required=False,
                            default={"asins": [], "coverage": {}, "records": []})

    group = (input_cfg.get("groups") or [{}])[0]
    marketplace = group.get("marketplace", "US")
    mode = detect_mode(group)
    mode_cn, mode_file = MODE_INFO[mode]
    cur_fmt = CURRENCY.get(marketplace, NUM_FMT)
    options_cfg = input_cfg.get("options") or {}
    matrix_enabled = bool(options_cfg.get("matrix_enabled", True))
    data_month = str(options_cfg.get("data_month") or datetime.now().strftime("%Y%m"))
    if not re.fullmatch(r"\d{6}", data_month):
        raise SystemExit("options.data_month 必须为 yyyyMM，例如 202607。")
    # 未勾选矩阵时矩阵缓存没有观测，不把本地聚合脚本的生成时间冒充采集时刻
    matrix_observed_at = str(asin_matrix.get("observed_at") or "") if matrix_enabled else ""
    direct_asins = []
    for g in input_cfg.get("groups", []):
        for asin in g.get("asins") or []:
            asin = str(asin).upper()
            if asin not in direct_asins:
                direct_asins.append(asin)
    # ASIN矩阵口径：未勾选矩阵与「纯Node模式（无输入ASIN）」是两回事，不许共用一句文案
    if not matrix_enabled:
        matrix_scope_note = "本次未勾选矩阵，未调用 traffic_keyword"
    elif direct_asins:
        matrix_scope_note = "当前前100流量词匹配"
    else:
        matrix_scope_note = "纯Node模式，不调用"
    kw_type_map = {k: v["type"] for k, v in trends["keywords"].items()}
    kw_cn_map = {r["keyword"]: (r.get("translation_fixed") or r.get("keywordCn") or "")
                 for r in labeled["keywords"]}
    traffic_type_map = {r["keyword"]: r.get("traffic_type") for r in labeled["keywords"]}

    # 品类标签：LLM 在 analysis.json 产出 category_label；缺失时回退首个核心趋势词
    category_label = analysis.get("category_label") or next(
        (k for k, v in trends["keywords"].items() if v["type"] == "core"), "未分类")

    # ---------- M1 对比集（脚本确定性计算，不新增 MCP 调用） ----------
    input_asins = set(group.get("asins") or [])
    node_asins = {a.get("asin") for a in (pool or {}).get("asins", [])
                  if any(str(s).startswith("node:") for s in a.get("sources", []))}
    comparison = None
    if mode == "M1" and input_asins:
        shared, gap, diff = [], [], []
        for r in labeled["keywords"]:
            srcs = set(r.get("source_asins") or [])
            in_input = bool(srcs & input_asins)
            in_market = bool(srcs & node_asins) if node_asins else bool(srcs - input_asins)
            row = [r["keyword"], kw_cn_map.get(r["keyword"], ""), r.get("searches"),
                   r.get("purchaseRate"),
                   r.get("monopolyClickRate") if r.get("monopolyClickRate") is not None else r.get("top3ClickingRate"),
                   r.get("top3ConversionRate"), r.get("bid")]
            if in_input and in_market:
                shared.append(row)
            elif in_market:
                gap.append(row)
            elif in_input:
                diff.append(row)
        key = lambda x: (x[2] or 0)
        comparison = {"shared": sorted(shared, key=key, reverse=True),
                      "gap": sorted(gap, key=key, reverse=True),
                      "diff": sorted(diff, key=key, reverse=True)}

    if args.template and os.path.exists(args.template):
        wb = load_workbook(args.template)
        for name in list(wb.sheetnames):
            del wb[name]
    else:
        wb = Workbook()
        wb.remove(wb.active)
    sheets = {name: wb.create_sheet(name) for name in active_sheet_names(matrix_enabled)}

    # ---------- 01 调研范围与调用 ----------
    ws = sheets["11_调研范围与调用"]
    rows = [["调研口径", mode_cn]]
    rows.append(["关键词总表快照月份", data_month])
    rows.append(["ASIN矩阵采集时刻", matrix_observed_at])
    rows.append(["ASIN矩阵口径", matrix_scope_note])
    rows.append(["24月趋势截止月份", trends.get("end_month", "")])
    pool_pages = labeled.get("keyword_pool_pages", (input_cfg.get("options") or {}).get("keyword_pool_pages", 3))
    rows.append(["关键词池采集口径", f"固定 {pool_pages} 页×50词；三页去重后全部收录，不请求第4页"])
    rows.append(["关键词池各页返回", "; ".join(
        f"批{p.get('batch')}-P{p.get('page')}={p.get('returned')}"
        for p in (labeled.get("keyword_pool_page_stats") or []))])
    rows.append(["关键词池原始行数", labeled.get("keyword_pool_raw_rows", "")])
    rows.append(["关键词池跨页重复行数", labeled.get("keyword_pool_cross_page_duplicates", "")])
    rows.append(["关键词池去重后实际数", labeled.get("keyword_pool_actual", labeled.get("keyword_count", len(labeled.get("keywords") or [])))])
    for g in input_cfg.get("groups", []):
        rows.append(["站点", g.get("marketplace")])
        rows.append(["输入ASIN", ", ".join(g.get("asins") or [])])
        rows.append(["输入Node", ", ".join(str(n) for n in (g.get("node_ids") or []))])
        rows.append(["Top N", g.get("node_top_n", 50)])
    if pool:
        rows.append(["去重后ASIN数", len(pool.get("asins", []))])
    rows.append(["计划调用量", plan.get("planned_total", call_log.get("planned_total", ""))])
    rows.append(["实际调用量", call_log.get("actual_total", len(call_log.get("calls", [])))])
    retries = [c for c in call_log.get("calls", []) if c.get("is_retry")]
    rows.append(["补调用次数", len(retries)])
    rows.append(["生成时间", datetime.now().isoformat(timespec="seconds")])
    for r in rows:
        ws.append(r)
    ws.append([])
    ws.append(["调用序号", "工具", "范围", "返回条数", "成功", "截断", "补调用", "补调用原因"])
    style_header(ws, 8, row=ws.max_row)
    for c in call_log.get("calls", []):
        ws.append([c.get("seq"), c.get("tool"), c.get("scope"), c.get("returned"),
                   "是" if c.get("success") else "否", "是" if c.get("truncated") else "否",
                   "是" if c.get("is_retry") else "否", c.get("retry_reason") or ""])
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 40

    # ---------- 02 关键词总表 ----------
    ws = sheets["01_关键词总表"]
    header = ["数据月份", "原始关键词", "MCP中文翻译", "校正中文", "标准词族", "需求标签", "流量性质", "流量性质理由", "相关度",
              "相关度理由", "购买意图", "月搜索量", "月购买量", "购买率", "点击集中度", "转化集中度",
              "PPC默认", "PPC最低", "PPC最高", "商品数", "1日广告竞品数", "30日广告竞品数",
              "供需比", "标题密度", "当前趋势资格", "来源标识", "数据质量备注"]
    rows = []
    for r in labeled["keywords"]:
        click_conc = r.get("monopolyClickRate") if r.get("monopolyClickRate") is not None else r.get("top3ClickingRate")
        trend_mark = {"core": "核心趋势词", "longtail": "长尾趋势词"}.get(kw_type_map.get(r["keyword"]), "")
        traffic_raw = r.get("traffic_type") or ""
        traffic_cn = TRAFFIC_TYPE_CN.get(traffic_raw, traffic_raw)
        tags_cn = "; ".join(cn_tag(t) for t in (r.get("demand_tags") or []))
        rows.append([
            data_month, r["keyword"], r.get("keywordCn"), r.get("translation_fixed"), r.get("family"),
            tags_cn, traffic_cn, r.get("traffic_type_reason"), r.get("relevance"),
            r.get("relevance_reason"), r.get("purchase_intent"), r.get("searches"), r.get("purchases"),
            r.get("purchaseRate"), click_conc, r.get("top3ConversionRate"),
            r.get("bid"), r.get("bidMin"), r.get("bidMax"), r.get("products"),
            r.get("latest1daysAds"), r.get("latest30daysAds"), r.get("supplyDemandRatio"),
            r.get("titleDensity"), trend_mark, ", ".join(r.get("source_asins") or []),
            " | ".join(r.get("quality_notes") or []),
        ])
    put_rows(ws, header, rows, formats={
        12: NUM_FMT, 13: NUM_FMT, 14: PCT_FMT, 15: PCT_FMT, 16: PCT_FMT,
        17: cur_fmt, 18: cur_fmt, 19: cur_fmt, 20: NUM_FMT, 21: NUM_FMT, 22: NUM_FMT,
        23: DEC_FMT, 24: DEC_FMT})
    for i, r in enumerate(labeled["keywords"], start=2):
        t = kw_type_map.get(r["keyword"])
        if t == "core":
            ws.cell(row=i, column=2).fill = FILL_CORE
        elif t == "longtail":
            ws.cell(row=i, column=2).fill = FILL_LONGTAIL
        if r.get("searches") is None:
            ws.cell(row=i, column=12).fill = FILL_MISSING
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["H"].width = 34
    ws.column_dimensions["J"].width = 34
    ws.column_dimensions["AA"].width = 40

    # ---------- 03 需求分类汇总 ----------
    ws = sheets["02_需求分类汇总"]
    header = ["需求标签", "关键词数", "代表词", "搜索量中位数", "购买率中位数", "点击集中度中位数",
              "转化集中度中位数", "当前PPC中位数", "趋势方向", "产品含义"]
    rows = [[cn_tag(d.get("tag")), d.get("kw_count"), ", ".join(d.get("representative") or []),
             d.get("searches_median"), d.get("purchase_rate_median"), d.get("click_conc_median"),
             d.get("conv_conc_median"), d.get("ppc_median"), d.get("trend_direction"),
             d.get("product_implication")] for d in analysis.get("demand_map", [])]
    put_rows(ws, header, rows, formats={4: NUM_FMT, 5: PCT_FMT, 6: PCT_FMT, 7: PCT_FMT, 8: cur_fmt})

    # ---------- 04 宽表 ----------
    ws = sheets["03_24月搜索量_宽表"]
    months = trends["months"]
    header = ["关键词", "中文翻译", "类型"] + months
    ws.append(header)
    style_header(ws, len(header))
    for kw, rec in trends["keywords"].items():
        tname = "核心" if rec["type"] == "core" else "长尾"
        ws.append([kw, kw_cn_map.get(kw, ""), tname] + [blank(p["searches"]) for p in rec["series"]])
        fill = FILL_CORE if rec["type"] == "core" else FILL_LONGTAIL
        ws.cell(row=ws.max_row, column=1).fill = fill
    nrows, ncols = ws.max_row, ws.max_column
    if nrows >= 2 and ncols >= 5:
        rng = f"D2:{get_column_letter(ncols)}{nrows}"
        ws.conditional_formatting.add(rng, ColorScaleRule(
            start_type="min", start_color="FFFFFF", end_type="max", end_color="63BE7B"))
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{nrows}"
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 22
    for c in range(4, ncols + 1):
        ws.column_dimensions[get_column_letter(c)].width = 11
        for row in range(2, nrows + 1):
            cell = ws.cell(row=row, column=c)
            if cell.value not in ("", None):
                cell.number_format = NUM_FMT
            else:
                cell.fill = FILL_MISSING

    # ---------- 05 长表 ----------
    ws = sheets["04_24月搜索量_长表"]
    header = ["marketplace", "keyword", "中文翻译", "关键词类型", "month", "searches",
              "ABA rank", "MoM", "YoY", "seasonality_index"]
    rows = []
    rank_row_ranges = {}
    for kw, rec in trends["keywords"].items():
        tname = "核心" if rec["type"] == "core" else "长尾"
        start_row = len(rows) + 2
        for p in rec["series"]:
            rows.append([marketplace, kw, kw_cn_map.get(kw, ""), tname, p["month"],
                         p["searches"], p.get("aba_rank"), p.get("mom"), p.get("yoy"),
                         p.get("seasonality_index")])
        rank_row_ranges[kw] = (start_row, len(rows) + 1)
    put_rows(ws, header, rows, formats={6: NUM_FMT, 8: PCT_FMT, 9: PCT_FMT, 10: DEC_FMT})

    # ---------- 06 趋势与季节性 ----------
    ws = sheets["05_趋势与季节性"]
    header = ["关键词", "类型", "24个月均值", "24个月中位数", "最新月搜索量", "MoM", "YoY",
              "近3月增长率", "近6月增长率", "12月同比累计", "变异系数", "峰谷比", "峰值月份",
              "谷值月份", "连续增长月数", "增长拐点", "两年重复度", "缺月", "生命周期", "进入时间建议"]
    rows = []
    for kw, rec in trends["keywords"].items():
        s = rec["stats"]
        rows.append([kw, "核心" if rec["type"] == "core" else "长尾", s.get("avg_24m"),
                     s.get("median_24m"), s.get("latest_searches"), s.get("mom"), s.get("yoy"),
                     s.get("growth_3m"), s.get("growth_6m"), s.get("yoy_cumulative_12m"),
                     s.get("cv"), s.get("peak_trough_ratio"), s.get("peak_month"),
                     s.get("trough_month"), s.get("consecutive_growth_months"),
                     s.get("first_growth_inflection"), s.get("two_year_repeat_score"),
                     ", ".join(s.get("missing_months") or []),
                     cn_lifecycle(analysis.get("lifecycle", {}).get("classification", "")),
                     analysis.get("seasonality", {}).get("timeline_advice", "")])
    put_rows(ws, header, rows, formats={3: NUM_FMT, 4: NUM_FMT, 5: NUM_FMT, 6: PCT_FMT, 7: PCT_FMT,
                                        8: PCT_FMT, 9: PCT_FMT, 10: PCT_FMT, 11: DEC_FMT, 12: DEC_FMT,
                                        17: PCT_FMT})

    # ---------- 07 集中度与机会矩阵 ----------
    ws = sheets["06_集中度与机会矩阵"]
    quad = analysis.get("quadrant", {})
    ws.append(["点击集中度中位数基准", quad.get("click_median", ""), "转化集中度中位数基准", quad.get("conv_median", "")])
    ws.append([])

    # 象限明细：点击/转化为空的词不入象限；象限归属由脚本按中位数重算（不信 LLM 字符串）
    quad_items_raw = quad.get("items", [])
    click_med, conv_med = quad.get("click_median"), quad.get("conv_median")
    quad_clean = []
    for it in quad_items_raw:
        c, v = it.get("click"), it.get("conv")
        if c is None or v is None:
            continue
        if isinstance(click_med, (int, float)) and isinstance(conv_med, (int, float)):
            q = f"{'high' if c >= click_med else 'low'}-{'high' if v >= conv_med else 'low'}"
        else:
            q = str(it.get("quadrant", "")).lower()
        quad_clean.append({"keyword": it.get("keyword"), "click": c, "conv": v,
                           "quadrant": QUADRANT_CN.get(q, q), "interpretation": it.get("interpretation", "")})

    # M1 对比区（共有词 / 市场缺口词 / 本品差异词）
    cmp_rows = []
    if comparison:
        ws.append(["市场+本品对比", "", "", "", "", "", ""])
        cmp_header = ["词集", "关键词", "中文", "月搜索量", "购买率", "点击集中度", "转化集中度"]
        ws.append(cmp_header + ["PPC"])
        style_header(ws, len(cmp_header) + 1, row=ws.max_row)
        for label, rows_ in (("共有词（市场∩本品）", comparison["shared"][:15]),
                             ("市场缺口词（市场有·本品无）", comparison["gap"][:15]),
                             ("本品差异词（本品有·头部无）", comparison["diff"][:15])):
            for r in rows_:
                ws.append([label, r[0], r[1], r[2], r[3], r[4], r[5], r[6]])
                cmp_rows.append(ws.max_row)
            if not rows_:
                ws.append([label, "（无）", "", "", "", "", "", ""])
        ws.append([])

    header = ["关键词", "点击集中度", "转化集中度", "象限", "机会类型", "数据证据", "置信度", "解释"]
    ws.append(header)
    style_header(ws, len(header), row=ws.max_row)
    matrix_rows = []
    quad_items = {i["keyword"]: i for i in quad_clean}
    for item in analysis.get("opportunity_matrix", []):
        kw = item.get("keyword")
        qi = quad_items.get(kw, {})
        ev = item.get("evidence", {})
        ev_text = "; ".join(f"{k}={v}" for k, v in ev.items() if v is not None)
        ws.append([kw, qi.get("click"), qi.get("conv"), qi.get("quadrant", ""),
                   OPPORTUNITY_CN.get(item.get("type"), item.get("type")), ev_text,
                   CONFIDENCE_CN.get(str(item.get("confidence", "")).lower(), item.get("confidence")),
                   qi.get("interpretation", "")])
        t = item.get("type")
        cell = ws.cell(row=ws.max_row, column=5)
        if t in ("core", "precise", "longtail", "differentiation", "seasonal"):
            cell.fill = FILL_GOOD
        elif t in ("monopoly", "trap", "declining", "cost_pressure"):
            cell.fill = FILL_BAD
        matrix_rows.append(ws.max_row)
    matrix_end = ws.max_row

    # 2×2 象限表（表格为主，图为辅）
    ws.append([])
    ws.append(["点击—转化集中度 2×2 象限表", "", ""])
    style_header(ws, 3, row=ws.max_row)
    q_rows = {"high": {"high": [], "low": []}, "low": {"high": [], "low": []}}
    if isinstance(click_med, (int, float)) and isinstance(conv_med, (int, float)):
        for it in quad_clean:
            ck = "high" if it["click"] >= click_med else "low"
            vk = "high" if it["conv"] >= conv_med else "low"
            q_rows[ck][vk].append(f"{it['keyword']}({it['click']:.1%}/{it['conv']:.1%})")
    ws.append(["", "高转化（≥中位数）", "低转化（<中位数）"])
    style_header(ws, 3, row=ws.max_row)
    ws.append(["高点击（≥中位数）", "\n".join(q_rows["high"]["high"]), "\n".join(q_rows["high"]["low"])])
    ws.append(["低点击（<中位数）", "\n".join(q_rows["low"]["high"]), "\n".join(q_rows["low"]["low"])])
    for row in ws.iter_rows(min_row=ws.max_row - 1, max_row=ws.max_row, min_col=2, max_col=3):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws.freeze_panes = "A4"
    for row in cmp_rows:  # 对比区：4=搜索量 5/6/7=比率 8=PPC
        for col, fmt in ((4, NUM_FMT), (5, PCT_FMT), (6, PCT_FMT), (7, PCT_FMT), (8, cur_fmt)):
            cell = ws.cell(row=row, column=col)
            if isinstance(cell.value, (int, float)):
                cell.number_format = fmt
    for row in matrix_rows:  # 矩阵区：2/3=集中度
        for col in (2, 3):
            cell = ws.cell(row=row, column=col)
            if isinstance(cell.value, (int, float)):
                cell.number_format = PCT_FMT
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["F"].width = 50
    ws.column_dimensions["H"].width = 44

    # 散点图数据区（J 列起，按总表流量性质分组；品牌/错类/泛流量不入图）
    chart_anchor_row = matrix_end
    scatter_groups = {"核心/精准词": [], "人群长尾": [], "其他长尾": []}
    for it in quad_clean:
        t = traffic_type_map.get(it["keyword"])
        if t in {"core_head", "precise_category"}:
            scatter_groups["核心/精准词"].append(it)
        elif t == "audience_longtail":
            scatter_groups["人群长尾"].append(it)
        elif t in {"attribute_longtail", "scene_longtail", "high_intent_longtail"}:
            scatter_groups["其他长尾"].append(it)
    col = 10  # J
    series_refs = []
    for gname, items in scatter_groups.items():
        if not items:
            continue
        ws.cell(row=1, column=col, value=f"{gname}_click")
        ws.cell(row=1, column=col + 1, value=f"{gname}_conv")
        for i, it in enumerate(items, start=2):
            ws.cell(row=i, column=col, value=it["click"]).number_format = PCT_FMT
            ws.cell(row=i, column=col + 1, value=it["conv"]).number_format = PCT_FMT
        series_refs.append((gname, col, len(items)))
        col += 2
    x_values = [it["click"] for items in scatter_groups.values() for it in items]
    y_values = [it["conv"] for items in scatter_groups.values() for it in items]
    x_axis_min, x_axis_max = dynamic_percent_axis(x_values)
    y_axis_min, y_axis_max = dynamic_percent_axis(y_values)

    # 中位十字线数据（仅覆盖局部动态轴范围）
    cross_refs = None
    if isinstance(click_med, (int, float)) and isinstance(conv_med, (int, float)):
        ws.cell(row=1, column=col, value="vertical_x")
        ws.cell(row=1, column=col + 1, value="vertical_y")
        ws.cell(row=1, column=col + 2, value="horizontal_x")
        ws.cell(row=1, column=col + 3, value="horizontal_y")
        # 使用21个密集标记组成点状参考线。这样在Excel/WPS/Artifact Tool中
        # 都能保持普通关键词只显示散点，同时横/竖中位参考线仍各自为独立系列。
        for i in range(21):
            row = i + 2
            x_axis_value = x_axis_min + (x_axis_max - x_axis_min) * i / 20
            y_axis_value = y_axis_min + (y_axis_max - y_axis_min) * i / 20
            ws.cell(row=row, column=col, value=click_med)
            ws.cell(row=row, column=col + 1, value=y_axis_value)
            ws.cell(row=row, column=col + 2, value=x_axis_value)
            ws.cell(row=row, column=col + 3, value=conv_med)
        cross_refs = (col, col + 2, 21)
    # 隐藏数据区列宽
    for c in range(10, col + (4 if cross_refs else 2)):
        ws.column_dimensions[get_column_letter(c)].hidden = True

    # ---------- 07 产品方向 ----------
    ws = sheets["07_产品方向"]
    header = ["需求板块", "证据关键词", "数据证据", "用户可能问题（推断）", "产品形态", "材料", "功能", "规格",
              "套装/配件", "产品响应假设", "置信度", "需要验证的数据"]
    rows = []
    for h in analysis.get("product_hypotheses", []):
        ev = h.get("evidence") or {}
        ev_text = "; ".join(f"{k}={v}" for k, v in ev.items() if v is not None)
        rows.append([cn_tag(h.get("demand")), ", ".join(h.get("source_keywords") or []), ev_text,
                     h.get("user_problem"), h.get("product_form", ""), h.get("material", ""),
                     h.get("function", ""), h.get("spec", ""), h.get("bundle_accessory", ""),
                     h.get("product_response"), CONFIDENCE_CN.get(h.get("confidence"), h.get("confidence")),
                     h.get("validation_needed")])
    put_rows(ws, header, rows)
    for c, w in zip("ABCDEFGHIJKL", (22, 34, 40, 30, 16, 14, 18, 14, 18, 40, 12, 34)):
        ws.column_dimensions[c].width = w

    # ---------- 09 市场进入与风险 ----------
    ws = sheets["10_市场进入与风险"]
    entry = analysis.get("entry_conclusion", {})
    risk = analysis.get("risk_checklist", {})
    grade_cn = {"GO": "值得优先推进", "CONDITIONAL": "有条件推进",
                "VALIDATE": "继续验证", "HOLD": "暂不建议进入"}
    ws.append(["调研口径", mode_cn])
    if mode == "M4":
        ws.append(["口径限制", "样本为单竞品流量面，不代表市场整体；以下结论为初步判断，建议补充 Node 榜单调研（ASIN+Node 对比模式）验证市场面。"])
        ws.cell(row=2, column=2).fill = FILL_WARN
    ws.append(["市场进入等级", grade_cn.get(entry.get("grade"), entry.get("grade", ""))])
    grade_cell = ws.cell(row=ws.max_row, column=2)
    grade_cell.fill = FILL_GOOD if entry.get("grade") == "GO" else (
        FILL_BAD if entry.get("grade") == "HOLD" else FILL_WARN)
    ws.append(["生命周期", cn_lifecycle(analysis.get("lifecycle", {}).get("classification", ""))])
    ws.append(["生命周期证据", analysis.get("lifecycle", {}).get("evidence", "")])
    ws.append(["结论证据", entry.get("evidence", "")])
    ws.append(["限制条件", entry.get("limitations", "")])
    ws.append(["时间窗口", analysis.get("seasonality", {}).get("timeline_advice", "")])
    ws.append([])
    answers = entry.get("answers", {})
    q_labels = {"q1": "需求真实性/稳定性", "q2": "竞争壁垒来源", "q3": "最值得验证的产品方向",
                "q4": "最适合切入的需求", "q5": "是否值得继续调研", "q6": "何时启动产品/供应链",
                "q7": "仍缺哪些数据"}
    ws.append(["七个必答问题", "回答"])
    style_header(ws, 2, row=ws.max_row)
    for q, label in q_labels.items():
        ws.append([label, answers.get(q, "")])
    ws.append([])
    ws.append(["不能证明的事项", ""])
    style_header(ws, 2, row=ws.max_row)
    for x in risk.get("cannot_prove", []):
        ws.append([x, ""])
    ws.append([])
    ws.append(["下一步验证清单", ""])
    style_header(ws, 2, row=ws.max_row)
    for x in risk.get("next_validations", []):
        ws.append([x, ""])
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 90
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    # ---------- 10 数据质量 ----------
    ws = sheets["12_数据质量"]
    header = ["类别", "对象", "说明", "是否影响结论"]
    ws.append(header)
    style_header(ws, len(header))
    analysis_mode = analysis.get("analysis_mode", "legacy_single")
    if analysis_mode in {"hybrid_fallback", "deterministic_fallback"}:
        failed_sections = [name for name, meta in (analysis.get("analysis_sections") or {}).items()
                           if meta.get("status") == "degraded"]
        ws.append([
            "分析降级", "模型分析",
            f"分析模式={analysis_mode}；降级分段={', '.join(failed_sections) or '未知'}。报告保留事实数据，产品假设可能为空，进入等级强制为继续验证。",
            "是",
        ])
        for cell in ws[ws.max_row]:
            cell.fill = FILL_BAD
    missing_fields = []
    for r in labeled["keywords"]:
        for f in ("searches", "purchaseRate", "top3ConversionRate"):
            if r.get(f) is None:
                missing_fields.append((r["keyword"], f))
    for kw, f in missing_fields[:200]:
        ws.append(["缺失字段", kw, f"字段 {f} 缺失（留空，未填0）", "待评估"])
    # 转化集中度稀疏度（象限图可信度提示）
    conv_vals = [r.get("top3ConversionRate") for r in labeled["keywords"]]
    conv_valid = [v for v in conv_vals if isinstance(v, (int, float)) and v > 0]
    conv_sparse = len(conv_vals) > 0 and len(conv_valid) / len(conv_vals) <= 0.5
    if conv_sparse:
        ws.append(["指标稀疏", "top3ConversionRate",
                   f"仅 {len(conv_valid)}/{len(conv_vals)} 个词有非零转化集中度，象限图仅供参考，以 2×2 象限表为准", "是"])
    for r in labeled["keywords"]:
        for note in r.get("quality_notes") or []:
            ws.append(["指标冲突", r["keyword"], note, "待评估"])
        if r.get("translation_fixed") and r.get("keywordCn") and r["translation_fixed"] != r["keywordCn"]:
            ws.append(["翻译修正", r["keyword"],
                       f"{r.get('keywordCn')} -> {r['translation_fixed']}（{r.get('translation_reason') or ''}）", "否"])
    for kw, rec in trends["keywords"].items():
        miss = rec["stats"].get("missing_months") or []
        if miss:
            ws.append(["趋势缺月", kw, ", ".join(miss), "待评估"])
    for c in call_log.get("calls", []):
        if c.get("truncated"):
            ws.append(["响应截断", c.get("scope"), f"{c.get('tool')} 第{c.get('seq')}次调用截断", "待评估"])
        if c.get("is_retry"):
            ws.append(["补调用", c.get("scope"), c.get("retry_reason") or "", "待评估"])
    if labeled.get("keyword_pool_missing_pages"):
        ws.append(["关键词池采集", "缺失计划页",
                   ", ".join(f"批{p.get('batch')}-P{p.get('page')}" for p in labeled["keyword_pool_missing_pages"]),
                   "是"])
    ws.append(["关键词池采集", "三页去重",
               f"原始 {labeled.get('keyword_pool_raw_rows', '')} 行；跨页/批次重复 "
               f"{labeled.get('keyword_pool_cross_page_duplicates', '')} 行；去重后 "
               f"{labeled.get('keyword_pool_actual', labeled.get('keyword_count', len(labeled.get('keywords') or [])))} 词；未请求第4页",
               "否"])
    if not matrix_enabled:
        ws.append(["ASIN矩阵", "本次未勾选矩阵",
                   "本次未勾选矩阵，未调用 traffic_keyword；08_ASIN自然广告位矩阵与"
                   "09_ASIN流量占比矩阵两张表本次未产出", "否"])
    elif not direct_asins:
        ws.append(["ASIN矩阵", "纯Node模式", "没有直接输入ASIN，未调用 traffic_keyword；两个矩阵仅保留关键词行", "否"])
    else:
        if asin_matrix.get("snapshot_type") == "current" and asin_matrix.get("scope") == "current_top100":
            ws.append(["ASIN矩阵", "当前前100口径",
                       f"观测时间 {matrix_observed_at}；仅匹配关键词总表，空白表示未进入该ASIN当前前100流量词或字段缺失", "否"])
        elif (asin_matrix.get("snapshot_type") == "month"
              and re.fullmatch(r"month_\d{6}_top100", str(asin_matrix.get("scope") or "")) is not None):
            ws.append(["ASIN矩阵", f"{asin_matrix.get('month')}前100口径",
                       f"快照月份 {asin_matrix.get('month')}；观测时间 {matrix_observed_at}；仅匹配关键词总表，空白表示未进入该ASIN该月前100流量词或字段缺失", "否"])
        else:
            ws.append(["ASIN矩阵", "缓存口径",
                       "不是 current_top100 或 month_yyyyMM_top100 矩阵缓存；禁止用旧历史缓存冒充", "是"])
        for asin in direct_asins:
            cov = (asin_matrix.get("coverage") or {}).get(asin, {})
            if not cov.get("request_success"):
                detail = "; ".join(cov.get("notes") or []) or "当前第1页请求失败或缺失"
                ws.append(["ASIN矩阵采集", asin, detail, "是"])
    finalize_data_sheet(ws, 4, ws.max_row)
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 60

    # ---------- 11/12 ASIN矩阵：同一次 current traffic_keyword 响应分开展示 ----------
    # 未勾选矩阵：这两张表不产出（链路也未调用 traffic_keyword，无矩阵数据可取）
    if matrix_enabled:
        rank_ws = sheets["08_ASIN自然广告位矩阵"]
        traffic_ws = sheets["09_ASIN流量占比矩阵"]
        # 矩阵首列 = 调用 traffic_keyword 时输入的快照月份（matrix_month；未设置=近30天实时）
        # 近30天实时需标注窗口终点（=采集日），便于追溯"从哪天算的30天"
        matrix_snapshot_label = asin_matrix.get("month")
        if not matrix_snapshot_label:
            as_of = str(asin_matrix.get("observed_at") or "")[:10]
            matrix_snapshot_label = f"近30天实时（截至 {as_of}）" if as_of else "近30天实时"
        rank_header = ["快照月份", "原始关键词", "中文翻译"] + [
            f"{asin}|自然位/广告位" for asin in direct_asins]
        traffic_header = ["快照月份", "原始关键词", "中文翻译"] + [
            f"{asin}|流量占比" for asin in direct_asins]
        matrix_records = {}
        for rec in asin_matrix.get("records") or []:
            asin = str(rec.get("asin") or "").upper()
            key = rec.get("normalized_key") or normalize_key(rec.get("keyword"))
            if asin in direct_asins and key:
                matrix_records[(asin, key)] = rec
        rank_rows = []
        traffic_rows = []
        for r in labeled["keywords"]:
            key = r.get("normalized_key") or normalize_key(r.get("keyword"))
            prefix = [matrix_snapshot_label, r["keyword"], kw_cn_map.get(r["keyword"], "")]
            rank_row = list(prefix)
            traffic_row = list(prefix)
            for asin in direct_asins:
                rec = matrix_records.get((asin, key), {})
                rank_row.append(combined_rank(rec))
                traffic_row.append(rec.get("traffic_percentage"))
            rank_rows.append(rank_row)
            traffic_rows.append(traffic_row)
        put_rows(rank_ws, rank_header, rank_rows)
        put_rows(traffic_ws, traffic_header, traffic_rows,
                 formats={col: PCT_FMT for col in range(4, 4 + len(direct_asins))})
        for matrix_ws in (rank_ws, traffic_ws):
            matrix_ws.freeze_panes = "D2" if direct_asins else "B2"
            matrix_ws.column_dimensions["A"].width = 28
            matrix_ws.column_dimensions["B"].width = 34
            matrix_ws.column_dimensions["C"].width = 22
            for col_idx in range(4, matrix_ws.max_column + 1):
                matrix_ws.column_dimensions[get_column_letter(col_idx)].width = 20

    # ---------- 00 市场看板（最后写，引用其他 Sheet 建图） ----------
    ws = sheets["00_市场看板"]
    info = [
        ["SellerSprite 市场关键词调研报告", ""],
        ["调研口径", mode_cn],
        ["站点", marketplace],
        ["品类标签", category_label],
        ["输入范围", "; ".join(
            f"{g.get('marketplace')}: {len(g.get('asins') or [])} ASIN + {len(g.get('node_ids') or [])} Node"
            for g in input_cfg.get("groups", []))],
        ["关键词样本数", labeled.get("keyword_count", len(labeled["keywords"]))],
        ["趋势窗口", f"{trends['months'][0]} ~ {trends['months'][-1]}（{trends.get('window', 24)}个月）"],
        ["关键词总表快照月份", data_month],
        ["ASIN矩阵采集时刻", matrix_observed_at],
        ["ASIN矩阵口径", matrix_scope_note],
        ["24月趋势截止月份", trends.get("end_month", "")],
        ["市场生命周期", cn_lifecycle(analysis.get("lifecycle", {}).get("classification", ""))],
        ["市场进入结论", grade_cn.get(entry.get("grade"), entry.get("grade", ""))],
        ["核心机会", "; ".join(i.get("keyword", "") for i in analysis.get("opportunity_matrix", [])
                          if i.get("type") in ("core", "precise"))],
        ["主要风险", "; ".join(analysis.get("risk_checklist", {}).get("cannot_prove", [])[:3])],
        ["象限图分组", "核心/精准词=核心大词+精准品类词；人群长尾独立；其他长尾=属性/场景/高意图长尾。品牌、错类、泛流量不入图；局部放大，关键词明细见06页。"],
        ["调用量(计划/实际)", f"{plan.get('planned_total', call_log.get('planned_total', ''))} / {call_log.get('actual_total', len(call_log.get('calls', [])))}"],
        ["生成时间", datetime.now().isoformat(timespec="seconds")],
    ]
    if mode == "M4":
        info.insert(2, ["口径限制", "单竞品流量面，结论为初步判断，建议补 Node 榜单验证市场面"])
    if comparison:
        info.insert(8, ["市场+本品对比", f"共有词 {len(comparison['shared'])} / 市场缺口词 {len(comparison['gap'])} / 本品差异词 {len(comparison['diff'])}（明细见 06 表）"])
    for r in info:
        ws.append(r)
    ws["A1"].font = Font(size=16, bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 100
    for row in ws.iter_rows(min_row=2, max_row=len(info)):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    chart_row = len(info) + 3

    trend_order = list(trends["keywords"].keys())
    trend_colors = {kw: LINE_COLORS[i % len(LINE_COLORS)] for i, kw in enumerate(trend_order)}

    # 图1：24 个月搜索量趋势（逐词显式命名，避免 Excel 默认“系列1”）
    wide = sheets["03_24月搜索量_宽表"]
    if wide.max_row >= 2:
        line = LineChart()
        line.title = "核心/长尾词 24 个月搜索量趋势"
        line.height, line.width = 9, 22
        cats = Reference(wide, min_col=4, min_row=1, max_col=wide.max_column)
        line.x_axis.tickLblSkip = 2
        line.display_blanks = "gap"
        line.legend.position = "r"
        for row_idx, kw in enumerate(trend_order, start=2):
            values = Reference(wide, min_col=4, max_col=wide.max_column,
                               min_row=row_idx, max_row=row_idx)
            s = Series(values, title=kw)
            bind_string_categories(s, cats)
            s.graphicalProperties.line.solidFill = trend_colors[kw]
            line.series.append(s)
        ws.add_chart(line, f"A{chart_row}")

    # 图2：24 个月 ABA Rank 趋势（数值越小越靠前，纵轴反向）
    long_ws = sheets["04_24月搜索量_长表"]
    rank_line = LineChart()
    rank_line.title = "核心/长尾词 24 个月 ABA Rank 趋势（数值越小排名越靠前）"
    rank_line.height, rank_line.width = 9, 22
    rank_line.y_axis.title = "ABA Rank"
    # Excel y-axis default displays the minimum at the bottom.  Keep the
    # minimum (Rank 1) at the top so a lower ABA rank is visually higher.
    rank_line.y_axis.scaling.orientation = "minMax"
    rank_line.y_axis.crosses = "max"
    rank_line.x_axis.tickLblSkip = 2
    rank_line.display_blanks = "gap"
    rank_line.legend.position = "r"
    for kw in trend_order:
        start_row, end_row = rank_row_ranges[kw]
        has_rank = any(p.get("aba_rank") is not None for p in trends["keywords"][kw]["series"])
        if not has_rank:
            continue
        categories = Reference(long_ws, min_col=5, min_row=start_row, max_row=end_row)
        values = Reference(long_ws, min_col=7, min_row=start_row, max_row=end_row)
        s = Series(values, title=kw)
        bind_string_categories(s, categories)
        s.graphicalProperties.line.solidFill = trend_colors[kw]
        rank_line.series.append(s)
    if rank_line.series:
        ws.add_chart(rank_line, f"L{chart_row}")

    # 图3：需求分类搜索量中位数柱状（引用 02）
    dem = sheets["02_需求分类汇总"]
    if dem.max_row >= 2:
        bar = BarChart()
        bar.title = "需求分类 · 搜索量中位数"
        # This is a single-metric category chart; a default "Series 1" legend
        # adds noise and makes the dashboard look like an unbound chart.
        bar.legend = None
        bar.height, bar.width = 9, 22
        data = Reference(dem, min_col=4, min_row=1, max_row=dem.max_row)
        cats = Reference(dem, min_col=1, min_row=2, max_row=dem.max_row)
        bar.add_data(data, titles_from_data=True)
        bar.set_categories(cats)
        ws.add_chart(bar, f"A{chart_row + 19}")

    # 图4：点击—转化集中度散点（总表流量性质分组 + 中位十字线 + 动态局部轴）
    quad_ws = sheets["06_集中度与机会矩阵"]
    if series_refs:
        sc = ScatterChart()
        sc.scatterStyle = "marker"
        sc.title = "点击—转化集中度象限（局部放大）" + ("（转化数据稀疏）" if conv_sparse else "")
        sc.height, sc.width = 9, 22
        sc.x_axis.title = "点击集中度"
        sc.y_axis.title = "转化集中度"
        sc.x_axis.scaling.min, sc.x_axis.scaling.max = x_axis_min, x_axis_max
        sc.y_axis.scaling.min, sc.y_axis.scaling.max = y_axis_min, y_axis_max
        sc.x_axis.numFmt = "0%"
        sc.y_axis.numFmt = "0%"
        sc.visible_cells_only = False
        colors = {"核心/精准词": "C00000", "人群长尾": "4472C4", "其他长尾": "ED7D31"}
        for gname, c0, cnt in series_refs:
            xs = Reference(quad_ws, min_col=c0, min_row=2, max_row=1 + cnt)
            ys = Reference(quad_ws, min_col=c0 + 1, min_row=2, max_row=1 + cnt)
            s = Series(ys, xs, title=gname)
            s.marker = Marker(symbol="circle", size=7)
            s.graphicalProperties.line.noFill = True
            s.marker.graphicalProperties.solidFill = colors.get(gname, "7F7F7F")
            sc.series.append(s)
        if cross_refs:
            for ref_col, title in ((cross_refs[0], "点击中位线"), (cross_refs[1], "转化中位线")):
                xs = Reference(quad_ws, min_col=ref_col, min_row=2, max_row=1 + cross_refs[2])
                ys = Reference(quad_ws, min_col=ref_col + 1, min_row=2, max_row=1 + cross_refs[2])
                s = Series(ys, xs, title=title)
                s.marker = Marker(symbol="dash", size=4)
                s.marker.graphicalProperties.solidFill = "1F4E79"
                s.graphicalProperties.line.noFill = True
                sc.series.append(s)
        ws.add_chart(sc, f"L{chart_row + 19}")
    else:
        ws[f"L{chart_row + 19}"] = "集中度象限图未生成：没有同时具备点击集中度与转化集中度的有效关键词。"
        ws[f"L{chart_row + 19}"].fill = FILL_WARN
        ws[f"L{chart_row + 19}"].alignment = Alignment(wrap_text=True, vertical="top")

    out_dir = os.path.join(wd, "output")
    os.makedirs(out_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    fname = f"{marketplace}_{sanitize_filename(category_label)}_{mode_file}_{date_str}.xlsx"
    out_path = os.path.join(out_dir, fname)
    wb.save(out_path)
    print(f"OK 工作簿已生成: {out_path}")
    print(f"调研口径: {mode_cn} | 品类标签: {category_label}")
    print("下一步: python validate_report.py --workdir <workdir>")


if __name__ == "__main__":
    main()
