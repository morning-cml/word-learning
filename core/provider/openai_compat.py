"""OpenAI Chat Completions 兼容端点的统一实现。

DeepSeek 和 Kimi 都走这一个类，区别全在 ProviderSpec 的配置里。
再接第三家（智谱 / 通义 / OpenRouter / 本地 Ollama）同样不需要新代码。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

import httpx

from .base import ChatResult, ProviderError, ProviderSpec


class OpenAICompatProvider:
    def __init__(self, spec: ProviderSpec, api_key: str, *, timeout: float = 180.0):
        self.spec = spec
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    # ---------------------------------------------------------------- helpers

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.spec.base_url.rstrip('/')}/{path.lstrip('/')}"

    # ------------------------------------------------------------------- API

    def list_models(self) -> list[str]:
        if not self.spec.capabilities.models_endpoint:
            return [m.id for m in self.spec.models]
        try:
            with httpx.Client(timeout=30.0) as client:
                r = client.get(self._url("models"), headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"无法连接 {self.spec.base_url}：{exc}") from exc
        if r.status_code >= 400:
            raise ProviderError(
                self._explain(r.status_code), status=r.status_code, body=r.text[:500]
            )
        data = r.json().get("data") or []
        return [m.get("id", "") for m in data if m.get("id")]

    def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
        **options: Any,
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": list(messages)}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            # DeepSeek 文档特别警告：max_tokens 不足会让 JSON 中途截断
            payload["max_tokens"] = max_tokens

        if json_schema and self.spec.capabilities.json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        elif json_mode and self.spec.capabilities.json_object:
            payload["response_format"] = {"type": "json_object"}

        # Kimi 的 thinking 之类参数在官方 SDK 里必须走 extra_body，但 extra_body
        # 本来就是「原样合并进请求 body」——我们直接发 HTTP，所以平铺即可，
        # 不需要再为它单开一层。
        payload.update(options)

        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(self._url("chat/completions"), headers=self._headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"请求失败：{exc}") from exc
        ms = int((time.perf_counter() - started) * 1000)

        if r.status_code >= 400:
            raise ProviderError(
                self._explain(r.status_code), status=r.status_code, body=r.text[:800]
            )

        data = r.json()
        choices = data.get("choices") or []
        text, finish = "", ""
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
            finish = choices[0].get("finish_reason") or ""

        return ChatResult(
            text=text, model=data.get("model", model), ms=ms,
            usage=data.get("usage") or {}, raw=data, finish_reason=finish,
        )

    # --------------------------------------------------------------- 错误翻译

    @staticmethod
    def _explain(status: int) -> str:
        return {
            400: "请求格式错误（400）——可能是该模型不支持某个参数",
            401: "API Key 无效或未授权（401）",
            402: "余额不足（402）",
            403: "无权访问该模型（403）",
            404: "接口或模型不存在（404）——检查 base_url 与模型名是否已下线",
            422: "参数校验失败（422）",
            429: "触发限流（429）——稍后重试或降低并发",
            500: "服务端错误（500）",
            503: "服务暂时不可用（503）",
        }.get(status, f"HTTP {status}")
