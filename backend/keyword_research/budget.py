# -*- coding: utf-8 -*-
"""MCP 调用预算计算（skill v1.6.2 references/mcp-routing.md §6 公式）。

K = keyword_pool_pages（新任务固定 3）
ASIN 模式:   ceil(A/20) × K + A + T
Node 模式:   N + ΣP_i + ceil(U/20) × K + T
混合模式:    N + ΣP_i + ceil(U/20) × K + A + T
其中 A=同站点去重直接 ASIN 数；N=Node 数；P_i=Node i 榜单页数（ceil(top_n/50)）；
U=直接 ASIN 与 Node 榜单合并去重 ASIN 数（预算阶段按上限估算）；
T=趋势词数（core+longtail，受 cap 限制）。
公式里的 A 项是逐 ASIN traffic_keyword 矩阵调用：options.matrix_enabled 缺省为 True；
显式为假时矩阵调用数为 0，A 项从 total 中扣除，其余项不变。
M1 模式（单输入 ASIN + Node）：输入 ASIN 单独批次反查，+1 次。
基准外最多 10 次临时错误补调用；硬上限默认 200。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class GroupBudget:
    marketplace: str
    direct_asins: int
    node_count: int
    node_top_n: int
    node_pages: int = 0
    pool_batches: int = 0
    matrix_calls: int = 0
    trend_calls: int = 0
    extra_m1: int = 0
    total: int = 0

    def detail_lines(self) -> list[str]:
        lines = [
            f"[{self.marketplace}] 直接ASIN {self.direct_asins} 个 / Node {self.node_count} 个(Top{self.node_top_n})",
            f"  traffic_extend 反查: {self.pool_batches} 批 × {3} 页 = {self.pool_batches * 3} 次",
            f"  traffic_keyword 矩阵: {self.matrix_calls} 次",
            f"  榜单取样 product_research: {self.node_pages} 次" if self.node_pages else "",
            f"  M1 单独批次: +{self.extra_m1} 次" if self.extra_m1 else "",
            f"  趋势 aba_research_trend: {self.trend_calls} 次",
        ]
        return [ln for ln in lines if ln]

    @property
    def summary(self) -> str:
        return (f"[{self.marketplace}] {self.total} 次"
                f"（反查{self.pool_batches * 3} + 矩阵{self.matrix_calls}"
                + (f" + 榜单{self.node_pages}" if self.node_pages else "")
                + (f" + M1{self.extra_m1}" if self.extra_m1 else "")
                + f" + 趋势{self.trend_calls}）")


@dataclass
class Budget:
    groups: list[GroupBudget] = field(default_factory=list)
    trend_cap: int = 20
    hard_cap: int = 200
    retry_budget: int = 10

    @property
    def planned_total(self) -> int:
        return sum(g.total for g in self.groups)

    @property
    def effective_hard_cap(self) -> int:
        return max(self.hard_cap, self.planned_total + self.retry_budget)

    def detail_lines(self) -> list[str]:
        lines: list[str] = []
        for g in self.groups:
            lines.extend(g.detail_lines())
        lines.append(f"预计总调用: {self.planned_total} 次（基准外临时错误重试最多 {self.retry_budget} 次，硬上限 {self.effective_hard_cap} 次）")
        return lines

    def as_dict(self) -> dict:
        return {
            "groups": [
                {
                    "marketplace": g.marketplace,
                    "direct_asins": g.direct_asins,
                    "node_count": g.node_count,
                    "node_top_n": g.node_top_n,
                    "node_pages": g.node_pages,
                    "pool_batch_calls": g.pool_batches * 3,
                    "matrix_calls": g.matrix_calls,
                    "trend_calls": g.trend_calls,
                    "extra_m1": g.extra_m1,
                    "total": g.total,
                }
                for g in self.groups
            ],
            "planned_total": self.planned_total,
            "hard_cap": self.effective_hard_cap,
            "retry_budget": self.retry_budget,
        }


def _trend_count(core: int, longtail: int, cap: int) -> int:
    return max(0, min(core + longtail, cap))


def plan_budget(
    groups: list[dict],
    options: dict | None = None,
    keyword_pool_pages: int = 3,
    trend_cap: int = 20,
    core_trend: int = 5,
    longtail_trend: int = 5,
) -> Budget:
    """按 input.json 的 groups/options 计算预算。groups 元素须已校验去重。"""
    options = options or {}
    matrix_enabled = bool(options.get("matrix_enabled", True))
    t = _trend_count(
        int(options.get("core_trend_keywords", core_trend)),
        int(options.get("longtail_trend_keywords", longtail_trend)),
        int(options.get("trend_keyword_cap", trend_cap)),
    )
    budget = Budget(trend_cap=t, hard_cap=200, retry_budget=10)
    for g in groups:
        marketplace = g["marketplace"]
        asins = list(g.get("asins") or [])
        node_ids = list(g.get("node_ids") or [])
        node_top_n = int(g.get("node_top_n") or 50)
        a = len(asins)
        n = len(node_ids)
        node_pages = sum(max(1, math.ceil(node_top_n / 50)) for _ in node_ids)
        # 估算合并去重 ASIN 上限：直接 ASIN + 每 Node 最多 node_top_n 个
        u_est = a + n * node_top_n
        if n > 0 and a > 0:
            # 混合模式：反查池 = 直接 ASIN + Node 榜单 ASIN（预算按上限估算）
            if a == 1:
                # M1：直接 ASIN 单独批次（来源归因，实拉 3 页）+ 榜单余量批
                pool_batches = 1 + max(0, math.ceil((u_est - a) / 20))
            else:
                pool_batches = math.ceil(u_est / 20)
        elif n > 0:
            # 纯 Node
            pool_batches = math.ceil(u_est / 20) if u_est > 0 else 0
        else:
            pool_batches = math.ceil(a / 20) if a > 0 else 0
        matrix_calls = a if matrix_enabled else 0
        total = pool_batches * keyword_pool_pages + matrix_calls + t
        extra_m1 = 1 if (a == 1 and n >= 1) else 0  # 展示标记；费用已计入 pool_batches
        if n:
            # product_node 校验每 Node 一次 + 榜单取样页数
            total += n + node_pages
        gb = GroupBudget(
            marketplace=marketplace,
            direct_asins=a,
            node_count=n,
            node_top_n=node_top_n,
            node_pages=node_pages,
            pool_batches=pool_batches,
            matrix_calls=matrix_calls,
            trend_calls=t,
            extra_m1=extra_m1,
            total=total,
        )
        budget.groups.append(gb)
    return budget
