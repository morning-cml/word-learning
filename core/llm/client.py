"""在 Provider 之上加一层：重试、空响应处理、JSON 解析。

任务层只跟这里打交道，不直接碰 HTTP。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from core.llm import jsonfix
from core.provider.base import ChatResult, ProviderError
from core.provider.openai_compat import OpenAICompatProvider

# 提醒模型必须输出 json —— DeepSeek 文档明确要求 prompt 中出现 "json" 字样
JSON_KEYWORD_HINT = (
    "\n\n严格只输出一个 json 对象，不要任何解释文字，不要 markdown 代码块围栏。"
)


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    ms: int = 0

    def add(self, res: ChatResult) -> None:
        self.calls += 1
        self.ms += res.ms
        u = res.usage or {}
        self.prompt_tokens += int(u.get("prompt_tokens") or 0)
        self.completion_tokens += int(u.get("completion_tokens") or 0)
        self.total_tokens += int(u.get("total_tokens") or 0)


@dataclass
class LLM:
    provider: OpenAICompatProvider
    model: str
    usage: Usage = field(default_factory=Usage)
    max_retries: int = 3
    on_event: Callable[[str, dict], None] | None = None

    # -------------------------------------------------------------- internals

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event(kind, data)

    @property
    def spec(self):
        """按当前模型解析出来的那份声明。

        能力 / quirks / 思考预算都可能因模型而异（见 ProviderSpec.for_model），
        所以这一层一律走它，不直接摸 provider.spec。
        """
        return self.provider.spec.for_model(self.model)

    def temperature(self, purpose: str) -> float:
        return self.spec.temperature_for(purpose)

    # ------------------------------------------------------------------- API

    def chat(self, messages: list[dict], *, purpose: str = "structured", **kw) -> ChatResult:
        spec = self.spec
        kw.setdefault("temperature", self.temperature(purpose))
        # 推理模型：max_tokens 含思考 token，必须额外留出思考预算，
        # 否则思考一多就把正文挤成空字符串。
        if kw.get("max_tokens"):
            kw["max_tokens"] = spec.reasoning.budget(kw["max_tokens"])
        for key, val in spec.reasoning.params_for(purpose).items():
            kw.setdefault(key, val)
        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            # 每一次真正发出去的请求都报一声。前端拿它算进度和剩余时间：
            # 模型调用是这条管线里唯一实质耗时的东西（一次 30-40 秒），
            # 而「跑到第几步」在别处都数不准——修复和补线索是条件触发的，
            # 光看阶段事件推不出来实际发了几次请求。重试也要算，它一样在花时间。
            self._emit("call", purpose=purpose, attempt=attempt)
            try:
                res = self.provider.chat(messages, model=self.model, **kw)
            except ProviderError as exc:
                last = exc
                # 4xx 里只有限流值得重试，其余重试也是白费
                if exc.status and exc.status < 500 and exc.status != 429:
                    raise
                self._emit("retry", attempt=attempt, reason=str(exc))
                time.sleep(min(2**attempt, 8))
                continue
            self.usage.add(res)
            return res
        raise last or ProviderError("重试耗尽")

    def json(
        self,
        messages: list[dict],
        *,
        purpose: str = "structured",
        max_tokens: int = 4096,
        json_schema: dict | None = None,
        **kw,
    ) -> Any:
        """要求模型返回 JSON，并保证解析成功或抛出可读错误。"""
        quirks = self.spec.quirks
        msgs = [dict(m) for m in messages]
        if quirks.json_needs_keyword and msgs:
            msgs[-1]["content"] = msgs[-1]["content"] + JSON_KEYWORD_HINT

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            res = self.chat(
                msgs, purpose=purpose, json_mode=True,
                json_schema=json_schema, max_tokens=max_tokens, **kw,
            )
            if not res.text.strip():
                # 空响应对不同家的含义不一样：声明了 may_return_empty 的（DeepSeek）
                # 是已知抖动，重试基本能过；没声明的家里出现空响应说明别处不对劲，
                # 别让用户对着同一句「模型返回空内容」猜是哪种。
                why = res.diagnose() or (
                    "这家模型官方承认偶发空响应，重试通常能过"
                    if quirks.may_return_empty else "模型返回了空内容"
                )
                last_err = jsonfix.JsonParseError(f"模型返回空内容（{why}）")
                self._emit("retry", attempt=attempt, reason=f"空响应：{why}")
                continue
            if res.truncated:
                self._emit("retry", attempt=attempt, reason="输出被 max_tokens 截断，尝试救回")
            try:
                return jsonfix.loads(res.text)
            except jsonfix.JsonParseError as exc:
                last_err = exc
                self._emit("retry", attempt=attempt, reason="JSON 解析失败")
                msgs = msgs + [
                    {"role": "assistant", "content": res.text[:2000]},
                    {"role": "user", "content": "上面的输出不是合法 json。请只重新输出合法的 json 对象本身。"},
                ]
        raise last_err or jsonfix.JsonParseError("JSON 获取失败")
