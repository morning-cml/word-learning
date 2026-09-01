"""模型提供商的统一抽象。

设计要点：不假设「OpenAI 兼容」就等于「完全一致」。每家的偏差用
Capabilities / Quirks 显式声明出来，调用方按声明分支，而不是靠试错。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProviderError(RuntimeError):
    """带上下文的调用失败，便于在设置页里显示人话。"""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str = ""


@dataclass(frozen=True)
class Capabilities:
    json_object: bool = True        # 支持 response_format={"type":"json_object"}
    json_schema: bool = False       # 支持 Structured Output（json_schema）
    models_endpoint: bool = True    # 支持 GET /models


@dataclass(frozen=True)
class Quirks:
    # DeepSeek：prompt 里必须出现 "json" 字样并给出样例，否则 JSON 模式不生效
    json_needs_keyword: bool = False
    # DeepSeek：官方承认偶尔返回空 content
    may_return_empty: bool = False
    # DeepSeek：思考模式下采样参数静默失效（设了不报错也不生效）
    sampling_ignored_when_thinking: bool = False


@dataclass(frozen=True)
class Reasoning:
    """推理模型的处理方式。

    坑在于：max_tokens 通常把思考 token 一起算。DeepSeek V4 实测同一个 prompt
    的思考量能从 552 跳到 2633——预算给不足，思考就把正文挤成空字符串，
    表现出来就是官方文档里那句「偶尔返回空 content」。
    """

    counts_toward_max_tokens: bool = False
    headroom: int = 0                       # 在正文需求之上额外留给思考的预算
    disable: dict = field(default_factory=dict)      # 关掉思考要传的参数
    disable_for: tuple[str, ...] = ()                # 这些用途关掉思考

    def budget(self, want: int) -> int:
        return want + self.headroom if self.counts_toward_max_tokens else want

    def params_for(self, purpose: str) -> dict:
        return dict(self.disable) if purpose in self.disable_for else {}


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    base_url: str
    models: tuple[ModelSpec, ...] = ()
    capabilities: Capabilities = field(default_factory=Capabilities)
    quirks: Quirks = field(default_factory=Quirks)
    reasoning: Reasoning = field(default_factory=Reasoning)
    temperatures: dict[str, float] = field(default_factory=dict)
    key_url: str = ""
    docs: str = ""

    def temperature_for(self, purpose: str, fallback: float = 1.0) -> float:
        return float(self.temperatures.get(purpose, fallback))

    def default_model(self) -> str:
        return self.models[0].id if self.models else ""


@dataclass
class ChatResult:
    text: str
    model: str
    ms: int
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or 0)

    @property
    def reasoning_tokens(self) -> int:
        details = self.usage.get("completion_tokens_details") or {}
        return int(details.get("reasoning_tokens") or 0)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    def diagnose(self) -> str:
        """空响应 / 截断时给出人能看懂的原因，而不是只说「失败」。"""
        bits = []
        if self.truncated:
            bits.append("输出被 max_tokens 截断")
        if self.reasoning_tokens:
            content = int(self.usage.get("completion_tokens") or 0) - self.reasoning_tokens
            bits.append(f"思考用了 {self.reasoning_tokens} tokens，只剩 {content} 给正文")
        return "；".join(bits)
