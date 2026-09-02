"""从 config/providers.yaml 加载提供商注册表。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .base import Capabilities, ModelSpec, ProviderSpec, Quirks, Reasoning
from .openai_compat import OpenAICompatProvider

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "providers.yaml"


def _merge(base: dict, over: dict) -> dict:
    """模型级声明覆盖厂商级，只覆盖写出来的那几个键。

    浅合并就够：capabilities / quirks / reasoning 三段都是一层扁平的键值，
    唯一的例外是 reasoning.disable（一个 dict），而它整段替换才有意义——
    「关思考要传哪些参数」是一整套，混着两家的写法拼出来的东西谁也没验过。
    """
    return {**base, **over}


def _spec_from_dict(pid: str, raw: dict, *, _model_overrides: bool = True) -> ProviderSpec:
    caps = raw.get("capabilities") or {}
    quirks = raw.get("quirks") or {}
    reasoning = raw.get("reasoning") or {}

    per_model: dict[str, ProviderSpec] = {}
    if _model_overrides:
        for m in raw.get("models") or []:
            over = {k: m[k] for k in ("capabilities", "quirks", "reasoning", "temperatures")
                    if k in m}
            if not over:
                continue
            merged = dict(raw)
            for key in over:
                merged[key] = _merge(raw.get(key) or {}, over[key] or {})
            # 变体自己不再带 per_model：解析一层就够，避免 for_model 递归下去
            per_model[m["id"]] = _spec_from_dict(pid, merged, _model_overrides=False)

    return ProviderSpec(
        id=pid,
        label=raw.get("label", pid),
        base_url=raw.get("base_url", ""),
        models=tuple(
            ModelSpec(id=m["id"], label=m.get("label", "")) for m in (raw.get("models") or [])
        ),
        capabilities=Capabilities(
            json_object=caps.get("json_object", True),
            json_schema=caps.get("json_schema", False),
            models_endpoint=caps.get("models_endpoint", True),
        ),
        quirks=Quirks(
            json_needs_keyword=quirks.get("json_needs_keyword", False),
            may_return_empty=quirks.get("may_return_empty", False),
            sampling_ignored_when_thinking=quirks.get("sampling_ignored_when_thinking", False),
        ),
        reasoning=Reasoning(
            counts_toward_max_tokens=reasoning.get("counts_toward_max_tokens", False),
            headroom=int(reasoning.get("headroom") or 0),
            disable=dict(reasoning.get("disable") or {}),
            disable_for=tuple(reasoning.get("disable_for") or ()),
        ),
        temperatures=dict(raw.get("temperatures") or {}),
        key_url=raw.get("key_url", ""),
        docs=raw.get("docs", ""),
        per_model=per_model,
    )


@lru_cache(maxsize=1)
def load_specs() -> dict[str, ProviderSpec]:
    if not CONFIG_PATH.exists():
        return {}
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {pid: _spec_from_dict(pid, cfg) for pid, cfg in (raw.get("providers") or {}).items()}


def get_spec(provider_id: str) -> ProviderSpec:
    specs = load_specs()
    if provider_id not in specs:
        raise KeyError(f"未知的模型提供商：{provider_id}")
    return specs[provider_id]


def build(provider_id: str, api_key: str, **kwargs) -> OpenAICompatProvider:
    return OpenAICompatProvider(get_spec(provider_id), api_key, **kwargs)
