# 寻龙诀 v2.4.2 / 2026-06-10
"""
trend_judgment.py — 趋势判定核心模块
=====================================

对亚马逊类目的 24 个月销量 + 均价数据进行时间序列分解（STL），
科学判定是否具备"整体向上"的选品潜力。

使用方式（作为模块导入）：
    from trend_judgment import judge_category_trend
    result = judge_category_trend(sales_list, price_list)

单独运行（Mock 数据演示）：
    python trend_judgment.py

依赖：
    pip install pandas numpy statsmodels
"""

import warnings
warnings.filterwarnings('ignore')

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Union
from .site_profiles import normalize_site

# ── 从统一配置读取参数（优先） ──
MIN_AVG_SALES = 10000.0
SALES_DECLINE_TOLERANCE = 0.05
PRICE_DECLINE_TOLERANCE = 0.10
STL_PERIOD = 12
MIN_R_SQUARED = 0.15
SEASONALITY_THRESHOLD = 0.15


# ═══════════════════════════════════════════════════════════════════
# 公共函数
# ═══════════════════════════════════════════════════════════════════

def judge_category_trend(
    monthly_sales: List[float],
    monthly_prices: List[float],
    node_id: str = "",
    site: str = None,
    period: int = STL_PERIOD,
    min_avg_sales: float = MIN_AVG_SALES,
    sales_decline_tolerance: float = SALES_DECLINE_TOLERANCE,
    price_decline_tolerance: float = PRICE_DECLINE_TOLERANCE,
    min_r_squared: float = MIN_R_SQUARED,
) -> Dict:
    """
    对 24 个月销量和均价数据执行完整的趋势判定流程。

    Parameters
    ----------
    monthly_sales : List[float]
        24 个月的月销量数据（顺序从旧到新）。
    monthly_prices : List[float]
        24 个月的月均价数据（顺序从旧到新）。
    node_id : str, optional
        类目 ID，用于输出标识。
    site : str, optional
        站点，默认 "US"。
    period : int, optional
        季节性周期，默认 12（月度数据自然年周期）。
    min_avg_sales : float, optional
        兼容参数。体量分级与停止下钻由 v4 状态机按近6月均销处理，
        本函数只负责趋势判定，不因体量直接淘汰。
    sales_decline_tolerance : float, optional
        销量斜率下滑容忍度（年化百分比），默认 0.05（即 5%）。
    price_decline_tolerance : float, optional
        均价斜率下滑容忍度（年化百分比），默认 0.10（即 10%）。
    min_r_squared : float, optional
        趋势拟合优度最低门槛，默认 0.3。销量和价格的 R² 均低于此值时
        认为趋势不可靠，直接淘汰。

    Returns
    -------
    Dict
        {
            "status": "passed" | "eliminated" | "error",
            "reason": str,
            "is_valid_node": bool,
            "direction": "双升" | "销量升_价格微降" | "销量微降_价格升" | "淘汰" | "错误",
            "sales_slope_annual_pct": float,   # 销量年化趋势斜率(%)
            "price_slope_annual_pct": float,    # 均价年化趋势斜率(%)
            "avg_monthly_sales": float,
            "avg_monthly_price": float,
            "sales_r2": float,                  # 销量趋势拟合优度
            "price_r2": float,                  # 均价趋势拟合优度
            "human_summary": str,
        }
    """
    # ─────────────────────────────────────────────
    # 1. 数据预处理与合法性校验
    # ─────────────────────────────────────────────
    site = normalize_site(site)
    sales_arr: np.ndarray = np.array(monthly_sales, dtype=float)
    price_arr: np.ndarray = np.array(monthly_prices, dtype=float)

    if len(sales_arr) < 2 * period:
        return _error_result(
            node_id, site,
            f"数据不足：仅 {len(sales_arr)} 个月，需要至少 {2 * period} 个月"
        )

    if len(sales_arr) != len(price_arr):
        return _error_result(node_id, site, "销量与价格数据长度不一致")

    # 插值填补缺失值
    sales_series: np.ndarray = _fill_missing(sales_arr)
    price_series: np.ndarray = _fill_missing(price_arr)

    avg_sales: float = float(np.mean(sales_series))
    avg_price: float = float(np.mean(price_series))

    # ─────────────────────────────────────────────
    # 2. 时间序列分解（STL）提取趋势分量
    # ─────────────────────────────────────────────
    try:
        from statsmodels.tsa.seasonal import STL

        sales_stl = STL(sales_series, period=period, robust=True).fit()
        sales_trend: np.ndarray = sales_stl.trend.values

        price_stl = STL(price_series, period=period, robust=True).fit()
        price_trend: np.ndarray = price_stl.trend.values

    except Exception:
        # 退化策略：STL 失败时直接用原始数据
        sales_trend = sales_series.copy()
        price_trend = price_series.copy()

    # ─────────────────────────────────────────────
    # 3. 对趋势分量进行线性回归，计算斜率
    # ─────────────────────────────────────────────
    x: np.ndarray = np.arange(len(sales_trend))  # 0, 1, 2, ..., 23

    # 剔除趋势分量中的 NaN
    valid_sales: np.ndarray = ~np.isnan(sales_trend)
    valid_price: np.ndarray = ~np.isnan(price_trend)

    if valid_sales.sum() < 3 or valid_price.sum() < 3:
        return _error_result(node_id, site, "趋势分量数据点过少，无法计算有效斜率")

    # 销量斜率（线性回归）
    coeffs_sales: np.ndarray = np.polyfit(x[valid_sales], sales_trend[valid_sales], 1)
    sales_slope: float = coeffs_sales[0]  # 每月变化量

    # 均价斜率（线性回归）
    coeffs_price: np.ndarray = np.polyfit(x[valid_price], price_trend[valid_price], 1)
    price_slope: float = coeffs_price[0]  # 每月变化量

    # ── R² 计算（销量趋势拟合优度）──
    sales_trend_valid: np.ndarray = sales_trend[valid_sales]
    sales_pred: np.ndarray = np.polyval(coeffs_sales, x[valid_sales])
    ss_res: float = np.sum((sales_trend_valid - sales_pred) ** 2)
    ss_tot: float = np.sum((sales_trend_valid - np.mean(sales_trend_valid)) ** 2)
    sales_r2: float = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # ── R² 计算（均价趋势拟合优度）──
    price_trend_valid: np.ndarray = price_trend[valid_price]
    price_pred: np.ndarray = np.polyval(coeffs_price, x[valid_price])
    ss_res_p: float = np.sum((price_trend_valid - price_pred) ** 2)
    ss_tot_p: float = np.sum((price_trend_valid - np.mean(price_trend_valid)) ** 2)
    price_r2: float = 1 - ss_res_p / ss_tot_p if ss_tot_p > 0 else 0.0

    # ─────────────────────────────────────────────
    # 4. 年化斜率百分比（便于人类理解）
    #    公式：年化斜率(%) = 月斜率 × 12 / 均值 × 100
    #    含义：每年相对于均值的增长/下降百分比
    # ─────────────────────────────────────────────
    sales_slope_annual_pct: float = (
        (sales_slope * 12 / avg_sales) * 100 if avg_sales > 0 else 0.0
    )
    price_slope_annual_pct: float = (
        (price_slope * 12 / avg_price) * 100 if avg_price > 0 else 0.0
    )

    # ─────────────────────────────────────────────
    # 5. 逻辑判定（三重通过规则）
    # ─────────────────────────────────────────────
    sales_up: bool = sales_slope > 0
    price_up: bool = price_slope > 0

    # 容忍度阈值：年化百分比的下界
    # 销量容忍边界: -5.0%（销量微降 ≤5%/年可接受）
    # 均价容忍边界: -10.0%（均价微降 ≤10%/年可接受）
    sales_tolerance_boundary: float = -sales_decline_tolerance * 100
    price_tolerance_boundary: float = -price_decline_tolerance * 100

    sales_not_bad: bool = sales_slope_annual_pct >= sales_tolerance_boundary
    price_not_bad: bool = price_slope_annual_pct >= price_tolerance_boundary

    # ── R² 拟合优度检查 ──
    MIN_R_SALES_CHECK = min_r_squared
    MIN_R_PRICE_CHECK = min_r_squared
    r_sales_ok: bool = bool(sales_r2 >= MIN_R_SALES_CHECK)
    r_price_ok: bool = bool(price_r2 >= MIN_R_PRICE_CHECK)
    r_both_bad: bool = not r_sales_ok and not r_price_ok

    # ──────────────────────────────────────────────────────────
    # 方案C：YoY (年同比增长率) 双信号并联
    # ──────────────────────────────────────────────────────────
    # 在原始数据上计算 YoY 增长率（不是 STL 趋势分量，而是原始销量/均价）
    yoy_sales, yoy_sales_pos = _calc_yoy_growth(sales_series)
    yoy_price, yoy_price_pos = _calc_yoy_growth(price_series)

    # 检测季节强度
    is_seasonal, season_strength = _detect_seasonality(sales_series, period)

    yoy_sales_up: bool = yoy_sales > 0
    yoy_price_up: bool = yoy_price > 0
    yoy_sales_not_bad: bool = float(yoy_sales * 100) >= sales_tolerance_boundary
    yoy_price_not_bad: bool = float(yoy_price * 100) >= price_tolerance_boundary

    # ── 判定路径A：线性回归（现有逻辑，R²门槛已降为0.15）──
    path_a_valid: bool = False
    path_a_dir: str = ""
    path_a_reason: List[str] = []

    if not r_both_bad:
        # 规则① 双升
        if sales_up and price_up:
            path_a_valid = True
            path_a_dir = "双升"
            path_a_reason = [f"线性回归: 销量+均价双升 (R²={sales_r2:.3f}/{price_r2:.3f})"]
        # 规则② 销量升+价格微降
        elif sales_up and price_not_bad and not price_up and r_sales_ok:
            path_a_valid = True
            path_a_dir = "销量升_价格微降"
            path_a_reason = [f"线性回归: 销量升均价微降 (R²={sales_r2:.3f})"]
        # 规则③ 销量微降+价格升
        elif sales_not_bad and price_up and not sales_up and r_price_ok:
            path_a_valid = True
            path_a_dir = "销量微降_价格升"
            path_a_reason = [f"线性回归: 销量微降均价升 (R²={price_r2:.3f})"]

    # ── 判定路径B：年同比增长率（YoY，不受季节性影响）──
    path_b_valid: bool = False
    path_b_dir: str = ""
    path_b_reason: List[str] = []

    # 规则① 双升
    if yoy_sales_up and yoy_price_up:
        path_b_valid = True
        path_b_dir = "双升"
        path_b_reason = [f"YoY: 销量+{yoy_sales*100:.1f}%，均价+{yoy_price*100:.1f}%"]
    # 规则② 销量升+价格微降
    elif yoy_sales_up and yoy_price_not_bad and not yoy_price_up:
        path_b_valid = True
        path_b_dir = "销量升_价格微降"
        path_b_reason = [f"YoY: 销量+{yoy_sales*100:.1f}%，均价{yoy_price*100:.1f}%(可接受)"]
    # 规则③ 销量微降+价格升
    elif yoy_sales_not_bad and yoy_price_up and not yoy_sales_up:
        path_b_valid = True
        path_b_dir = "销量微降_价格升"
        path_b_reason = [f"YoY: 均价+{yoy_price*100:.1f}%，销量{yoy_sales*100:.1f}%(可接受)"]

    # ── 合并判定：任一路径通过即视为有效 ──
    # v2.4.3: 方向增加路径前缀，修复 YoY-only 节点被误标为"回归·"的 bug
    is_valid: bool = path_a_valid or path_b_valid
    raw_dir: str = path_b_dir if path_b_valid else (path_a_dir if path_a_valid else "淘汰")

    if is_valid:
        if path_b_valid and path_a_valid:
            direction = f"双路径·{raw_dir}"
        elif path_b_valid:
            direction = f"YoY·{raw_dir}"
        else:
            direction = f"回归·{raw_dir}"
    else:
        direction = raw_dir
    human_lines: List[str] = []

    if path_b_valid:
        human_lines.extend(path_b_reason)
        if is_seasonal:
            human_lines.append(f"(季节强度{season_strength:.2f}，YoY已消除季节性)")

    if path_a_valid:
        human_lines.extend(path_a_reason)
    elif not path_b_valid:
        # 双路径均未通过 → 给出淘汰原因
        human_lines.append("线性回归与YoY双路径均未通过。")
        if not yoy_sales_up and not yoy_price_up:
            human_lines.append(f"YoY: 销量{yoy_sales*100:.1f}%，均价{yoy_price*100:.1f}%，双降。")
        elif not yoy_sales_up:
            human_lines.append(f"YoY: 销量{yoy_sales*100:.1f}%，超出容忍范围。")
        elif not yoy_price_up:
            human_lines.append(f"YoY: 均价{yoy_price*100:.1f}%，超出容忍范围。")
        if r_both_bad:
            human_lines.append(
                f"线性回归: R² 双低 (销量={sales_r2:.3f}, 均价={price_r2:.3f})"
            )
        else:
            if not sales_up and not price_up:
                human_lines.append("线性回归: 销量与均价均呈下降趋势。")
            elif not sales_up:
                human_lines.append(f"线性回归: 销量下降（年化 {sales_slope_annual_pct:.1f}%）。")
            elif not price_up:
                human_lines.append(f"线性回归: 均价下降（年化 {price_slope_annual_pct:.1f}%）。")

    # ─────────────────────────────────────────────
    # 6. 组装返回字典
    # ─────────────────────────────────────────────
    status: str = "passed" if is_valid else "eliminated"
    reason: str = "" if is_valid else " ".join(human_lines)

    human_summary: str = (
        f"【{node_id}】{'✅' if is_valid else '❌'} {direction}\n"
        f"  ├─ 24 月均销量: {avg_sales:>10,.0f}  | 24 月均价: ${avg_price:>6.2f}\n"
        f"  ├─ YoY 销量增长率: {yoy_sales*100:>+7.2f}%/年 (积极月占比 {yoy_sales_pos*100:.0f}%)\n"
        f"  ├─ YoY 均价增长率: {yoy_price*100:>+7.2f}%/年\n"
        f"  ├─ 季节强度: {season_strength:.3f} {'🌊强' if is_seasonal else '🟢弱'}\n"
        f"  ├─ 线性回归: 销量R²={sales_r2:.3f} 均价R²={price_r2:.3f} | 斜率 {sales_slope_annual_pct:>+.1f}%/{price_slope_annual_pct:>+.1f}%\n"
        f"  └─ 结论: {' '.join(human_lines)}"
    )

    return {
        "status": status,
        "reason": reason,
        "is_valid_node": is_valid,
        "direction": direction,
        "sales_slope_annual_pct": round(sales_slope_annual_pct, 4),
        "price_slope_annual_pct": round(price_slope_annual_pct, 4),
        "avg_monthly_sales": round(avg_sales, 0),
        "avg_monthly_price": round(avg_price, 2),
        "sales_r2": round(float(sales_r2), 4),
        "price_r2": round(float(price_r2), 4),
        # 方案C新增字段
        "yoy_sales_growth": round(float(yoy_sales), 4),
        "yoy_price_growth": round(float(yoy_price), 4),
        "yoy_sales_positive_ratio": round(float(yoy_sales_pos), 4),
        "seasonal_strength": round(float(season_strength), 4),
        "is_seasonal": is_seasonal,
        "path_a_valid": path_a_valid,
        "path_b_valid": path_b_valid,
        "human_summary": human_summary,
    }


