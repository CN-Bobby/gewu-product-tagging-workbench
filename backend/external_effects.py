"""Environment policy for operations that can incur cost or contact external services.

The run center always sets ``GEWU_PROFILE`` explicitly. A missing or unknown profile fails closed
as development, so direct source launches are safe by default. There is intentionally no ambient
environment-variable bypass: real external validation belongs in a separately managed workflow.
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


def profile() -> str:
    """Return the normalized run-center profile. Unknown/missing values are offline DEV."""
    raw = os.environ.get("GEWU_PROFILE", "dev").strip().lower()
    return _ALIASES.get(raw, "dev")


def effects_allowed(service: str | None = None) -> bool:
    """Whether an external effect is allowed in this process.

    The service parameter is reserved for future narrowly scoped validation policy. For now every
    outbound model, MCP, scraping, and other paid/network task is denied in DEV and TEST.
    """
    return profile() == "prod"


def assert_allowed(service: str | None = None) -> None:
    """Fail before dispatching work that may call an external provider or public service."""
    if not effects_allowed(service):
        label = f"（{service}）" if service else ""
        raise ExternalEffectsDisabled(
            f"{profile().upper()} 环境已关闭外部调用{label}；请使用独立、限额的专项验证流程。"
        )


def public_status() -> dict:
    current = profile()
    return {
        "profile": current,
        "external_effects_allowed": current == "prod",
        "policy": "online" if current == "prod" else "offline",
        "services": {
            "model_api": current == "prod",
            "mcp": current == "prod",
            "amazon_collection": current == "prod",
            "other_external_tasks": current == "prod",
        },
    }
