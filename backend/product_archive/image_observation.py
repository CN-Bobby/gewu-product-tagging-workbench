"""One bounded visual observation of selected Product Archive images.

This reuses the workbench's validated vision provider and OpenAI-compatible
image input contract. Reasonix receives only the resulting short descriptions.
"""
from __future__ import annotations

import base64
import io
import time

import httpx
from PIL import Image

from ..external_effects import assert_allowed
from ..usage import finish_usage_event, start_usage_event
from ..vision import provider_config
from .overview_drafts import _parse_response_content

MAX_IMAGES = 4
MAX_OUTPUT_TOKENS = 1600


def observe(image_paths: list[str], *, model_ref: str, credential_id: str,
            run_id: str) -> dict:
    """Describe visible product features once; fail without blocking text research."""
    if not image_paths:
        return {"observations": [], "image_count": 0, "usage": {}, "issue": ""}
    assert_allowed("model_api")
    parts = [{"type": "text", "text": (
        "逐张观察商品图片，只描述实际可见的形态、覆盖范围、袖长、连体/分体、领口、图案和搭配。"
        "不要推断 UPF 数值、性能、销量、人群比例或未显示的结构。"
        "只返回 JSON：{\"images\":[{\"index\":1,\"visible_features\":\"...\"}]}。"
        "每张不超过 200 字。图片编号按输入顺序从 1 开始。") }]
    issues = []
    for index, path in enumerate(image_paths[:MAX_IMAGES], 1):
        try:
            with Image.open(path) as image:
                image.thumbnail((1400, 1400))
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=82)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + encoded}})
        except (OSError, ValueError):
            issues.append(f"第 {index} 张关联图片无法读取")
    image_count = len(parts) - 1
    if not image_count:
        return {"observations": [], "image_count": 0, "usage": {},
                "issue": "；".join(issues) or "关联图片不可读取"}
    event_id, started = None, time.perf_counter()
    try:
        endpoint, key, model = provider_config(model_ref, credential_id)
        event_id = start_usage_event(kind="model", module="product_archive", task_id=run_id,
            task_title="选品档案图片客观观察", model=model_ref, provider=model_ref.partition("/")[0],
            metadata={"stage": "image_observation", "image_count": image_count,
                      "output_tokens_max": MAX_OUTPUT_TOKENS}, request_text=parts)
        body = {"model": model, "messages": [{"role": "user", "content": parts}],
                "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.1,
                "response_format": {"type": "json_object"}}
        with httpx.Client(timeout=120) as client:
            response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=body)
        if response.status_code >= 400:
            raise ValueError(f"HTTP {response.status_code}")
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("output truncated")
        parsed = _parse_response_content(choice["message"]["content"])
        values = parsed.get("images")
        if not isinstance(values, list):
            raise ValueError("invalid vision response")
        observations = []
        for item in values:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            detail = item.get("visible_features")
            if isinstance(index, int) and 1 <= index <= image_count and isinstance(detail, str) and detail.strip():
                observations.append({"image_index": index, "visible_features": detail.strip()[:500]})
        if not observations:
            raise ValueError("empty vision response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        finish_usage_event(event_id, status="completed",
            duration_ms=round((time.perf_counter() - started) * 1000), usage=usage)
        return {"observations": observations, "image_count": image_count,
                "usage": usage, "issue": "；".join(issues)}
    except Exception as exc:
        finish_usage_event(event_id, status="failed",
            duration_ms=round((time.perf_counter() - started) * 1000))
        return {"observations": [], "image_count": image_count, "usage": {},
                "issue": f"视觉观察未完成（{type(exc).__name__}）；未将图片视为已分析"}
