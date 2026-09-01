"""文章生成与读取接口。

生成走 SSE：一篇 400 词的文章要跑 5-8 次模型调用，几十秒起步。
盲转圈的体验很差，所以把选题结果、每段完成、每次修复都实时推给前端。
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, Iterator

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

import tasks
from core import settings
from core.llm.client import LLM
from core.provider import registry
from core.provider.base import ProviderError
from core.store import db
from core.store.models import STATUS_LABELS
from tasks.article.task import MAX_PARAGRAPHS, MAX_WORDS, WORDS_PER_PARAGRAPH, sizing

router = APIRouter(prefix="/api", tags=["article"])

_SENTINEL = object()


def _parse_words(raw: Any) -> list[str]:
    """接受数组，或用换行 / 逗号 / 空格分隔的一整段文本。"""
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").replace(",", "\n").replace("，", "\n").split("\n")
    out, seen = [], set()
    for item in items:
        for token in item.split():
            w = token.strip().strip(".,;:!?\"'()[]{}").lower()
            if w and w not in seen and any(c.isalpha() for c in w):
                seen.add(w)
                out.append(w)
    return out


@router.post("/article/plan-preview")
def plan_preview(payload: dict = Body(...)) -> dict:
    """还没调模型就先告诉用户篇幅会是多少、词是不是塞太多了。"""
    words = _parse_words(payload.get("words"))
    n_para, per_para, n_sent = sizing(len(words))
    capacity = MAX_PARAGRAPHS * WORDS_PER_PARAGRAPH
    warning = ""
    if len(words) > capacity:
        n_extra = -(-len(words) // capacity)
        warning = (
            f"{len(words)} 个词超出一篇文章的舒适容量（约 {capacity} 个）。"
            f"硬塞会让文章退化成填空作业，反而破坏语境记忆——"
            f"建议分成 {n_extra} 批分别生成。"
        )
    return {
        "words": words,
        "count": len(words),
        "paragraphs": n_para,
        "per_paragraph": per_para,
        "sentences_per_paragraph": n_sent,
        "estimated_words": n_para * n_sent * 17,
        "warning": warning,
    }


def _generate(payload: dict, out: queue.Queue, cancel: threading.Event) -> None:
    """在工作线程里跑生成，把事件塞进队列。

    cancel 由 SSE 流的收尾逻辑置位——用户点「停止」或者直接关掉页面，
    两种情况都会走到那里。取消在两次管线事件之间生效：正在飞的那次模型调用
    没法从外面掐断，但只要它一返回就停，而且半篇文章不落库。
    """
    try:
        words = _parse_words(payload.get("words"))
        if not words:
            raise ValueError("没有识别到任何单词")
        if len(words) > MAX_WORDS:
            raise ValueError(f"一次最多 {MAX_WORDS} 个词，收到 {len(words)} 个")

        cfg = settings.load()
        provider_id = payload.get("provider") or cfg.get("active_provider") or "deepseek"
        model = payload.get("model") or settings.active(provider_id)[1]
        level = payload.get("level") or cfg.get("level") or "B2"
        key = settings.api_key(provider_id)
        if not key:
            raise ValueError(f"{provider_id} 还没有配置 API Key，请先去设置页填上")

        llm = LLM(
            provider=registry.build(provider_id, key),
            model=model,
            on_event=lambda kind, data: out.put({"type": kind, **data}),
        )
        task = tasks.get("article")

        document, stats = None, {}
        for event in task.run(llm, {"words": words, "level": level}):
            if cancel.is_set():
                out.put({"type": "cancelled"})
                return                      # 半篇文章不落库
            if event.get("type") == "done":
                document, stats = event["document"], event.get("stats", {})
            out.put(event)

        if document:
            with db.session() as s:
                article = db.save_article(
                    s, document,
                    {"level": level, "provider": provider_id, "model": model,
                     "target_words": words, "stats": stats,
                     "glossary": document.get("glossary") or {}},
                )
                out.put({"type": "saved", "article_id": article.id})
    except (ProviderError, ValueError, KeyError) as exc:
        out.put({"type": "error", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001  兜底，避免前端一直转圈
        out.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        out.put(_SENTINEL)


@router.post("/article/generate")
async def generate(payload: dict = Body(...)) -> StreamingResponse:
    out: queue.Queue = queue.Queue()
    cancel = threading.Event()
    threading.Thread(target=_generate, args=(payload, out, cancel), daemon=True).start()

    async def stream() -> Any:
        loop = asyncio.get_running_loop()
        try:
            while True:
                event = await loop.run_in_executor(None, out.get)
                if event is _SENTINEL:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 前端 abort 了 fetch，或者用户直接关掉页面——两种都走到这里。
            # 不置位的话工作线程会把剩下几次模型调用跑完再落库：
            # 用户以为停了，其实还在烧 token，最后还多出一篇没人要的文章。
            cancel.set()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _clue_ratio(stats: dict) -> str:
    """把 clue_strength 压成 "3/5" 这样一眼能比的形式。老文章没有这个字段。"""
    cs = stats.get("clue_strength") or {}
    total = sum(cs.values())
    return f"{cs.get('strong', 0)}/{total}" if total else ""


@router.get("/articles")
def list_articles() -> dict:
    with db.session() as s:
        rows = db.list_articles(s)
        return {
            "articles": [
                {
                    "id": a.id,
                    "title_en": a.title_en,
                    "title_zh": a.title_zh,
                    "genre": a.genre,
                    "level": a.level,
                    "model": f"{a.provider}/{a.model}",
                    "target_words": a.target_words or [],
                    "word_count": (a.stats or {}).get("word_count", 0),
                    # 线索强度是这个应用的头号指标，文库列表里也该一眼看得到
                    "clue": _clue_ratio(a.stats or {}),
                    "created_at": a.created_at.isoformat() if a.created_at else "",
                }
                for a in rows
            ]
        }


@router.get("/articles/{article_id}")
def read_article(article_id: int) -> dict:
    with db.session() as s:
        article = db.get_article(s, article_id)
        if article is None:
            raise HTTPException(404, "文章不存在")
        return db.article_to_doc(article)


@router.delete("/articles/{article_id}")
def delete_article(article_id: int) -> dict:
    with db.session() as s:
        if not db.delete_article(s, article_id):
            raise HTTPException(404, "文章不存在")
    return {"ok": True}


@router.get("/words/{lemma}")
def word_detail(lemma: str) -> dict:
    """一个词跨所有文章的全部语境——词条面板的数据源。"""
    with db.session() as s:
        detail = db.word_detail(s, lemma)
        if detail is None:
            raise HTTPException(404, f"词条 {lemma} 不存在")
        return detail


@router.post("/words/{lemma}/status")
def update_status(lemma: str, payload: dict = Body(...)) -> dict:
    try:
        status = int(payload.get("status"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "status 必须是整数") from exc
    if status not in STATUS_LABELS:
        raise HTTPException(400, f"status 必须是 {sorted(STATUS_LABELS)} 之一")
    with db.session() as s:
        result = db.set_word_status(s, lemma, status)
        if result is None:
            raise HTTPException(404, f"词条 {lemma} 不存在")
        return result


@router.get("/words")
def list_words() -> dict:
    from sqlalchemy import select

    from core.store.models import Word

    with db.session() as s:
        rows = list(s.scalars(select(Word).order_by(Word.last_seen_at.desc().nullslast())))
        return {
            "stats": db.word_stats(s),
            "words": [
                {
                    "lemma": w.lemma,
                    "cefr": w.cefr,
                    "gloss": w.gloss,
                    "status": w.status,
                    "status_label": w.status_label,
                    "times_seen": w.times_seen or 0,
                    "forms": [f.surface for f in w.forms],
                }
                for w in rows
            ],
        }