# ═══════════════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════════════

def _fill_missing(data: np.ndarray) -> np.ndarray:
    """用线性插值 + 前后填充处理缺失值。"""
    series: pd.Series = (
        pd.Series(data)
        .interpolate(method='linear')
        .bfill()
        .ffill()
    )
    return series.values


# ═══════════════════════════════════════════════════════════════════
# 新增：年同比增长率（YoY）计算 + 季节强度检测（方案C）
# ═══════════════════════════════════════════════════════════════════

def _calc_yoy_growth(data: np.ndarray) -> tuple:
    """
    计算年同比增长率（YoY Growth Rate）。
    将 24 个月的数据分为前后两半，逐月对比（第 i 月 vs 第 i+12 月），
    返回平均增长率 和 增长月份占比。

    这样天然消除了季节性：夏天 vs 去年夏天，冬天 vs 去年冬天。
    """
    n = len(data)
    half = n // 2
    if half < 1:
        return 0.0, 0.0

    growth_rates = []
    for i in range(half):
        older = data[i]
        newer = data[i + half]
        if older > 0:
            growth_rates.append((newer - older) / older)

    if not growth_rates:
        return 0.0, 0.0

    avg_growth = float(np.mean(growth_rates))
    positive_ratio = float(sum(1 for v in growth_rates if v > 0) / len(growth_rates))
    return avg_growth, positive_ratio


