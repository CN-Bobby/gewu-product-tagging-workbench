"""Presence-only settings for the optional Bright Data review provider."""
from __future__ import annotations

from ..credential_vault import delete_secret, get_secret, set_secret


NAMESPACE = "brightdata"
KEY_NAME = "api_key"


def status() -> dict[str, bool]:
    return {"key_configured": bool(get_secret(NAMESPACE, KEY_NAME))}


def save(api_key: str) -> dict[str, bool]:
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("请填写 Bright Data API Key")
    if len(api_key) > 2000 or any(char in api_key for char in "\r\n\0"):
        raise ValueError("Bright Data API Key 格式无效")
    set_secret(NAMESPACE, KEY_NAME, api_key)
    return status()


def clear_key() -> dict[str, bool]:
    delete_secret(NAMESPACE, KEY_NAME)
    return status()


def connection() -> str:
    key = get_secret(NAMESPACE, KEY_NAME)
    if not key:
        raise ValueError("请先在总设置中配置 Bright Data API Key")
    return key
