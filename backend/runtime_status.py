"""Expose the identity and configured external-call mode of this process."""

import os


def _selected_policy(profile: str) -> str:
    if profile == "dev":
        value = os.environ.get("GEWU_DEV_EXTERNAL_EFFECTS")
    elif profile == "test":
        value = os.environ.get("GEWU_TEST_EXTERNAL_EFFECTS")
    else:
        return "managed" if profile == "prod" else "unmanaged"

    normalized = (value or "").strip().lower()
    if not normalized or normalized in {"online", "live_validation"}:
        return "live_validation"
    if normalized == "offline":
        return "offline"
    return "invalid"


def runtime_status() -> dict:
    """Return the Run Center contract without exposing paths or credentials."""
    profile = (os.environ.get("GEWU_PROFILE") or "local").strip().lower() or "local"
    instance_id = (os.environ.get("GEWU_INSTANCE_ID") or "").strip() or None
    policy = _selected_policy(profile)

    external_effects = {"policy": policy}
    if profile in {"dev", "test"}:
        external_effects["services"] = {"model_api": policy == "live_validation"}

    return {
        "status": "ok",
        "profile": profile,
        "instance_id": instance_id,
        "external_effects": external_effects,
    }