def _detect_seasonality(data: np.ndarray, period: int = 12) -> tuple:
    """
    通过 STL 分解检测季节强度。
    返回 (is_strong_seasonal: bool, seasonal_strength: float)。
    seasonal_strength = 季节分量振幅 / 趋势分量均值
    当 strength > SEASONALITY_THRESHOLD (默认0.15) 时判定为强季节性。
    """
    try:
        from statsmodels.tsa.seasonal import STL as _STL

        stl = _STL(data, period=period, robust=True).fit()
        seasonal = stl.seasonal.values
        trend = stl.trend.values

        seasonal_amp = (float(np.max(seasonal)) - float(np.min(seasonal))) / 2.0
        trend_mean = float(np.nanmean(trend))
        if trend_mean == 0:
            return False, 0.0

        strength = seasonal_amp / abs(trend_mean)
        is_strong = bool(strength > SEASONALITY_THRESHOLD)
        return is_strong, round(strength, 4)
    except Exception:
        return False, 0.0


def _error_result(node_id: str, site: str, reason: str) -> Dict:
    """构造错误结果字典。"""
    return {
        "status": "error",
        "reason": reason,
        "is_valid_node": False,
        "direction": "错误",
        "sales_slope_annual_pct": 0.0,
        "price_slope_annual_pct": 0.0,
        "avg_monthly_sales": 0.0,
        "avg_monthly_price": 0.0,
        "sales_r2": 0.0,
        "price_r2": 0.0,
        "human_summary": f"【{node_id}】⚠️ 判定失败: {reason}",
    }


