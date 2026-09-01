"""本地设置读写（含 API Key）。

Key 只落在 config/settings.local.json，已在 .gitignore 里排除，
也绝不下发到前端——接口返回的永远是掩码串。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.provider import registry

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.local.json"

DEFAULTS: dict[str, Any] = {
    "active_provider": "deepseek",
    "active_model": "",
    "level": "B2",
    "keys": {},          # provider_id -> api key
}


def _read() -> dict[str, Any]:
    data = dict(DEFAULTS)
    data["keys"] = {}
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                data.update(stored)
                data["keys"] = dict(stored.get("keys") or {})
        except (json.JSONDecodeError, OSError):
            pass
    return data


def _write(data: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load() -> dict[str, Any]:
    return _read()


def save(patch: dict[str, Any]) -> dict[str, Any]:
    data = _read()
    for key in ("active_provider", "active_model", "level"):
        if key in patch and patch[key] is not None:
            data[key] = patch[key]
    keys = patch.get("keys")
    for pid, val in (keys if isinstance(keys, dict) else {}).items():
        val = (val or "").strip()
        if val == "":
            data["keys"].pop(pid, None)      # 传空字符串 = 删除该 key
        elif "*" not in val:
            # 掩码串原样回传时不覆盖。判据是「含 *」而不是「以 * 开头」——
            # mask() 产出的是 sk-ab********wxyz，开头恰恰不是 *，
            # 按开头判等于这道防线在它唯一该起作用的时候不起作用。
            # 真 key 里不会有 *，误伤不了。
            data["keys"][pid] = val
    _write(data)
    return data


def api_key(provider_id: str) -> str:
    """取 Key：优先本地设置，其次环境变量。"""
    key = (_read().get("keys") or {}).get(provider_id, "")
    if key:
        return key
    env = {"deepseek": "DEEPSEEK_API_KEY", "kimi": "MOONSHOT_API_KEY"}.get(provider_id)
    return os.environ.get(env, "") if env else ""


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:5]}{'*' * 8}{key[-4:]}"


def active(provider_id: str = "") -> tuple[str, str]:
    """当前 (provider_id, model)，model 为空时取该家的第一个模型。

    传 provider_id = 「按这一家解析」。已保存的 active_model 只对已保存的
    那一家有效——调用方指定了别家时还拿它当默认值，就会把 A 家的模型名
    发给 B 家的端点，报回来的是一个「模型不存在」，很难看出根因在这。
    """
    data = _read()
    saved = data.get("active_provider") or "deepseek"
    pid = provider_id or saved
    model = (data.get("active_model") or "") if pid == saved else ""
    if not model:
        try:
            model = registry.get_spec(pid).default_model()
        except KeyError:
            model = ""
    return pid, model


def public_view() -> dict[str, Any]:
    """给前端的设置快照——Key 一律掩码。"""
    data = _read()
    specs = registry.load_specs()
    return {
        "active_provider": data.get("active_provider"),
        "active_model": data.get("active_model"),
        "level": data.get("level", "B2"),
        "providers": [
            {
                "id": pid,
                "label": spec.label,
                "base_url": spec.base_url,
                "key_url": spec.key_url,
                "docs": spec.docs,
                "models": [{"id": m.id, "label": m.label} for m in spec.models],
                "capabilities": {
                    "json_object": spec.capabilities.json_object,
                    "json_schema": spec.capabilities.json_schema,
                    "models_endpoint": spec.capabilities.models_endpoint,
                },
                "has_key": bool(api_key(pid)),
                "masked_key": mask(api_key(pid)),
            }
            for pid, spec in specs.items()
        ],
    }
