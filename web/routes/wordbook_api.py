"""词书接口：录入、浏览、背诵。

路由按资源分层（books / units / entries），不是按页面上的动作分。
页面会改，资源不会——以后加测验、加导出、加遗忘曲线，都是在这几层下面
挂新的动作，而不是回来把已有的路由推倒重排。

删除走的是和删文章同一套保护（先留档、先把代价摆出来）：**这几张表装的是
用户一页页敲进去的顺序，删掉只能再敲一遍**，和累计语境是同一档资产。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Response

from core.store import backup, db, export
from core.wordbook import dictionary
from core.wordbook import store as wb

router = APIRouter(prefix="/api/wordbook", tags=["wordbook"])


@router.get("/status")
def status() -> dict:
    """词典就绪没有。缺了整页仍然能开，只是配不上释义——要说出来。"""
    return {
        "dictionary": {
            "ready": dictionary.is_available(),
            "size": dictionary.size(),
            "source": "ECDICT (MIT)",
        }
    }


@router.get("/preview")
def preview(text: str = "") -> dict:
    """只解析不落库：录入框里边打边告诉他认出了几个词、哪几个字典里没有。

    和首页那个 plan-preview 是同一个用意——**在花代价之前先看见结果**。
    这里的代价是「贴错了要一条条删」，比生成一篇文章便宜，但一样值得先看一眼。
    """
    words = dictionary.split_words(text)
    found, missing = dictionary.lookup_many(words)
    return {
        "count": len(words),
        "matched": len(found),
        "missing": missing,
        # 只回前 12 条：这是个输入提示，不是结果页
        "sample": [
            {"headword": e["surface"], "lemma": e["word"], "phonetic": e["phonetic"],
             "translation": e["translation"][:60], "fallback": e["lemma_fallback"]}
            for e in found[:12]
        ],
    }


# ------------------------------------------------------------------ 词书

@router.get("/books")
def list_books() -> dict:
    with db.session() as s:
        return {"books": wb.list_books(s)}


@router.post("/books")
def create_book(payload: dict = Body(...)) -> dict:
    with db.session() as s:
        got = wb.create_book(s, str(payload.get("name") or ""),
                             str(payload.get("note") or ""))
    if got is None:
        raise HTTPException(400, "词书得有个名字")
    return got


@router.patch("/books/{book_id}")
def update_book(book_id: int, payload: dict = Body(...)) -> dict:
    with db.session() as s:
        got = wb.update_book(s, book_id, name=payload.get("name"),
                             note=payload.get("note"))
    if got is None:
        raise HTTPException(404, "词书不存在")
    return got


@router.get("/books/{book_id}/impact")
def book_impact(book_id: int) -> dict:
    """删这本会丢掉什么。确认之前把代价摆出来。

    「N 个单元、M 个词」听着不大，但那是**一页页敲进去的**——
    删除按钮从来没说过这件事（需要注意.md 第 10e 条说的就是它）。
    """
    with db.session() as s:
        units = wb.list_units(s, book_id)
        if units is None:
            raise HTTPException(404, "词书不存在")
        return {
            "units": len(units),
            "entries": sum(u["total"] for u in units),
            "known": sum(u["known"] for u in units),
            "learning": sum(u["learning"] for u in units),
        }


@router.get("/books/{book_id}/export.csv")
def export_book(book_id: int) -> Response:
    """把一本词书导成 CSV，**严格按书上的顺序**。

    这是「和纸质版一致」这条承诺唯一能拿到手上校对的形式——在这之前只能
    在界面上一行一行翻。顺序在这里错了，用户会以为是自己录错了。
    """
    with db.session() as s:
        body = export.book_csv(s, book_id)
        name = export.book_filename(s, book_id)
    if body is None:
        raise HTTPException(404, "词书不存在")
    # 文件名里可能有中文，走 RFC 5987 的 filename*；同时留一个 ASCII 的
    # filename 兜底，老浏览器只认那个。
    ascii_name = quote(name)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="wordbook.csv"; filename*=UTF-8\'\'{ascii_name}'},
    )


@router.delete("/books/{book_id}")
def delete_book(book_id: int) -> dict:
    # 动手之前先留档，和 db.delete_article 同一条规矩：
    # **明知自己马上要动库的调用方必须自己传 force**
    snap = backup.run(db.DB_PATH, force=True, tag=backup.BEFORE_DELETE)
    with db.session() as s:
        if not wb.delete_book(s, book_id):
            raise HTTPException(404, "词书不存在")
    return {"ok": True, "backup": {"made": bool(snap.get("made")),
                                   "name": snap.get("latest", ""),
                                   "error": "" if snap.get("ok", True) else snap.get("error", "")}}


# ------------------------------------------------------------------ 单元

@router.get("/books/{book_id}/units")
def list_units(book_id: int) -> dict:
    with db.session() as s:
        units = wb.list_units(s, book_id)
    if units is None:
        raise HTTPException(404, "词书不存在")
    return {"units": units}


@router.post("/books/{book_id}/units")
def create_unit(book_id: int, payload: dict = Body(...)) -> dict:
    idx = payload.get("idx")
    with db.session() as s:
        got = wb.create_unit(s, book_id, str(payload.get("label") or ""),
                             int(idx) if str(idx or "").strip().isdigit() else None)
    if got is None:
        raise HTTPException(400, "单元得有个编号，比如 List 07")
    return got


@router.patch("/units/{unit_id}")
def update_unit(unit_id: int, payload: dict = Body(...)) -> dict:
    idx = payload.get("idx")
    with db.session() as s:
        got = wb.update_unit(s, unit_id, label=payload.get("label"),
                             idx=int(idx) if str(idx or "").strip().isdigit() else None,
                             note=payload.get("note"))
    if got is None:
        raise HTTPException(404, "单元不存在")
    return got


@router.delete("/units/{unit_id}")
def delete_unit(unit_id: int) -> dict:
    snap = backup.run(db.DB_PATH, force=True, tag=backup.BEFORE_DELETE)
    with db.session() as s:
        if not wb.delete_unit(s, unit_id):
            raise HTTPException(404, "单元不存在")
    return {"ok": True, "backup": {"made": bool(snap.get("made")),
                                   "name": snap.get("latest", "")}}


# ------------------------------------------------------------------ 词条

@router.get("/units/{unit_id}/entries")
def list_entries(unit_id: int, page: int | None = None) -> dict:
    with db.session() as s:
        rows = wb.list_entries(s, unit_id, page=page)
    if rows is None:
        raise HTTPException(404, "单元不存在")
    return {"entries": rows}


@router.post("/units/{unit_id}/entries")
def add_entries(unit_id: int, payload: dict = Body(...)) -> dict:
    """往单元里加一页词。

    收 text（整段粘贴）或 words（已经切好的数组）。两者都保**原样顺序**——
    这是整个功能唯一的卖点，任何一处重排都会把它破掉。
    """
    raw = payload.get("words")
    words = ([str(w) for w in raw] if isinstance(raw, list)
             else dictionary.split_words(str(payload.get("text") or "")))
    if not words:
        raise HTTPException(400, "没有识别到任何单词")
    page = payload.get("page")
    if not wb.page_is_valid(page):
        raise HTTPException(400, "页号只能是数字，留空表示这一页不写页号")
    with db.session() as s:
        got = wb.add_entries(
            s, unit_id, words,
            page=wb.page_value(page),
            replace_page=bool(payload.get("replace_page")),
        )
    if got is None:
        raise HTTPException(404, "单元不存在")
    return got


@router.patch("/entries/{entry_id}")
def update_entry(entry_id: int, payload: dict = Body(...)) -> dict:
    # 页号是词表视图里一个自由文本框，打错是常态。认不出来当场说出来，
    # 而不是悄悄存成「没有页号」——那样这一条会掉到单元末尾去，
    # 而用户以为自己刚把页号改对了。
    if "page" in payload and not wb.page_is_valid(payload["page"]):
        raise HTTPException(400, "页号只能是数字，留空表示不写页号")
    with db.session() as s:
        got = wb.update_entry(s, entry_id, payload)
    if got is None:
        raise HTTPException(404, "词条不存在")
    return got


@router.delete("/entries/{entry_id}")
def delete_entry(entry_id: int) -> dict:
    with db.session() as s:
        if not wb.delete_entry(s, entry_id):
            raise HTTPException(404, "词条不存在")
    return {"ok": True}


@router.post("/entries/{entry_id}/review")
def review(entry_id: int, payload: dict = Body(...)) -> dict:
    """记一次复习结果。

    只收「认不认识」和「哪种模式」，不收档位——档位怎么推是 store 的事。
    以后加拼写、加选择题、加遗忘曲线，都还是打这个口，前端不用知道规则变了。
    """
    if "known" not in payload:
        raise HTTPException(400, "得说清楚这次认不认识")
    with db.session() as s:
        got = wb.record_review(s, entry_id, bool(payload["known"]),
                               str(payload.get("mode") or "flip"))
    if got is None:
        raise HTTPException(404, "词条不存在")
    return got
