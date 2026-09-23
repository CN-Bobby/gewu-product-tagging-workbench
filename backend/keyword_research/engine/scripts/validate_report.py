#!/usr/bin/env python3
"""validate_report.py — 交付前验收（对应 references/guardrails.md §5 中可机械检查的项）

检查：13 个 Sheet 齐全有序（options.matrix_enabled 为假时为 11 个，不含 08/09，顺序不变）；
数据表无合并单元格；筛选与冻结窗格；原始关键词保留；缺失值未填 0（与 cache 比对抽查）；
百分比为数值格式；宽表/长表行数一致。矩阵开关字段缺失时按勾选处理。

用法: python validate_report.py --workdir <任务目录>
退出码: 0=全部 PASS，1=存在 FAIL
"""
import argparse
import glob
import json
import math
import os
import re
import sys

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

EXPECTED_SHEETS = ["00_市场看板", "01_关键词总表", "02_需求分类汇总", "03_24月搜索量_宽表",
                   "04_24月搜索量_长表", "05_趋势与季节性", "06_集中度与机会矩阵",
                   "07_产品方向", "08_ASIN自然广告位矩阵", "09_ASIN流量占比矩阵",
                   "10_市场进入与风险", "11_调研范围与调用", "12_数据质量"]
DATA_SHEETS = ["01_关键词总表", "02_需求分类汇总", "03_24月搜索量_宽表", "04_24月搜索量_长表",
               "05_趋势与季节性", "12_数据质量", "08_ASIN自然广告位矩阵",
               "09_ASIN流量占比矩阵"]
MATRIX_SHEETS = ["08_ASIN自然广告位矩阵", "09_ASIN流量占比矩阵"]

results = []


def expected_sheets(matrix_enabled):
    """未勾选矩阵时 08/09 不产出：校验 11 个且顺序不变；勾选时一个都不放宽。"""
    return [n for n in EXPECTED_SHEETS if matrix_enabled or n not in MATRIX_SHEETS]


def data_sheets(matrix_enabled):
    return [n for n in DATA_SHEETS if matrix_enabled or n not in MATRIX_SHEETS]


def check(name, ok, detail=""):
    results.append((name, ok, detail))


def retry_budget_from_plan(plan_data):
    """Read retry allowance from current or legacy plan layouts.

    Plans created before retry_budget was recorded historically allowed 2 retries.
    """
    budget = plan_data.get("budget") if isinstance(plan_data.get("budget"), dict) else {}
    value = plan_data.get("retry_budget", budget.get("retry_budget", 2))
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 2


def chart_title(chart):
    try:
        paragraphs = chart.title.tx.rich.p
        return "".join(run.t or "" for p in paragraphs for run in (p.r or []))
    except (AttributeError, TypeError):
        return ""


def series_title(series):
    tx = getattr(series, "tx", None)
    if tx is None:
        return None
    if getattr(tx, "v", None) is not None:
        return tx.v
    return None


def series_color(series):
    try:
        return series.graphicalProperties.line.solidFill.srgbClr
    except AttributeError:
        return None


def series_formula(series):
    try:
        return series.val.numRef.f or ""
    except AttributeError:
        return ""


def series_category_formula(series):
    cat = getattr(series, "cat", None)
    for attr in ("strRef", "numRef"):
        try:
            return getattr(cat, attr).f or ""
        except AttributeError:
            continue
    return ""


def consecutive_year_months(months):
    if len(months) != 24 or not all(re.fullmatch(r"\d{4}-\d{2}", str(m)) for m in months):
        return False
    values = []
    for month in months:
        year, number = map(int, month.split("-"))
        if number < 1 or number > 12:
            return False
        values.append(year * 12 + number)
    return all(right - left == 1 for left, right in zip(values, values[1:]))


