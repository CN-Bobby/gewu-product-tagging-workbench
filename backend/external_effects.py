"""Environment policy for operations that can incur cost or contact external services.

The run center always sets ``GEWU_PROFILE`` explicitly. Direct source launches default to offline.
DEV and TEST can independently opt into manually initiated live validation for the current process
with their profile-specific environment variables.
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
_LIVE_VALIDATION_VARIABLES = {
    "dev": "GEWU_DEV_EXTERNAL_EFFECTS",
    "test": "GEWU_TEST_EXTERNAL_EFFECTS",
}
_LIVE_VALIDATION = "live_validation"


def profile() -> str:
    """Return the normalized profile. Missing means offline DEV; unknown names fail closed."""
    raw = os.environ.get("GEWU_PROFILE", "dev").strip().lower()
    if not raw:
        return "dev"
    return _ALIASES.get(raw, "unknown")


def effects_allowed(service: str | None = None) -> bool:
    """Whether an external effect is allowed in this process.

    DEV and TEST honor only their own exact opt-in. The other profile's variable is ignored, as
    are unknown profiles. Live validation does not change DEV/TEST startup recovery (prod-only).
    """
    current = profile()
    if current == "prod":
        return True
    variable = _LIVE_VALIDATION_VARIABLES.get(current)
    raw_profile = os.environ.get("GEWU_PROFILE", "").strip().lower()
    explicitly_selected_profile = _ALIASES.get(raw_profile)
    return (
        variable is not None
        and explicitly_selected_profile == current
        and os.environ.get(variable) == _LIVE_VALIDATION
    )


def assert_allowed(service: str | None = None) -> None:
    """Fail before dispatching work that may call an external provider or public service."""
    if not effects_allowed(service):
        label = f"（{service}）" if service else ""
        current = profile()
        if current == "test":
            next_step = "请停止当前离线 TEST，并从桌面 TEST 入口选择“真实接口验收”后重新启动。"
        elif current == "dev":
            next_step = "DEV 默认离线；请从桌面 DEV 入口选择“真实接口验收”后重新启动。"
        else:
            next_step = "运行环境身份未知，已拒绝外部调用；请通过桌面 DEV 或 TEST 入口启动。"
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
    live_validation = current in _LIVE_VALIDATION_VARIABLES and any(services.values())
    return {
        "profile": current,
        "external_effects_allowed": any(services.values()),
        "policy": "online" if current == "prod" else "live_validation" if live_validation else "offline",
        "services": services,
    }
