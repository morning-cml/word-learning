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

    def _body(self, r: httpx.Response) -> dict[str, Any]:
        """把响应体读成 dict，读不出来就翻译成 ProviderError。

        这个类对外的约定是「任何失败都变成一个带上下文的 ProviderError」，
        设置页才有人话可显示。`r.json()` 是这条约定上唯一的漏洞：base_url
        指到了网页、代理或另一种协议上时，拿回来的常常是一个 200 的 HTML 页，
        抛出的 ValueError 不在任何调用方的捕获范围里，一路冒到接口层就是
        HTTP 500——而这恰好发生在四层检验、也就是专门用来诊断这类配置错误
        的那个页面上。
        """
        try:
            data = r.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.spec.base_url} 返回的不是 JSON（HTTP {r.status_code}）"
                "——检查 base_url 是不是指到了网页或代理上",
                status=r.status_code, body=r.text[:500],
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                f"{self.spec.base_url} 返回的 JSON 顶层不是对象",
                status=r.status_code, body=r.text[:500],
            )
        return data

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
        models = self._body(r).get("data") or []
        # 只收字符串 id。这个函数对外声明返回 list[str]，而调用方拿它去做
        # `model in ids`、`"、".join(ids[:5])`——后者撞上一个数字 id 抛的是
        # TypeError，不是 ProviderError，于是四层检验的 L1 会整个崩掉、
        # 后面三层一层都跑不到（和 _body 那条注释说的是同一件事：
        # 这个类对外的约定是「任何失败都变成一个带上下文的 ProviderError」）。
        # 非字符串的 id 本来也当不了模型名，留着没有用处。
        return [m["id"] for m in models
                if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]

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
        # 能力按**这次要用的模型**解析，不是按厂商。同一家的模型能力不一样，
        # 拿厂商的声明去发请求，差异会以一个含糊的 400 冒出来。
        spec = self.spec.for_model(model)
        payload: dict[str, Any] = {"model": model, "messages": list(messages)}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            # DeepSeek 文档特别警告：max_tokens 不足会让 JSON 中途截断
            payload["max_tokens"] = max_tokens

        if json_schema and spec.capabilities.json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        elif json_mode and spec.capabilities.json_object:
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

        data = self._body(r)
        choices = data.get("choices") or []
        text, finish = "", ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            text = (choices[0].get("message") or {}).get("content") or ""
            finish = choices[0].get("finish_reason") or ""

        usage = data.get("usage")
        return ChatResult(
            text=text if isinstance(text, str) else "",
            model=data.get("model") or model, ms=ms,
            usage=usage if isinstance(usage, dict) else {},
            raw=data, finish_reason=finish if isinstance(finish, str) else "",
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
