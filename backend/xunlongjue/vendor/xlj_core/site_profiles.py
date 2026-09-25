#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Amazon 站点的唯一配置源。"""
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class SiteProfile:
    code: str
    domain: str
    locale: str
    currency_code: str
    currency_symbol: str
    aliases: Tuple[str, ...] = ()


_PROFILES = (
    SiteProfile("US", "www.amazon.com", "en-US", "USD", "$"),
    SiteProfile("UK", "www.amazon.co.uk", "en-GB", "GBP", "£", ("GB",)),
    SiteProfile("DE", "www.amazon.de", "de-DE", "EUR", "€"),
    SiteProfile("FR", "www.amazon.fr", "fr-FR", "EUR", "€"),
    SiteProfile("IT", "www.amazon.it", "it-IT", "EUR", "€"),
    SiteProfile("ES", "www.amazon.es", "es-ES", "EUR", "€"),
    SiteProfile("CA", "www.amazon.ca", "en-CA", "CAD", "C$"),
    SiteProfile("JP", "www.amazon.co.jp", "ja-JP", "JPY", "¥"),
)

SITE_PROFILES: Dict[str, SiteProfile] = {p.code: p for p in _PROFILES}
SITE_ALIASES: Dict[str, str] = {
    alias: profile.code
    for profile in _PROFILES
    for alias in (profile.code, *profile.aliases)
}
SUPPORTED_SITES = tuple(SITE_PROFILES)


def normalize_site(site: str) -> str:
    """返回规范站点代码；空值或不支持的站点直接报错。"""
    raw = str(site or "").strip().upper()
    if not raw:
        raise ValueError("站点不能为空。支持: " + ", ".join(SUPPORTED_SITES))
    try:
        return SITE_ALIASES[raw]
    except KeyError as exc:
        raise ValueError(
            f"不支持的站点 '{site}'。支持: {', '.join(SUPPORTED_SITES)}；GB 可作为 UK 别名"
        ) from exc


def get_site_profile(site: str) -> SiteProfile:
    return SITE_PROFILES[normalize_site(site)]


def _normalize_node_id(node_id: str) -> str:
    value = str(node_id or "").strip()
    if not value or not value.isdigit():
        raise ValueError(f"Node ID 必须为纯数字，收到: {node_id!r}")
    return value


def get_category_url(site: str, node_id: str) -> str:
    profile = get_site_profile(site)
    return f"https://{profile.domain}/b?node={_normalize_node_id(node_id)}"


def get_bestseller_url(site: str, node_id: str) -> str:
    """生成 Best Sellers 榜单链接；kitchen 仅为 Amazon 路由占位段。"""
    profile = get_site_profile(site)
    return (
        f"https://{profile.domain}/gp/bestsellers/kitchen/"
        f"{_normalize_node_id(node_id)}"
    )


def format_currency(value, site: str, precision: int = 2) -> str:
    if value is None:
        return ""
    profile = get_site_profile(site)
    return f"{profile.currency_symbol}{float(value):,.{precision}f}"