def scatter_series_formula(series):
    """Return x/y formulas for an XY series without depending on chart internals."""
    refs = []
    for attr in ("xVal", "yVal"):
        node = getattr(series, attr, None)
        try:
            refs.append(node.numRef.f or "")
        except AttributeError:
            refs.append("")
    return " | ".join(refs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--file", default=None, help="不指定时取 output/ 下最新 xlsx")
    args = ap.parse_args()

    if args.file:
        report = args.file
    else:
        files = glob.glob(os.path.join(args.workdir, "output", "*.xlsx"))
        if not files:
            raise SystemExit("output/ 下没有 xlsx 文件")
        report = max(files, key=os.path.getmtime)

    wb = load_workbook(report)
    check("报告文件存在", True, report)

    # 0. 矩阵开关：与产出侧同一信号（input.json options.matrix_enabled，缺失=勾选）
    input_path = os.path.join(args.workdir, "input.json")
    input_cfg = {}
    if os.path.exists(input_path):
        with open(input_path, "r", encoding="utf-8") as f:
            input_cfg = json.load(f)
    matrix_enabled = bool((input_cfg.get("options") or {}).get("matrix_enabled", True))
    sheets_expected = expected_sheets(matrix_enabled)
    sheets_checked = data_sheets(matrix_enabled)

    # 1. Sheet 齐全与顺序
    check(f"{len(sheets_expected)}个Sheet齐全且顺序正确", wb.sheetnames == sheets_expected,
          f"实际: {wb.sheetnames}")

    # 2. 数据表无合并单元格
    for name in sheets_checked:
        if name in wb.sheetnames:
            ws = wb[name]
            check(f"[{name}] 无合并单元格", len(ws.merged_cells.ranges) == 0,
                  f"发现 {len(ws.merged_cells.ranges)} 处合并")

    # 3. 筛选与冻结
    for name in sheets_checked:
        if name in wb.sheetnames:
            ws = wb[name]
            check(f"[{name}] 启用自动筛选", bool(ws.auto_filter.ref), f"ref={ws.auto_filter.ref}")
            check(f"[{name}] 冻结窗格", ws.freeze_panes is not None, f"freeze={ws.freeze_panes}")

    # 4. 原始关键词保留（与 keyword_labeled.json 比对）
    labeled_path = os.path.join(args.workdir, "cache", "keyword_labeled.json")
    labeled = {"keywords": []}
    if os.path.exists(labeled_path):
        with open(labeled_path, "r", encoding="utf-8") as f:
            labeled = json.load(f)
        src_kws = {r["keyword"] for r in labeled["keywords"]}
        ws = wb["01_关键词总表"]
        sheet_kws = {ws.cell(row=i, column=2).value for i in range(2, ws.max_row + 1)}
        sheet_kws.discard(None)
        check("原始关键词全部保留且未被替换", src_kws == sheet_kws,
              f"源 {len(src_kws)} 个 / 表内 {len(sheet_kws)} 个；缺失: {list(src_kws - sheet_kws)[:5]}")

        # 5. 缺失值未填 0：抽查月搜索量列（第12列）
        zero_filled = []
        kw_searches = {r["keyword"]: r.get("searches") for r in labeled["keywords"]}
        for i in range(2, ws.max_row + 1):
            kw = ws.cell(row=i, column=2).value
            v = ws.cell(row=i, column=12).value
            if kw in kw_searches and kw_searches[kw] is None and v == 0:
                zero_filled.append(kw)
        check("缺失值未填 0（抽查月搜索量列）", not zero_filled,
              f"疑似填0: {zero_filled[:5]}")
    else:
        check("keyword_labeled.json 存在", False, "无法执行关键词保留与填0检查")

    # 6. 百分比列为数值格式（01 表购买率列=14）
    ws = wb["01_关键词总表"]
    bad_pct = []
    for i in range(2, min(ws.max_row, 51) + 1):
        cell = ws.cell(row=i, column=14)
        if cell.value not in (None, "") and not isinstance(cell.value, (int, float)):
            bad_pct.append((i, cell.value))
    check("购买率列为数值（非文本百分比）", not bad_pct, f"前50行异常: {bad_pct[:3]}")

    # 7. 标签、流量性质及理由
    CN_TRAFFIC = {"核心大词", "精准品类词", "高意图长尾", "人群长尾", "属性长尾", "场景长尾",
                  "泛流量词", "品牌词", "错类词"}
    n_rows = ws.max_row - 1
    empty_tags, bad_traffic, empty_reasons, bad_intents = [], [], [], []
    CN_INTENT = {"高", "中", "低"}
    for i in range(2, ws.max_row + 1):
        tag_v = ws.cell(row=i, column=6).value
        tra_v = ws.cell(row=i, column=7).value
        reason_v = ws.cell(row=i, column=8).value
        intent_v = ws.cell(row=i, column=11).value
        if tag_v in (None, ""):
            empty_tags.append(i)
        if tra_v not in (None, "") and tra_v not in CN_TRAFFIC:
            bad_traffic.append((i, tra_v))
        if reason_v in (None, ""):
            empty_reasons.append(i)
        if intent_v not in (None, "") and intent_v not in CN_INTENT:
            bad_intents.append((i, intent_v))
    check("需求标签列非空（≥90%）", n_rows == 0 or len(empty_tags) / max(n_rows, 1) <= 0.10,
          f"空标签 {len(empty_tags)}/{n_rows} 行（标签步骤可能被跳过）")
    check("流量性质列已全部中文化", not bad_traffic, f"非中文枚举: {bad_traffic[:3]}")
    check("流量性质理由逐词可审计", not empty_reasons, f"空理由行: {empty_reasons[:5]}")
    check("购买意图统一为中文枚举", not bad_intents, f"异常: {bad_intents[:3]}")

    # 8. 图表白名单与折线系列绑定
    dash = wb["00_市场看板"]
    analysis_path = os.path.join(args.workdir, "cache", "analysis.json")
    has_scatter_data = True
    scatter_point_items = []
    if os.path.exists(analysis_path):
        with open(analysis_path, "r", encoding="utf-8") as f:
            analysis_cache = json.load(f)
        analysis_mode = analysis_cache.get("analysis_mode", "legacy_single")
        sections = analysis_cache.get("analysis_sections") or {}
        check("分析模式与三段来源元数据完整",
              analysis_mode in {"model_split", "hybrid_fallback", "deterministic_fallback", "legacy_single"}
              and (analysis_mode == "legacy_single" or set(sections) == {"market", "product", "decision"}),
              f"mode={analysis_mode} / sections={sorted(sections)}")
        degraded = analysis_mode in {"hybrid_fallback", "deterministic_fallback"}
        safeguards_ok = (not degraded) or (
            (analysis_cache.get("entry_conclusion") or {}).get("grade") == "VALIDATE"
            and (analysis_mode != "deterministic_fallback" or not (analysis_cache.get("product_hypotheses") or []))
        )
        check("降级分析强制保守进入结论", safeguards_ok,
              f"mode={analysis_mode} / grade={(analysis_cache.get('entry_conclusion') or {}).get('grade')}")
        quality_text = " ".join(str(cell.value or "") for row in wb["12_数据质量"].iter_rows() for cell in row)
        check("降级分析在数据质量页醒目标记", (not degraded) or "分析降级" in quality_text,
              f"mode={analysis_mode}")
        traffic_by_kw = {r.get("keyword"): r.get("traffic_type") for r in (labeled.get("keywords") or [])}
        allowed_types = {"core_head", "precise_category", "audience_longtail",
                         "attribute_longtail", "scene_longtail", "high_intent_longtail"}
        scatter_point_items = [item for item in (analysis_cache.get("quadrant") or {}).get("items") or []
                               if isinstance(item.get("click"), (int, float))
                               and isinstance(item.get("conv"), (int, float))
                               and traffic_by_kw.get(item.get("keyword")) in allowed_types]
        has_scatter_data = bool(scatter_point_items)
    dash_types = sorted(type(c).__name__ for c in dash._charts)
    expected_chart_types = (["BarChart", "LineChart", "LineChart", "ScatterChart"]
                            if has_scatter_data else ["BarChart", "LineChart", "LineChart"])
    check("看板图表数量与类型符合有效数据口径",
          dash_types == expected_chart_types,
          f"实际: {dash_types}（可能被手工追加过图表，违反一次性生成禁令）")
    if not has_scatter_data:
        dashboard_text = " ".join(str(c.value or "") for row in dash.iter_rows() for c in row)
        check("散点数据不足时看板显示明确提示", "集中度象限图未生成" in dashboard_text,
              "未发现缺数提示")
    other_charts = [n for n in wb.sheetnames if n != "00_市场看板" and wb[n]._charts]
    check("数据 Sheet 无图表", not other_charts, f"发现图表: {other_charts}")

    scatter_charts = [c for c in dash._charts if type(c).__name__ == "ScatterChart"]
    if scatter_charts:
        scatter = scatter_charts[0]
        scatter_titles = [series_title(s) for s in scatter.series]
        scatter_formulas = [scatter_series_formula(s) for s in scatter.series]
        check("散点图允许读取隐藏辅助数据", scatter.visible_cells_only is False,
              f"visible_cells_only={scatter.visible_cells_only}")
        check("散点图中位线拆为两个独立系列",
              "点击中位线" in scatter_titles and "转化中位线" in scatter_titles,
              f"系列={scatter_titles}")
        marker_series = [s for s in scatter.series
                         if series_title(s) not in {"点击中位线", "转化中位线"}]
        marker_titles = [series_title(s) for s in marker_series]
        check("散点系列只使用总表流量性质分组",
              set(marker_titles).issubset({"核心/精准词", "人群长尾", "其他长尾"})
              and not ({"核心词", "长尾词", "其他词"} & set(marker_titles)),
              f"系列={marker_titles}")
        check("关键词系列只显示散点不连线",
              scatter.scatterStyle == "marker"
              and all(s.graphicalProperties.line.noFill is True for s in marker_series),
              f"scatterStyle={scatter.scatterStyle}; "
              f"noFill={[s.graphicalProperties.line.noFill for s in marker_series]}")
        check("散点图全部引用06集中度辅助区",
              all("06_集中度与机会矩阵" in formula for formula in scatter_formulas),
              f"公式={scatter_formulas[:3]}")
        xmin, xmax = scatter.x_axis.scaling.min, scatter.x_axis.scaling.max
        ymin, ymax = scatter.y_axis.scaling.min, scatter.y_axis.scaling.max
        xvals = [i["click"] for i in scatter_point_items]
        yvals = [i["conv"] for i in scatter_point_items]
        def step_aligned(v):
            return v is not None and abs(v * 20 - round(v * 20)) < 1e-8
        bounds_ok = (xvals and yvals and 0 <= xmin <= min(xvals) <= max(xvals) <= xmax <= 1
                     and 0 <= ymin <= min(yvals) <= max(yvals) <= ymax <= 1
                     and xmax - xmin >= 0.15 - 1e-8 and ymax - ymin >= 0.15 - 1e-8
                     and all(step_aligned(v) for v in (xmin, xmax, ymin, ymax)))
        padding_ok = ((xmin == 0 or xmin <= min(xvals) - 0.03 + 1e-8)
                      and (xmax == 1 or xmax >= max(xvals) + 0.03 - 1e-8)
                      and (ymin == 0 or ymin <= min(yvals) - 0.03 + 1e-8)
                      and (ymax == 1 or ymax >= max(yvals) + 0.03 - 1e-8)) if xvals and yvals else False
        check("散点图动态轴覆盖点位、按5个百分点取整且跨度至少15个百分点",
              bounds_ok and padding_ok,
              f"x={xmin}..{xmax}; y={ymin}..{ymax}")
        check("散点图坐标轴按百分比显示",
              getattr(scatter.x_axis.numFmt, "formatCode", scatter.x_axis.numFmt) == "0%"
              and getattr(scatter.y_axis.numFmt, "formatCode", scatter.y_axis.numFmt) == "0%",
              f"x={scatter.x_axis.numFmt}; y={scatter.y_axis.numFmt}")

    # 9. ASIN矩阵结构（未勾选矩阵时 08/09 不产出，结构与缓存口径一并跳过）
    if os.path.exists(input_path):
        if matrix_enabled:
            direct_asins = []
            for group in input_cfg.get("groups") or []:
                for asin in group.get("asins") or []:
                    asin = str(asin).upper()
                    if asin not in direct_asins:
                        direct_asins.append(asin)
            rank_ws = wb["08_ASIN自然广告位矩阵"]
            traffic_ws = wb["09_ASIN流量占比矩阵"]
            expected_rank_header = ["快照月份", "原始关键词", "中文翻译"] + [
                f"{asin}|自然位/广告位" for asin in direct_asins]
            expected_traffic_header = ["快照月份", "原始关键词", "中文翻译"] + [
                f"{asin}|流量占比" for asin in direct_asins]
            actual_rank_header = [rank_ws.cell(1, c).value for c in range(1, rank_ws.max_column + 1)]
            actual_traffic_header = [traffic_ws.cell(1, c).value for c in range(1, traffic_ws.max_column + 1)]
            check("自然/广告位矩阵列结构正确", actual_rank_header == expected_rank_header,
                  f"实际前列: {actual_rank_header[:6]}")
            check("流量占比矩阵列结构正确", actual_traffic_header == expected_traffic_header,
                  f"实际前列: {actual_traffic_header[:6]}")
            if os.path.exists(labeled_path):
                labeled_kws = [r["keyword"] for r in labeled["keywords"]]
                rank_kws = [rank_ws.cell(i, 2).value for i in range(2, rank_ws.max_row + 1)]
                traffic_kws = [traffic_ws.cell(i, 2).value for i in range(2, traffic_ws.max_row + 1)]
                check("两个ASIN矩阵关键词顺序与总表一致",
                      rank_kws == labeled_kws and traffic_kws == labeled_kws,
                      f"排名 {len(rank_kws)} / 占比 {len(traffic_kws)} / 总表 {len(labeled_kws)}")
            bad_rank_values = []
            rank_pattern = re.compile(r"^(?:P\d+-\d+)?/(?:P\d+-\d+)?$")
            for col in range(4, rank_ws.max_column + 1):
                for row in range(2, rank_ws.max_row + 1):
                    value = rank_ws.cell(row, col).value
                    if value in (None, ""):
                        continue
                    if not isinstance(value, str) or not rank_pattern.fullmatch(value):
                        bad_rank_values.append((row, col, value))
            check("自然/广告位矩阵使用P页-页内位/P页-页内位格式", not bad_rank_values,
                  f"异常: {bad_rank_values[:3]}")
            bad_traffic_values = []
            for col in range(4, traffic_ws.max_column + 1):
                for row in range(2, traffic_ws.max_row + 1):
                    value = traffic_ws.cell(row, col).value
                    if value not in (None, "") and not isinstance(value, (int, float)):
                        bad_traffic_values.append((row, col, value))
            check("流量占比矩阵保持数值百分比", not bad_traffic_values,
                  f"异常: {bad_traffic_values[:3]}")

            matrix_path = os.path.join(args.workdir, "cache", "asin_keyword_matrix.json")
            if os.path.exists(matrix_path):
                with open(matrix_path, "r", encoding="utf-8") as f:
                    matrix_cache = json.load(f)
                check("ASIN矩阵缓存口径（当前前100或指定月份前100）",
                      matrix_cache.get("page_limit") == 1 and (
                          (matrix_cache.get("snapshot_type") == "current"
                           and matrix_cache.get("scope") == "current_top100")
                          or (matrix_cache.get("snapshot_type") == "month"
                              and re.fullmatch(r"month_\d{6}_top100",
                                               str(matrix_cache.get("scope") or "")) is not None)
                      ),
                      f"实际: snapshot={matrix_cache.get('snapshot_type')} scope={matrix_cache.get('scope')} page_limit={matrix_cache.get('page_limit')}")

    # 10. 文件名格式。Skill 命令行保留旧格式；桌面应用使用带快照、生成时间和引擎版本的正式格式。
    fname = os.path.basename(report)
    legacy_name = re.fullmatch(r"[A-Z]{2}_.+_.+_\d{8}\.xlsx", fname)
    desktop_name = re.fullmatch(
        r"[A-Z]{2}_.{1,24}_\d+ASIN_快照\d{6}_生成\d{8}-\d{4}_引擎v\d+\.\d+\.\d+(?:-\d{2})?\.xlsx",
        fname,
    )
    check("文件名符合固定格式", bool(legacy_name or desktop_name),
          f"实际: {fname}")

    # 11. 宽表/长表与折线图一致性
    trend_path = os.path.join(args.workdir, "cache", "trend_analysis.json")
    if os.path.exists(trend_path):
        with open(trend_path, "r", encoding="utf-8") as f:
            trends = json.load(f)
        n_kw = len(trends["keywords"])
        n_months = len(trends["months"])
        wide_rows = wb["03_24月搜索量_宽表"].max_row - 1
        long_rows = wb["04_24月搜索量_长表"].max_row - 1
        check("宽表行数=趋势词数", wide_rows == n_kw, f"{wide_rows} vs {n_kw}")
        check("长表行数=趋势词数×月数", long_rows == n_kw * n_months,
              f"{long_rows} vs {n_kw}×{n_months}")
        check("趋势月份为连续24个月且使用yyyy-MM",
              consecutive_year_months(trends["months"]),
              f"实际: {trends['months']}")
        line_charts = [c for c in dash._charts if type(c).__name__ == "LineChart"]
        search_chart = next((c for c in line_charts if "搜索量趋势" in chart_title(c)), None)
        rank_chart = next((c for c in line_charts if "ABA Rank" in chart_title(c)), None)
        expected_search = list(trends["keywords"].keys())
        expected_rank = [kw for kw, rec in trends["keywords"].items()
                         if any(p.get("aba_rank") is not None for p in rec.get("series", []))]
        check("搜索量折线图存在且系列名为真实关键词",
              search_chart is not None and [series_title(s) for s in search_chart.series] == expected_search,
              f"实际: {[] if search_chart is None else [series_title(s) for s in search_chart.series]}")
        check("ABA Rank折线图存在且系列名为真实关键词",
              rank_chart is not None and [series_title(s) for s in rank_chart.series] == expected_rank,
              f"实际: {[] if rank_chart is None else [series_title(s) for s in rank_chart.series]}")
        if search_chart is not None:
            check("搜索量折线图引用03宽表搜索量行",
                  all("03_24月搜索量_宽表" in series_formula(s) for s in search_chart.series),
                  f"实际: {[series_formula(s) for s in search_chart.series[:2]]}")
            last_col = get_column_letter(3 + n_months)
            expected_cat = f"'03_24月搜索量_宽表'!$D$1:${last_col}$1"
            actual_cats = [series_category_formula(s) for s in search_chart.series]
            check("搜索量折线图分类轴绑定03宽表24个月且隔月显示",
                  actual_cats == [expected_cat] * len(expected_search)
                  and search_chart.x_axis.tickLblSkip == 2,
                  f"分类={actual_cats[:2]} / 标签间隔={search_chart.x_axis.tickLblSkip}")
        if rank_chart is not None:
            check("ABA Rank折线图引用04长表ABA rank列",
                  all("04_24月搜索量_长表" in series_formula(s) and "$G$" in series_formula(s)
                      for s in rank_chart.series),
                  f"实际: {[series_formula(s) for s in rank_chart.series[:2]]}")
            expected_rank_cats = []
            for kw in expected_rank:
                block = expected_search.index(kw)
                start_row = 2 + block * n_months
                end_row = start_row + n_months - 1
                expected_rank_cats.append(
                    f"'04_24月搜索量_长表'!$E${start_row}:$E${end_row}"
                )
            actual_rank_cats = [series_category_formula(s) for s in rank_chart.series]
            check("ABA Rank折线图分类轴绑定04长表对应24个月且隔月显示",
                  actual_rank_cats == expected_rank_cats
                  and rank_chart.x_axis.tickLblSkip == 2,
                  f"分类={actual_rank_cats[:2]} / 标签间隔={rank_chart.x_axis.tickLblSkip}")
            check("ABA Rank纵轴已反向（Rank 1在顶部）",
                  rank_chart.y_axis.scaling.orientation == "minMax",
                  f"实际: {rank_chart.y_axis.scaling.orientation}")
        if search_chart is not None and rank_chart is not None:
            search_colors = {series_title(s): series_color(s) for s in search_chart.series}
            rank_colors = {series_title(s): series_color(s) for s in rank_chart.series}
            check("同一关键词在两张折线图中颜色一致",
                  all(search_colors.get(kw) == color for kw, color in rank_colors.items()),
                  f"搜索量={search_colors} / Rank={rank_colors}")

        # 12. 纯ASIN模式调用预算与禁止项
        if os.path.exists(input_path):
            groups = input_cfg.get("groups") or []
            if groups and not any(g.get("node_ids") for g in groups):
                asins = []
                for g in groups:
                    for asin in g.get("asins") or []:
                        asin = str(asin).upper()
                        if asin not in asins:
                            asins.append(asin)
                options = input_cfg.get("options") or {}
                if options.get("keyword_pool_pages") is not None:
                    pool_pages = int(options["keyword_pool_pages"])
                else:
                    legacy_size = int(options.get("keyword_pool_size") or 100)
                    pool_pages = 1 if legacy_size == 50 else 3
                expected_calls = (math.ceil(len(asins) / 20) * pool_pages
                                  + (len(asins) if matrix_enabled else 0)
                                  + n_kw) if asins else n_kw
                plan_path = os.path.join(args.workdir, "plan.json")
                call_log_path = os.path.join(args.workdir, "cache", "call_log.json")
                plan_data, log_data = {}, {"calls": []}
                if os.path.exists(plan_path):
                    with open(plan_path, "r", encoding="utf-8") as f:
                        plan_data = json.load(f)
                if os.path.exists(call_log_path):
                    with open(call_log_path, "r", encoding="utf-8") as f:
                        log_data = json.load(f)
                check("纯ASIN模式计划调用符合固定公式",
                      plan_data.get("planned_total") == expected_calls,
                      f"计划={plan_data.get('planned_total')} / 期望={expected_calls}")
                actual = log_data.get("actual_total", len(log_data.get("calls") or []))
                retry_budget = retry_budget_from_plan(plan_data)
                allowed_total = plan_data.get("planned_total", expected_calls) + retry_budget
                check(f"实际调用不超过基准+{retry_budget}次临时重试",
                      actual <= allowed_total,
                      f"实际={actual} / 硬上限={allowed_total}")
                calls = log_data.get("calls") or []
                tools = [str(c.get("tool") or "") for c in calls]
                scopes = [str(c.get("scope") or "") for c in calls]
                check("标准报告没有Sorftime调用",
                      not any("sorftime" in tool.lower() for tool in tools), f"工具={tools}")
                check("traffic_keyword没有第2页调用",
                      not any(tool.endswith("traffic_keyword") and re.search(r"\b(?:page|p)\s*=?\s*[2-9]", scope, re.I)
                              for tool, scope in zip(tools, scopes)),
                      f"范围={scopes}")
                extend_calls = [c for c in calls
                                if str(c.get("tool") or "").lower().endswith("traffic_extend")
                                and not c.get("is_retry")]
                expected_extend_calls = math.ceil(len(asins) / 20) * pool_pages
                extend_pages = sorted(int(m.group(1)) for c in extend_calls
                                      if (m := re.search(r"\b(?:page|p)\s*=?\s*(\d+)",
                                                        str(c.get("scope") or ""), re.I)))
                check("traffic_extend按关键词池大小固定页数调用",
                      len(extend_calls) == expected_extend_calls
                      and extend_pages == sorted(list(range(1, pool_pages + 1)) * math.ceil(len(asins) / 20)),
                      f"基础调用={len(extend_calls)} / 页码={extend_pages} / 期望={expected_extend_calls}")
                check("traffic_extend绝不请求第4页",
                      not any(page >= 4 for page in extend_pages), f"页码={extend_pages}")
                traffic_calls = [c for c in calls
                                 if str(c.get("tool") or "").lower().endswith("traffic_keyword")]
                base_traffic_calls = [c for c in traffic_calls if not c.get("is_retry")]
                per_asin_counts = {
                    asin: sum(1 for c in base_traffic_calls
                              if re.search(rf"(?<![A-Z0-9]){re.escape(asin)}(?![A-Z0-9])",
                                           str(c.get("scope") or ""), re.I))
                    for asin in asins
                }
                if matrix_enabled:
                    check("每个直接输入ASIN恰好一次traffic_keyword基础调用",
                          len(base_traffic_calls) == len(asins)
                          and all(count == 1 for count in per_asin_counts.values()),
                          f"基础调用={len(base_traffic_calls)} / ASIN计数={per_asin_counts}")
                else:
                    check("未勾选矩阵时没有任何traffic_keyword调用",
                          not traffic_calls,
                          f"traffic_keyword调用={len(traffic_calls)}（本次未勾选矩阵，应为 0）")
                options_cfg = input_cfg.get("options") or {}
                task_month = options_cfg.get("matrix_month")
                invalid_current_scopes = []
                for c in traffic_calls:
                    scope = str(c.get("scope") or "")
                    if not re.search(r"\b(?:page|p)\s*=?\s*1(?:\D|$)", scope, re.I):
                        invalid_current_scopes.append(scope)
                        continue
                    has_current = "current" in scope.lower()
                    has_month = re.search(r"(?:month|historydate)\s*=", scope, re.I)
                    if task_month:
                        # 任务指定快照月份：traffic_keyword 允许携带该月份
                        if has_month and not re.search(
                                rf"month\s*=\s*{re.escape(str(task_month))}", scope, re.I):
                            invalid_current_scopes.append(scope)
                    else:
                        # 未指定月份：必须 current 且不携带历史月份
                        if (not has_current) or has_month:
                            invalid_current_scopes.append(scope)
                check("traffic_keyword固定 page=1（月份按任务快照口径）",
                      not invalid_current_scopes,
                      f"异常范围={invalid_current_scopes}")

    # 13. 产品方向必须是可追溯的假设，不能写成已证实功能结论
    product_ws = wb["07_产品方向"]
    expected_product_header = ["需求板块", "证据关键词", "数据证据", "用户可能问题（推断）",
                               "产品形态", "材料", "功能", "规格", "套装/配件",
                               "产品响应假设", "置信度", "需要验证的数据"]
    actual_product_header = [product_ws.cell(1, c).value for c in range(1, 13)]
    check("产品方向列结构为严格证据模式", actual_product_header == expected_product_header,
          f"实际={actual_product_header}")
    bad_product_rows = []
    for row in range(2, product_ws.max_row + 1):
        values = [product_ws.cell(row, c).value for c in range(1, 13)]
        if not any(v not in (None, "") for v in values):
            continue
        source_kw, evidence, response, confidence, validation = values[1], values[2], values[9], values[10], values[11]
        if (source_kw in (None, "") or evidence in (None, "")
                or "假设" not in str(response or "")
                or confidence not in {"高", "中", "低"}
                or validation in (None, "")):
            bad_product_rows.append((row, source_kw, response, confidence, validation))
    check("产品方向逐行含证据、假设、置信度和待验证项", not bad_product_rows,
          f"异常={bad_product_rows[:3]}")

    # 汇总
    fails = [r for r in results if not r[1]]
    print(f"\n=== 验收结果: {len(results) - len(fails)} PASS / {len(fails)} FAIL ===")
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f" — {detail}" if not ok and detail else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
