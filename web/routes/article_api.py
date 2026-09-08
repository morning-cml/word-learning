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

from fastapi import APIRouter, Body, HTTPException, Response
from fastapi.responses import StreamingResponse

import tasks
from core import settings
from core.lexicon import cefr
from core.lexicon.lemma import WORD_RE
from core.llm.client import LLM
from core.provider import registry
from core.provider.base import ProviderError
from core.store import db, export
from core.store.models import STATUS_IGNORED, STATUS_LABELS, as_utc
from tasks.article.task import (
    MAX_PARAGRAPHS,
    MAX_WORDS,
    WORDS_PER_PARAGRAPH,
    estimated_words,
    sizing,
)

router = APIRouter(prefix="/api", tags=["article"])

_SENTINEL = object()


def _parse_words(raw: Any) -> list[str]:
    """接受数组，或用换行 / 逗号 / 空格分隔的一整段文本。

    判据是「含 ASCII 字母」，不是 `str.isalpha()`：**汉字的 isalpha() 也是 True**。
    人贴进来的词表十有八九是「abandon 抛弃」这种词 + 释义的形状，按 isalpha()
    收的话「抛弃」也算一个目标词——然后整条管线拿它当英文词跑：选题要给它安排
    段落，check_paragraph 逐段找它、找不到就判 missing_target，两次修复预算全烧
    在一个不可能出现的词上，最后结果面板报「目标词命中 2/4」。
    钱和几分钟都花掉了，文章还被稀释了一遍。

    `WORD_RE` 是这个项目对「一个英文词形」的既定判据（core/lexicon/lemma.py），
    背单词那边也踩过同一个坑（dictionary._is_english）——同一件事没有理由
    在这里再立第三套（需要注意.md 第 20 条）。
    """
    if isinstance(raw, list):
        items = [str(x) for x in raw]
    else:
        items = str(raw or "").replace(",", "\n").replace("，", "\n").split("\n")
    out, seen = [], set()
    for item in items:
        for token in item.split():
            w = token.strip().strip(".,;:!?\"'()[]{}").lower()
            if w and w not in seen and WORD_RE.search(w):
                seen.add(w)
                out.append(w)
    return out


@router.get("/timing")
def timing(provider: str = "", model: str = "") -> dict:
    """从历史文章估「一次模型调用大概多久」。

    首页要在刚开始生成时就给出预计剩余时间，可那一刻还没有任何本次运行的
    测量值。拿用户自己前几篇的实测当先验，比在代码里拍一个常数诚实得多——
    这个数在不同模型、不同网络下能差好几倍（本机历史里 7.6 秒到 61.5 秒都有）。

    取中位数不取平均：偶尔一次重试或一次超长思考会把平均值拉飞，
    而那恰恰是「这次特别慢」而不是「平时就这么慢」。
    """
    # 不传就按当前生效的配置。前端不必自己先去查一遍 /api/status，
    # 而「拿哪个模型的历史来估」这件事本来也该由服务端说了算。
    if not provider or not model:
        provider, model = settings.active(provider)

    def per_call(article) -> float | None:
        stats = article.stats or {}
        ms, calls = stats.get("ms"), stats.get("llm_calls")
        if not ms or not calls:
            return None                     # 老文章没存过用量
        return ms / calls / 1000

    with db.session() as s:
        rows = db.list_articles(s, limit=20)

    def collect(match) -> list[float]:
        out = [per_call(a) for a in rows if match(a)]
        return sorted(v for v in out if v)

    # 先看同一个模型；没有就退回全部。不同模型的速度差得远，
    # 但「有个粗略的先验」仍然远好过「什么都不说」。
    same = collect(lambda a: a.provider == provider and a.model == model)
    scope = "model"
    if not same:
        same, scope = collect(lambda a: True), "all"
    if not same:
        return {"samples": 0, "sec_per_call": None, "scope": "none"}
    mid = len(same) // 2
    median = same[mid] if len(same) % 2 else (same[mid - 1] + same[mid]) / 2
    return {"samples": len(same), "sec_per_call": round(median, 1), "scope": scope}


