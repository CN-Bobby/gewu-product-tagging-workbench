from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


# Bound bulk VOC calls; compact synthesis excludes dictionaries, while term normalization
# only receives a small high-frequency candidate list and emits merge groups.
DEEPSEEK_V4_MAX_OUTPUT_TOKENS = 65_536
DEEPSEEK_V4_ASSIGN_MAX_OUTPUT_TOKENS = 16_384
DEEPSEEK_V4_DICTIONARY_MAX_OUTPUT_TOKENS = 8_192
FULL_VOC_V2_TASKS = {"full_voc_v2", "full_voc_v2_extract", "full_voc_v2_synthesis", "full_voc_v2_assign",
                     "full_voc_v2_dictionary"}


class ModelError(RuntimeError):
    def __init__(self, message, *, retryable=True, status_code=None, usage=None,
                 response_meta=None):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.status_code = status_code
        self.usage = usage if isinstance(usage, dict) else {}
        self.response_meta = response_meta if isinstance(response_meta, dict) else {}


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


def _parse_json_text(text):
    """Parse strict JSON, tolerating a surrounding Markdown fence or short preface."""
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\\{\\[]", candidate):
            try:
                value, _ = decoder.raw_decode(candidate, match.start())
                return value
            except json.JSONDecodeError:
                continue
        raise original


def _usage_tokens(usage, protocol):
    """Return reported token counts, preserving missing cache usage as unknown."""
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = {}

    if protocol == "responses":
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_hit = details.get("cached_tokens")
        cache_miss = None
    else:
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        # DeepSeek reports hit and miss separately. Other Chat Completions
        # providers commonly report cached_tokens in prompt token details.
        cache_hit = usage.get("prompt_cache_hit_tokens")
        if cache_hit is None:
            cache_hit = details.get("cached_tokens")
        cache_miss = usage.get("prompt_cache_miss_tokens")

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_input_tokens": cache_hit,
        "cache_miss_input_tokens": cache_miss,
    }


class OpenAICompatibleProvider:
    def __init__(self, settings):
        self.settings = settings

    def _request(self, url, body, timeout=180):
        request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.settings.model_api_key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def call(self, task, model, instructions, payload, schema):
        from backend.external_effects import assert_allowed
        assert_allowed("model_api")
        if not self.settings.model_api_key:
            raise ModelError("未配置 VOC_MODEL_API_KEY", retryable=False)
        started = time.perf_counter()
        request_timeout = (self.settings.full_voc_timeout_seconds
                           if task in FULL_VOC_V2_TASKS else 180)
        if self.settings.model_protocol == "responses":
            url = self.settings.model_base_url + "/responses"
            body = {"model": model, "instructions": instructions,
                    "input": json.dumps(payload, ensure_ascii=False),
                    "text": {"format": {"type": "json_schema", "name": task, "strict": True, "schema": schema}},
                    "prompt_cache_key": "amazon-voc-%s" % task}
            if task in FULL_VOC_V2_TASKS and "deepseek" in str(model or "").casefold():
                body["max_output_tokens"] = (DEEPSEEK_V4_ASSIGN_MAX_OUTPUT_TOKENS
                                              if task == "full_voc_v2_assign" else
                                              DEEPSEEK_V4_DICTIONARY_MAX_OUTPUT_TOKENS
                                              if task == "full_voc_v2_dictionary" else
                                              DEEPSEEK_V4_MAX_OUTPUT_TOKENS)
                if task == "full_voc_v2_synthesis":
                    body["reasoning"] = {"effort": "low"}
                elif task == "full_voc_v2_assign":
                    body["reasoning"] = {"effort": "none"}
                elif task == "full_voc_v2_dictionary":
                    body["reasoning"] = {"effort": "low"}
        else:
            url = self.settings.model_base_url + "/chat/completions"
            deepseek_chat = "deepseek" in str(model or "").casefold()
            system_instructions = instructions
            response_format = {"type": "json_schema", "json_schema": {
                "name": task, "strict": True, "schema": schema}}
            if deepseek_chat:
                system_instructions += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构：" + json.dumps(schema, ensure_ascii=False)
                response_format = {"type": "json_object"}
            body = {"model": model, "messages": [
                {"role": "system", "content": system_instructions},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                "response_format": response_format}
            if task in FULL_VOC_V2_TASKS and deepseek_chat:
                body["max_tokens"] = (DEEPSEEK_V4_ASSIGN_MAX_OUTPUT_TOKENS
                                      if task == "full_voc_v2_assign" else
                                      DEEPSEEK_V4_DICTIONARY_MAX_OUTPUT_TOKENS
                                      if task == "full_voc_v2_dictionary" else
                                      DEEPSEEK_V4_MAX_OUTPUT_TOKENS)
                if task == "full_voc_v2_assign":
                    body["thinking"] = {"type": "disabled"}
                elif task == "full_voc_v2_dictionary":
                    body["thinking"] = {"type": "disabled"}
        try:
            result = self._request(url, body, timeout=request_timeout)
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

            # Some providers support JSON object mode but not strict JSON Schema.
            if self.settings.model_protocol == "responses":
                body["text"] = {"format": {"type": "json_object"}}
                body["instructions"] += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构。"
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"][0]["content"] += "\n仅返回合法 JSON 对象，不要添加 Markdown 或解释文字。输出须符合给定结构：" + json.dumps(schema, ensure_ascii=False)
            try:
                result = self._request(url, body, timeout=request_timeout)
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
        response_meta = {}
        if self.settings.model_protocol == "responses":
            text = _output_text(result)
            usage = result.get("usage") or {}
            response_meta = {"status": result.get("status"),
                             "incomplete_reason": (result.get("incomplete_details") or {}).get("reason")
                             if isinstance(result.get("incomplete_details"), dict) else None}
        else:
            choice = (result.get("choices") or [{}])[0]
            text = ((choice.get("message") or {}).get("content") or "")
            usage = result.get("usage") or {}
            response_meta = {"finish_reason": choice.get("finish_reason")}
        token_usage = _usage_tokens(usage, self.settings.model_protocol)
        if (self.settings.model_protocol == "responses" and response_meta.get("status")
                and response_meta.get("status") != "completed"):
            reason = response_meta.get("incomplete_reason") or "unknown"
            raise ModelError("模型 Responses 响应未完成 (status=%s, reason=%s)" %
                             (response_meta["status"], reason), retryable=False,
                             usage={"latency_ms": int((time.perf_counter() - started) * 1000),
                                    **token_usage}, response_meta=response_meta)
        try:
            parsed = _parse_json_text(text)
        except json.JSONDecodeError as exc:
            diagnostic = ", ".join("%s=%s" % (key, value) for key, value in response_meta.items()
                                   if value is not None)
            message = "模型未返回有效 JSON (output_chars=%s%s)" % (
                len(text), ", " + diagnostic if diagnostic else "")
            raise ModelError(message, retryable=True,
                             usage={"latency_ms": int((time.perf_counter() - started) * 1000),
                                    **token_usage},
                             response_meta={**response_meta, "output_chars": len(text)}) from exc
        return parsed, {"latency_ms": int((time.perf_counter() - started) * 1000),
                        **token_usage}
