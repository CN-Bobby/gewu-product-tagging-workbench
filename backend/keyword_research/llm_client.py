# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容接口，自动识别 Responses API / Chat Completions。

需求 3 容量设计相关职责：
- 单次请求的 JSON 结构化输出（response_format）
- finish_reason=length 截断检测（由调用方决定拆小重试）
- DeepSeek 结构化任务关闭思考（reasoning.enabled=false，兼容模式自动识别）
- 上下文容量由 pipeline 分批控制，本客户端只负责单次请求。
"""
from __future__ import annotations

import json
from typing import Any, Optional

import httpx

DEFAULT_TIMEOUT = 120.0


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False,
                 truncated: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.truncated = truncated


class LLMResult:
    def __init__(self, text: str, *, finish_reason: str | None = None,
                 model: str | None = None, mode: str = "chat",
                 reasoning_tokens: int = 0) -> None:
        self.text = text
        self.finish_reason = finish_reason
        self.model = model
        self.mode = mode
        self.reasoning_tokens = reasoning_tokens

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def json(self) -> Any:
        """解析结构化输出为 Python 对象。"""
        text = self.text.strip()
        # 容忍 ```json ... ``` 围栏
        if text.startswith("```"):
            first_nl = text.find("\n")
            last = text.rfind("```")
            text = text[first_nl + 1:last].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 输出不是合法 JSON: {exc}") from exc


class LLMClient:
    """OpenAI 兼容 LLM 客户端。

    mode: "auto"（先 Responses 后 Chat Completions 回退）| "responses" | "chat"
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 mode: str = "auto", temperature: float = 0.2,
                 max_tokens: int = 4096, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.mode = mode
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.detected_mode: str | None = None  # 探测成功后记录

    # ---------- 内部 HTTP ----------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            resp = httpx.post(
                f"{self.base_url}{path}", json=body, headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"模型请求失败: {exc}", retryable=True) from exc
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            detail = resp.text[:500]
        except Exception:
            pass
        retryable = resp.status_code in (429, 500, 502, 503, 504)
        raise LLMError(
            f"模型接口 HTTP {resp.status_code}: {detail}", retryable=retryable
        )

    # ---------- Responses API ----------

    def _call_responses(self, messages: list[dict], max_tokens: int) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model,
            "input": [
                {"role": m["role"], "content": m["content"]}
                for m in messages
            ],
            # DeepSeek 关闭思考的正确参数：effort=none（OpenAI 的
            # reasoning.enabled 不被识别会被静默忽略，导致思考模式默认开启、
            # 思维链抢占输出预算、正文空输出——实测坑点，勿改回）
            "reasoning": {"effort": "none"},
            "max_output_tokens": max_tokens,
        }
        resp = self._post("/responses", body)
        self._raise_for_status(resp)
        data = resp.json()
        parts: list[str] = []
        finish = None
        model = data.get("model")
        reasoning_tokens = 0
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for content in item.get("content", []) or []:
                    if content.get("type") == "output_text":
                        parts.append(content.get("text", ""))
            elif item.get("type") == "reasoning":
                reasoning_tokens += len(item.get("summary", []) or [])
        status = data.get("status")
        if status == "incomplete":
            if not parts:
                # 空输出 + incomplete：视为接口异常（实测偶发），auto 模式回退 Chat
                raise LLMError("Responses 返回空（incomplete 且无输出）",
                               retryable=False)
            finish = "length"
        elif status == "failed":
            raise LLMError(f"Responses 调用失败: {data.get('error') or data}")
        return LLMResult(
            "\n".join(parts), finish_reason=finish, model=model, mode="responses",
            reasoning_tokens=reasoning_tokens,
        )

    # ---------- Chat Completions ----------

    def _call_chat(self, messages: list[dict], max_tokens: int,
                   json_mode: bool) -> LLMResult:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            # DeepSeek 关闭思考的正确参数（OpenAI 格式 extra_body 展开到顶层）
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = self._post("/chat/completions", body)
        self._raise_for_status(resp)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        return LLMResult(
            message.get("content") or "",
            finish_reason=finish,
            model=data.get("model"),
            mode="chat",
        )

    # ---------- 对外 ----------

    def complete(self, messages: list[dict], *, max_tokens: int | None = None,
                 json_mode: bool = True) -> LLMResult:
        """一次完整对话请求。auto 模式下 Responses 失败自动回退 Chat Completions。"""
        limit = max_tokens or self.max_tokens
        mode = self.detected_mode or self.mode
        if mode in ("auto", "responses"):
            try:
                result = self._call_responses(messages, limit)
                self.detected_mode = "responses"
                return result
            except LLMError as exc:
                if mode == "responses":
                    raise
                if exc.retryable:
                    # 传输/限流类错误不应回退协议，直接抛出让上层重试
                    raise
                self.detected_mode = "chat"  # 接口不兼容 → 回退
        return self._call_chat(messages, limit, json_mode=json_mode)

    def test_connection(self) -> dict:
        """设置页“测试模型”：探测可用模式并返回摘要。"""
        result = self.complete(
            [{"role": "user", "content": "只回复两个字：正常"}],
            max_tokens=32, json_mode=False,
        )
        return {
            "ok": True,
            "mode": result.mode,
            "model": result.model,
            "sample": result.text.strip()[:80],
        }
