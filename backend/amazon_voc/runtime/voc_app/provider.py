from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class ModelError(RuntimeError):
    pass


def _output_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    texts = []
    for item in payload.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") in ("output_text", "text") and part.get("text"):
                texts.append(part["text"])
    return "".join(texts)


class OpenAICompatibleProvider:
    def __init__(self, settings):
        self.settings = settings

    def call(self, task, model, instructions, payload, schema):
        if not self.settings.model_api_key:
            raise ModelError("未配置 VOC_MODEL_API_KEY")
        started = time.perf_counter()
        if self.settings.model_protocol == "responses":
            url = self.settings.model_base_url + "/responses"
            body = {"model": model, "instructions": instructions,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "text": {"format": {"type": "json_schema", "name": task, "strict": True, "schema": schema}},
                    "prompt_cache_key": "amazon-voc-%s" % task}
        else:
            url = self.settings.model_base_url + "/chat/completions"
            body = {"model": model, "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": {"type": "json_schema", "json_schema": {"name": task, "strict": True, "schema": schema}}}
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.settings.model_api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code not in (400, 422):
                raise ModelError("模型接口 HTTP %s: %s" % (exc.code, detail[:1000])) from exc
            # Some compatible gateways implement JSON mode but not strict JSON Schema.
            if self.settings.model_protocol == "responses":
                body["text"] = {"format": {"type": "json_object"}}
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"][0]["content"] += "\n输出必须匹配以下JSON Schema：" + json.dumps(schema, ensure_ascii=False)
            if self.settings.model_protocol == "responses":
                body["instructions"] = instructions + "\n输出必须匹配以下JSON Schema：" + json.dumps(schema, ensure_ascii=False)
            fallback = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"Authorization": "Bearer " + self.settings.model_api_key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(fallback, timeout=180) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except Exception as fallback_exc:
                raise ModelError("严格Schema与JSON模式均失败: %s" % detail[:1000]) from fallback_exc
        except Exception as exc:
            raise ModelError("模型接口调用失败: %s" % exc) from exc
        if self.settings.model_protocol == "responses":
            text = _output_text(result)
            usage = result.get("usage") or {}
            tokens = (usage.get("input_tokens"), usage.get("output_tokens"))
        else:
            text = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            usage = result.get("usage") or {}
            tokens = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError("模型未返回有效 JSON: %s" % text[:500]) from exc
        return parsed, {"latency_ms": int((time.perf_counter() - started) * 1000),
                        "input_tokens": tokens[0], "output_tokens": tokens[1]}
