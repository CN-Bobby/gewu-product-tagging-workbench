"""Visual prepass uses the configured vision model with bounded image input."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.product_archive import image_observation


def _image(path: Path):
    Image.new("RGB", (32, 24), color=(20, 100, 180)).save(path)


def test_selected_images_are_sent_as_image_blocks_and_return_bounded_observations(tmp_path, monkeypatch):
    path = tmp_path / "selected.jpg"
    _image(path)
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content":
                '{"images":[{"index":1,"visible_features":"Long sleeve swim top"}]}'}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 50}}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(image_observation, "assert_allowed", lambda _service: None)
    monkeypatch.setattr(image_observation, "provider_config", lambda *_args: (
        "https://provider.example/chat/completions", "synthetic-secret", "vision-model"))
    monkeypatch.setattr(image_observation.httpx, "Client", Client)
    monkeypatch.setattr(image_observation, "start_usage_event", lambda **_kwargs: "event")
    monkeypatch.setattr(image_observation, "finish_usage_event", lambda *_args, **_kwargs: None)

    result = image_observation.observe([str(path)], model_ref="provider/vision-model",
        credential_id="selected-id", run_id="run-one")

    assert result["observations"] == [{"image_index": 1,
        "visible_features": "Long sleeve swim top"}]
    assert result["image_count"] == 1
    assert result["issue"] == ""
    body = calls[0][1]["json"]
    assert body["messages"][0]["content"][1]["type"] == "image_url"
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,")


def test_failed_vision_is_explicitly_excluded_from_research(tmp_path, monkeypatch):
    path = tmp_path / "selected.jpg"
    _image(path)
    monkeypatch.setattr(image_observation, "assert_allowed", lambda _service: None)
    monkeypatch.setattr(image_observation, "provider_config", lambda *_args: (
        "https://provider.example/chat/completions", "synthetic-secret", "vision-model"))
    monkeypatch.setattr(image_observation, "start_usage_event", lambda **_kwargs: "event")
    monkeypatch.setattr(image_observation, "finish_usage_event", lambda *_args, **_kwargs: None)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(image_observation.httpx, "Client", Client)
    result = image_observation.observe([str(path)], model_ref="provider/vision-model",
        credential_id="selected-id", run_id="run-one")
    assert result["observations"] == []
    assert "未将图片视为已分析" in result["issue"]
