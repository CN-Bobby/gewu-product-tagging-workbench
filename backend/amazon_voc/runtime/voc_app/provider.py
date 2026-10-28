from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar


_USAGE_CONTEXT = ContextVar("amazon_voc_usage_context", default=None)


@contextmanager
def usage_scope(context):
    token = _USAGE_CONTEXT.set(dict(context or {}))
    try:
        yield
    finally:
        _USAGE_CONTEXT.reset(token)


class ModelError(RuntimeError):
    def __init__(self, message, *, retryable=True, status_code=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = status_code


def _retryable_status(status_code):
    return status_code in (408, 429) or status_code >= 500


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

    def _request(self, url, body):
        from .... import usage as usage_stats

        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.settings.model_api_key, "Content-Type": "application/json"})
        context = _USAGE_CONTEXT.get() or {}
        request_no = int(context.get("request_no") or 0) + 1
        context["request_no"] = request_no
        event_id = usage_stats.start_usage_event(
            kind="model", module="amazon_voc", task_id=context.get("run_id"),
            task_title=context.get("task_title"), model=body.get("model"),
            provider=usage_stats.provider_name(body.get("model"), url),
            attempt=context.get("pipeline_attempt", 1), usage_source="provider_response",
            request_text=body,
            metadata={key: value for key, value in context.items() if key != "request_no"}
                     | {"request_no": request_no},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
        except Exception:
            usage_stats.finish_usage_event(event_id, status="failed",
                duration_ms=round((time.perf_counter() - started) * 1000))
            raise
        response_text = _output_text(result)
        if not response_text:
            response_text = (((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        usage_stats.finish_usage_event(event_id, status="success",
            duration_ms=round((time.perf_counter() - started) * 1000),
            usage=result.get("usage") or {}, request_text=body,
            response_text=response_text or None)
        return result

    def call(self, task, model, instructions, payload, schema):
        if not self.settings.model_api_key:
            raise ModelError("未配置 VOC_MODEL_API_KEY", retryable=False)
        started = time.perf_counter()
        if self.settings.model_protocol == "responses":
            url = self.settings.model_base_url + "/responses"
            body = {"model": model, "instructions": instructions,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "text": {"format": {"type": "json_schema", "name": task, "strict": True, "schema": schema}},
                    "prompt_cache_key": "amazon-voc-%s" % task}
        else:
            url = self.settings.model_base_url + "/chat/completions"
            deepseek_chat = "deepseek" in str(model or "").casefold()
            system_instructions = instructions
            response_format = {"type": "json_schema", "json_schema": {
                "name": task, "strict": True, "schema": schema}}
            if deepseek_chat:
                # DeepSeek Chat Completions documents JSON object mode rather than
                # strict JSON Schema. Keep the application schema check after parsing.
                system_instructions += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构：" + json.dumps(schema, ensure_ascii=False)
                response_format = {"type": "json_object"}
            body = {"model": model, "messages": [
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": response_format}
        try:
            result = self._request(url, body)
        except urllib.error.HTTPError as exc:
            # Never persist the remote response body: some gateways echo request content.
            status_code = int(exc.code)
            try:
                diagnostic = exc.read(4096).decode("utf-8", "replace").casefold()
            except Exception:
                diagnostic = ""
            try:
                exc.close()
            except Exception:
                pass
            if status_code not in (400, 422):
                raise ModelError("模型接口 HTTP %s" % status_code,
                                 retryable=_retryable_status(status_code), status_code=status_code) from None
            schema_rejection = any(marker in diagnostic for marker in (
                "json_schema", "response_format", "structured output", "schema is not supported",
                "schema not supported", "unsupported schema", "schema unavailable",
            ))
            if not schema_rejection:
                raise ModelError("模型接口 HTTP %s（非 Schema 兼容性错误）" % status_code,
                                 retryable=False, status_code=status_code) from None

            # A number of chat-completions providers support JSON object mode but not
            # strict JSON Schema. Keep local schema validation authoritative.
            if self.settings.model_protocol == "responses":
                body["text"] = {"format": {"type": "json_object"}}
                body["instructions"] += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构。"
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"][0]["content"] += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构：" + json.dumps(schema, ensure_ascii=False)
            try:
                result = self._request(url, body)
            except urllib.error.HTTPError as fallback_exc:
                fallback_status = int(fallback_exc.code)
                try:
                    fallback_exc.close()
                except Exception:
                    pass
                raise ModelError(
                    "严格 JSON Schema 请求返回 HTTP %s；JSON 对象模式返回 HTTP %s" %
                    (status_code, fallback_status),
                    retryable=_retryable_status(fallback_status), status_code=fallback_status) from None
            except Exception as fallback_exc:
                raise ModelError("严格 JSON Schema 请求被拒绝；JSON 对象模式请求失败 (%s)" %
                                 type(fallback_exc).__name__, retryable=True) from None
        except urllib.error.URLError as exc:
            raise ModelError("模型接口连接失败 (%s)" % type(exc.reason).__name__, retryable=True) from None
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError("模型接口调用失败 (%s)" % type(exc).__name__, retryable=True) from None

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
            raise ModelError("模型未返回有效 JSON", retryable=True) from exc
        return parsed, {"latency_ms": int((time.perf_counter() - started) * 1000),
                        "input_tokens": tokens[0], "output_tokens": tokens[1]}
