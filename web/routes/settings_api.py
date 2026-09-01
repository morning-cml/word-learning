"""设置相关接口：模型切换、Key 管理、三层正确性检验。"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool

from core import health, settings
from core.provider import registry
from core.provider.base import ProviderError

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def read_settings() -> dict:
    return settings.public_view()


@router.post("")
def write_settings(patch: dict = Body(...)) -> dict:
    if "active_provider" in patch and patch["active_provider"]:
        try:
            registry.get_spec(patch["active_provider"])
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from exc
    settings.save(patch)
    return settings.public_view()


@router.get("/models/{provider_id}")
async def live_models(provider_id: str) -> dict:
    """从服务端实时拉模型列表。

    配置里的列表随时可能过期（kimi-k2.5 和 moonshot-v1 系列已于
    2026-08-31 下线），能实时拉就以实时为准。
    """
    try:
        spec = registry.get_spec(provider_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    key = settings.api_key(provider_id)
    configured = [{"id": m.id, "label": m.label} for m in spec.models]
    if not key:
        return {"models": configured, "live": False, "note": "未填 Key，显示的是配置里的列表"}

    try:
        provider = registry.build(provider_id, key)
        ids = await run_in_threadpool(provider.list_models)
    except ProviderError as exc:
        return {"models": configured, "live": False, "note": f"实时拉取失败：{exc}"}

    labels = {m.id: m.label for m in spec.models}
    return {
        "models": [{"id": i, "label": labels.get(i, "")} for i in ids],
        "live": True,
        "note": f"来自 {spec.base_url} 的实时列表",
    }


@router.post("/check")
async def run_check(payload: dict = Body(default={})) -> dict:
    """跑三层检验。不传参就检当前生效的配置。"""
    provider_id = payload.get("provider") or settings.active()[0]
    model = payload.get("model") or settings.active(provider_id)[1]
    key = (payload.get("api_key") or "").strip() or settings.api_key(provider_id)

    try:
        registry.get_spec(provider_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc

    return await run_in_threadpool(health.check, provider_id, model, key)