# ═══════════════════════════════════════════════════════════════════
# 在 `02_数据处理与过滤.py` 中的调用示例
# ═══════════════════════════════════════════════════════════════════
"""
以下是如何在数据处理脚本中导入并使用本模块的参考代码：

-----
# 02_数据处理与过滤.py (片段)
import pandas as pd
from trend_judgment import judge_category_trend

# 1. 从 Excel 读取 AI 粘贴的数据
df = pd.read_excel("02_MCP返回数据.xlsx", sheet_name="量价数据表")

# 2. 提取当前类目的 24 个月数据并排序
df = df.sort_values("月份")
sales = df["月销量"].tolist()
prices = df["月均价"].tolist()
node_id = df["类目 ID"].iloc[0]
site = df["站点"].iloc[0]

# 3. 调用趋势判定模块
result = judge_category_trend(
    monthly_sales=sales,
    monthly_prices=prices,
    node_id=node_id,
    site=site,
    min_avg_sales=10000.0,    # 硬门槛
    sales_decline_tolerance=0.05,    # 销量微降 ≤5%/年可接受
    price_decline_tolerance=0.10,    # 均价微降 ≤10%/年可接受
)

# 4. 根据 result["is_valid_node"] 做分支
if result["is_valid_node"]:
    print(f"✅ 通过: {result['direction']}")
    print(result["human_summary"])
else:
    print(f"❌ 淘汰: {result['reason']}")
-----
"""


