# -*- coding: utf-8 -*-
"""输入解析与校验：ASIN 格式/去重/每站点组 ≤ 20、站点绑定、月份格式。"""
from __future__ import annotations

import re
from typing import Iterable

ASIN_RE = re.compile(r"^B0[A-Z0-9]{8}$")
MONTH_RE = re.compile(r"^\d{4}(0[1-9]|1[0-2])$")

# SellerSprite 支持的站点（常见集合；校验只做白名单提示不做硬拒绝）
MARKETPLACES = {
    "US", "UK", "DE", "FR", "IT", "ES", "JP", "CA", "AU", "IN", "MX",
    "BR", "NL", "SE", "PL", "TR", "AE", "SA", "SG",
}

MAX_ASINS_PER_GROUP = 20


class ValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("；".join(errors))


def extract_asins(text: str) -> list[str]:
    """从粘贴文本提取 ASIN：按空白/逗号/分号/换行切分，忽略非法片段并保留合法项。"""
    parts = re.split(r"[\s,;，；、]+", text.strip())
    found = []
    for p in parts:
        p = p.strip().upper()
        if not p:
            continue
        if ASIN_RE.match(p):
            found.append(p)
    return found


def dedupe(asins: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for a in asins:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def normalize_month(month: str | None) -> str | None:
    """校验 yyyyMM 格式；空值返回 None（执行当月）。"""
    if month is None:
        return None
    m = month.strip()
    if not MONTH_RE.match(m):
        raise ValidationError([f"快照月份格式应为 yyyyMM（如 202607），收到：{month!r}"])
    return m


def validate_group(marketplace: str, asins: list[str],
                   node_ids: list[str] | None = None,
                   node_top_n: int | None = None,
                   max_asins: int = MAX_ASINS_PER_GROUP) -> dict:
    """校验并规整一个站点组。

    返回规整后的 group dict：marketplace / asins（去重后）/ node_ids / node_top_n。
    校验失败抛 ValidationError（errors 为全部问题列表）。
    """
    errors: list[str] = []
    mp = (marketplace or "").strip().upper()
    if not mp:
        errors.append("站点不能为空")
    elif mp not in MARKETPLACES:
        errors.append(f"站点 {mp!r} 不在支持列表（支持：{sorted(MARKETPLACES)}）")

    cleaned: list[str] = []
    for a in asins:
        a = (a or "").strip().upper()
        if not a:
            continue
        if ASIN_RE.match(a):
            cleaned.append(a)
        else:
            errors.append(f"ASIN 格式非法：{a}")
    cleaned = dedupe(cleaned)

    if not cleaned and not node_ids:
        errors.append("至少输入 1 个 ASIN 或 Node ID")

    if len(cleaned) > max_asins:
        errors.append(f"单个站点组 ASIN 最多 {max_asins} 个（去重后），收到 {len(cleaned)} 个")

    nodes = [str(x).strip() for x in (node_ids or []) if str(x).strip()]
    if node_top_n is not None and node_top_n not in (50, 100):
        errors.append("node_top_n 仅支持 50 或 100")

    if errors:
        raise ValidationError(errors)

    return {
        "marketplace": mp,
        "asins": cleaned,
        "node_ids": nodes,
        "node_top_n": node_top_n if node_top_n is not None else 50,
    }


def validate_task(groups: list[dict], month: str | None,
                  matrix_month: str | None = None,
                  max_asins: int = MAX_ASINS_PER_GROUP) -> dict:
    """校验整个任务输入，返回 {groups, options:{data_month, matrix_month}}。

    matrix_month=None → 不写入 options（矩阵跟随 data_month）。
    """
    if not groups:
        raise ValidationError(["至少需要一个站点组"])
    normalized_month = normalize_month(month)
    normalized_matrix = normalize_month(matrix_month)
    normalized_groups = [
        validate_group(
            g.get("marketplace", ""),
            list(g.get("asins") or []),
            list(g.get("node_ids") or []),
            g.get("node_top_n"),
            max_asins=max_asins,
        )
        for g in groups
    ]
    options: dict = {"data_month": normalized_month}
    if normalized_matrix:
        options["matrix_month"] = normalized_matrix
    return {"groups": normalized_groups, "options": options}
