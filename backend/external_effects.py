"""Environment policy for operations that can incur cost or contact external services.

The run center always sets ``GEWU_PROFILE`` explicitly. A missing or unknown profile fails closed
as development, so direct source launches are safe by default. TEST can opt into manually initiated
live validation for the current process with ``GEWU_TEST_EXTERNAL_EFFECTS=live_validation``.
"""
from __future__ import annotations

import os


class ExternalEffectsDisabled(PermissionError):
    """Raised when an offline DEV/TEST instance is asked to contact an external service."""

    code = "external_effects_disabled"


_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "test": "test",
    "acceptance": "test",
    "prod": "prod",
    "production": "prod",
}
_TEST_LIVE_VALIDATION = "live_validation"


def profile() -> str:
    """Return the normalized run-center profile. Unknown/missing values are offline DEV."""
    raw = os.environ.get("GEWU_PROFILE", "dev").strip().lower()
    return _ALIASES.get(raw, "dev")


def effects_allowed(service: str | None = None) -> bool:
    """Whether an external effect is allowed in this process.

    The exact opt-in is honored only by the TEST profile. It is deliberately ignored by DEV and
    unknown profiles, and does not change TEST startup recovery (which remains prod-only).
    """
    current = profile()
    if current == "prod":
        return True
    return current == "test" and os.environ.get("GEWU_TEST_EXTERNAL_EFFECTS") == _TEST_LIVE_VALIDATION


def assert_allowed(service: str | None = None) -> None:
    """Fail before dispatching work that may call an external provider or public service."""
    if not effects_allowed(service):
        label = f"（{service}）" if service else ""
        current = profile()
        if current == "test":
            next_step = "请停止当前离线 TEST，并从桌面 TEST 入口选择“真实接口验收”后重新启动。"
        else:
            next_step = "DEV 环境保持离线；需要真实接口验收时，请从桌面 TEST 入口选择“真实接口验收”后启动。"
        raise ExternalEffectsDisabled(
            f"{current.upper()} 环境当前策略未允许外部调用{label}，本次没有发起调用。{next_step}"
        )


def public_status() -> dict:
    current = profile()
    services = {
        "model_api": effects_allowed("model_api"),
        "mcp": effects_allowed("mcp"),
        "amazon_collection": effects_allowed("amazon_collection"),
        "other_external_tasks": effects_allowed("other_external_tasks"),
    }
    live_validation = current == "test" and any(services.values())
    return {
        "profile": current,
        "external_effects_allowed": any(services.values()),
        "policy": "online" if current == "prod" else "live_validation" if live_validation else "offline",
        "services": services,
    }