# ═══════════════════════════════════════════════════════════════════
# 独立运行演示（Mock 数据）
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 68)
    print("  trend_judgment.py — Mock 数据演示")
    print("=" * 68)

    np.random.seed(42)

    # ── 参数 ──
    months = 24
    seasonality = 1 + 0.3 * np.sin(np.linspace(0, 4 * np.pi, months))
    time_idx = np.arange(months)

    # ────────────────────────────────────────────
    # Demo 1：双升（销量 +15%/年，价格 +5%/年）
    # ────────────────────────────────────────────
    print("\n【Demo 1】双升 — 销量+15%/年 + 价格+5%/年")
    print("-" * 40)

    mock_demo1_sales = [
        max(0, 12000 * (1 + 0.15 / 12) ** t * seasonality[t] + np.random.normal(0, 300))
        for t in time_idx
    ]
    mock_demo1_prices = [
        max(5, 25.0 * (1 + 0.05 / 12) ** t + np.random.normal(0, 1.5))
        for t in time_idx
    ]

    r1 = judge_category_trend(mock_demo1_sales, mock_demo1_prices, node_id="DEMO_01", site="US", min_r_squared=0.0)
    print(f"  判定: {r1['status'].upper()} → {r1['direction']}")
    print(f"  均销量: {r1['avg_monthly_sales']:>8,.0f}  | 均价格: ${r1['avg_monthly_price']:>6.2f}")
    print(f"  销量斜率: {r1['sales_slope_annual_pct']:>+7.2f}%/年 | 价格斜率: {r1['price_slope_annual_pct']:>+7.2f}%/年")

    # ────────────────────────────────────────────
    # Demo 2：销量升 + 价格微降 -3%（应在容忍范围内）
    # ────────────────────────────────────────────
    print("\n【Demo 2】销量升 + 价格微降 -3%/年（容忍度 10%，应通过）")
    print("-" * 40)

    mock_demo2_sales = [
        max(0, 15000 * (1 + 0.10 / 12) ** t * seasonality[t] + np.random.normal(0, 400))
        for t in time_idx
    ]
    mock_demo2_prices = [
        max(5, 30.0 * (1 - 0.03 / 12) ** t + np.random.normal(0, 1.0))
        for t in time_idx
    ]

    r2 = judge_category_trend(mock_demo2_sales, mock_demo2_prices, node_id="DEMO_02", site="US", min_r_squared=0.0)
    print(f"  判定: {r2['status'].upper()} → {r2['direction']}")
    print(f"  均销量: {r2['avg_monthly_sales']:>8,.0f}  | 均价格: ${r2['avg_monthly_price']:>6.2f}")
    print(f"  销量斜率: {r2['sales_slope_annual_pct']:>+7.2f}%/年 | 价格斜率: {r2['price_slope_annual_pct']:>+7.2f}%/年")

    # ────────────────────────────────────────────
    # Demo 3：平均销量 < 10,000（硬过滤淘汰）
    # ────────────────────────────────────────────
    print("\n【Demo 3】硬过滤淘汰 — 平均销量不足 10,000")
    print("-" * 40)

    mock_demo3_sales = [800 + np.random.randint(-50, 50) for _ in range(months)]
    mock_demo3_prices = [15.0 + np.random.normal(0, 0.5) for _ in range(months)]

    r3 = judge_category_trend(mock_demo3_sales, mock_demo3_prices, node_id="DEMO_03", site="US")
    print(f"  判定: {r3['status'].upper()} → {r3['direction']}")
    print(f"  原因: {r3['reason']}")

    # ────────────────────────────────────────────
    # Demo 4：销量微降 -4%  + 价格升（容忍范围内通过）
    # ────────────────────────────────────────────
    print("\n【Demo 4】销量微降 -4%/年 + 价格升（销量容忍 5%，应通过）")
    print("-" * 40)

    mock_demo4_sales = [
        max(0, 20000 * (1 - 0.04 / 12) ** t * seasonality[t] + np.random.normal(0, 500))
        for t in time_idx
    ]
    mock_demo4_prices = [
        max(5, 20.0 * (1 + 0.08 / 12) ** t + np.random.normal(0, 1.2))
        for t in time_idx
    ]

    r4 = judge_category_trend(mock_demo4_sales, mock_demo4_prices, node_id="DEMO_04", site="US", min_r_squared=0.0)
    print(f"  判定: {r4['status'].upper()} → {r4['direction']}")
    print(f"  均销量: {r4['avg_monthly_sales']:>8,.0f}  | 均价格: ${r4['avg_monthly_price']:>6.2f}")
    print(f"  销量斜率: {r4['sales_slope_annual_pct']:>+7.2f}%/年 | 价格斜率: {r4['price_slope_annual_pct']:>+7.2f}%/年")

    # ────────────────────────────────────────────
    # Demo 5：双降（超出容忍，淘汰）
    # ────────────────────────────────────────────
    print("\n【Demo 5】双降（淘汰）")
    print("-" * 40)

    mock_demo5_sales = [
        max(0, 18000 * (1 - 0.15 / 12) ** t * seasonality[t] + np.random.normal(0, 300))
        for t in time_idx
    ]
    mock_demo5_prices = [
        max(5, 35.0 * (1 - 0.10 / 12) ** t + np.random.normal(0, 1.5))
        for t in time_idx
    ]

    r5 = judge_category_trend(mock_demo5_sales, mock_demo5_prices, node_id="DEMO_05", site="US")
    print(f"  判定: {r5['status'].upper()} → {r5['direction']}")
    print(f"  销量斜率: {r5['sales_slope_annual_pct']:>+7.2f}%/年 | 价格斜率: {r5['price_slope_annual_pct']:>+7.2f}%/年")

    print("\n" + "=" * 68)
    print("  Mock 演示结束")
    print("=" * 68)
