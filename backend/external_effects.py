"""Environment policy for operations that can incur cost or contact external services.

DEV and TEST allow external calls by default. A maintenance or offline-check process can still
explicitly set its profile-specific variable to ``offline``. Unknown profiles fail closed.
"""
from __future__ import annotations

import os


class ExternalEffectsDisabled(PermissionError):
    """Raised when an explicitly offline or unknown instance is asked to contact an external service."""

    code = "external_effects_disabled"


_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "test": "test",
    "acceptance": "test",
    "prod": "prod",
    "production": "prod",
}
_EXTERNAL_EFFECTS_VARIABLES = {
    "dev": "GEWU_DEV_EXTERNAL_EFFECTS",
    "test": "GEWU_TEST_EXTERNAL_EFFECTS",
}
def profile() -> str:
    """Return the normalized profile. Missing means DEV; unknown names fail closed."""
    raw = os.environ.get("GEWU_PROFILE", "dev").strip().lower()
    if not raw:
        return "dev"
    return _ALIASES.get(raw, "unknown")


def effects_allowed(service: str | None = None) -> bool:
    """Whether an external effect is allowed in this process.

    DEV and TEST allow calls unless their own profile-specific variable explicitly says ``offline``.
    The other profile's variable is ignored, and unknown profiles remain denied.
    """
    current = profile()
    if current == "prod":
        return True
    variable = _EXTERNAL_EFFECTS_VARIABLES.get(current)
    if variable is None:
        return False
    return os.environ.get(variable, "").strip().lower() != "offline"


def assert_allowed(service: str | None = None) -> None:
    """Fail before dispatching work that may call an external provider or public service."""
    if not effects_allowed(service):
        label = f"（{service}）" if service else ""
        current = profile()
        if current in {"test", "dev"}:
            next_step = "当前进程被明确配置为离线；请检查对应的运行参数后重新启动。"
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
    enabled = current in _EXTERNAL_EFFECTS_VARIABLES and any(services.values())
    return {
        "profile": current,
        "external_effects_allowed": any(services.values()),
        "policy": "online" if current == "prod" else "live_validation" if enabled else "offline",
        "services": services,
    }