@router.get("/levels")
def levels() -> dict:
    """用词上限各档对应多大的词汇量。

    首页那个下拉框原先是四个没有含义的字母。数字现算而不是写死在模板里：
    没下载 CEFR-J 时标尺会退回内置兜底表，写死的数字就会和程序实际拦的东西
    对不上，而这种不一致用户没法自己发现。
    """
    return {
        "using_real_data": cefr.is_real_data(),
        "cumulative": cefr.level_counts(),
    }


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
        "estimated_words": estimated_words(n_para, n_sent),
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
        # 在这里也收敛一次：入库 meta 里记的那一档必须和管线实际执行的那一档相同
        level = cefr.normalize_level(payload.get("level") or cfg.get("level"))
        key = settings.api_key(provider_id)
        if not key:
            raise ValueError(f"{provider_id} 还没有配置 API Key，请先去设置页填上")

        llm = LLM(
            provider=registry.build(provider_id, key),
            model=model,
            on_event=lambda kind, data: out.put({"type": kind, **data}),
        )
        task = tasks.get("article")

        # 词库里已有的词不该被难度标尺判成超纲——那些词用户自己挑来学过，
        # 有直接证据，不必再拿 CEFR 等级去猜。在这里查、当参数传进去，
        # 而不是让任务层自己开 db.session()：管线的测试大多不带 temp_db，
        # 任务层碰库会让它们去读用户真正的那个库（需要注意.md 第 17c 条）。
        with db.session() as s:
            studied = db.studied_lemmas(s)

        document, stats = None, {}
        for event in task.run(llm, {"words": words, "level": level, "studied": studied}):
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
                    # 同 article_to_doc：不带偏移量的 ISO 串会被 new Date() 当本地时间
                    "created_at": as_utc(a.created_at).isoformat() if a.created_at else "",
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


@router.get("/articles/{article_id}/impact")
def deletion_impact(article_id: int) -> dict:
    """删这篇会连带丢掉什么。确认删除之前拿它把代价摆出来。

    文章能重新生成，累计语境不能——而那个「删除」按钮从来没说过它会
    连着语境一起删。`orphaned` 里的词只在这一篇里出现过，删了就等于从没学过。
    """
    with db.session() as s:
        impact = db.deletion_impact(s, article_id)
        if impact is None:
            raise HTTPException(404, "文章不存在")
        return impact


@router.delete("/articles/{article_id}")
def delete_article(article_id: int) -> dict:
    with db.session() as s:
        if not db.delete_article(s, article_id):
            raise HTTPException(404, "文章不存在")
    # 删除前那次留档到底成没成，要如实说。悄悄失败比没有备份更糟：
    # 用户以为自己还有退路，等到需要它那天才发现没有。
    snap = db.last_delete_backup()
    return {"ok": True, "backup": {"made": bool(snap.get("made")),
                                   "name": snap.get("latest", ""),
                                   "error": "" if snap.get("ok", True) else snap.get("error", "")}}


@router.get("/reading/history")
def reading_history(days: int = 90) -> dict:
    """按天的阅读量 + 连续天数。文库页拿它画那条走势。

    数据全部来自已经入库的东西（Article.created_at + stats.word_count），
    零 schema 改动——这个应用一直在攒这份东西，只是从来没显示过。
    """
    with db.session() as s:
        return db.reading_history(s, days=max(1, min(days, 365)))


@router.get("/words/export.csv")
def export_words() -> Response:
    """把整个词库导成 CSV：一行一处语境。

    路由排在 `/words/{lemma}` **前面**——FastAPI 按注册顺序匹配，反过来的话
    `export.csv` 会被当成一个 lemma 吃掉，回一个 404「词条 export.csv 不存在」。
    这种错不会在别处冒出来，只有访问导出时才现形。
    """
    with db.session() as s:
        body = export.words_csv(s)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="words.csv"'},
    )


@router.get("/words/{lemma}")
def word_detail(lemma: str) -> dict:
    """一个词跨所有文章的全部语境——词条面板的数据源。"""
    with db.session() as s:
        detail = db.word_detail(s, lemma)
        if detail is None:
            raise HTTPException(404, f"词条 {lemma} 不存在")
        return detail


@router.post("/words")
def add_word(payload: dict = Body(...)) -> dict:
    """把一个不是目标词的词收进词库，默认标成「忽略」。

    这是「难度标尺误判了，我来纠正它」那条回路的入口：标尺在结果面板上
    报出超纲词，用户点一下说「这个不用管」，以后就不再判它超纲。
    在这之前 Word 行只有 save_article 一条来路，所以这条回路根本走不通。
    """
    try:
        status = int(payload.get("status", STATUS_IGNORED))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "status 必须是整数") from exc
    if status not in STATUS_LABELS:
        raise HTTPException(400, f"status 必须是 {sorted(STATUS_LABELS)} 之一")

    with db.session() as s:
        got = db.add_word(s, str(payload.get("lemma") or ""), status)
    if got is None:
        raise HTTPException(400, "这不是一个能收进词库的词")
    return got


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
