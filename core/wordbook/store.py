"""词书的读写。

单独成文件而不是塞进 core/store/db.py：那边已经 20KB，而且这一块以后还要长
（测验、遗忘曲线、导出）。表定义仍然留在 core/store/models.py——**必须留在那里**，
因为 db.init_db() 只 import 那一个模块，模型定义在别处就不会被
Base.metadata 收进去，create_all 静静地不建表，而错误要等到第一次查询才炸。

这一层的不变式只有一条，但它是整个功能的地基：

    **同一个单元里，entries 永远按 idx 升序 == 纸质书上的顺序。**

任何一处按别的字段排（字母、词频、id、创建时间），「和纸质版一致」就断了，
而且断得很安静——用户翻到第 42 页发现对不上，多半只会以为自己录错了。
所以插入要算位置（_insert_at），不是无脑 append。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.store.models import (
    STATUS_IGNORED, STATUS_KNOWN, STATUS_LEARNING, STATUS_LEARNING_MAX, STATUS_NEW,
    BookEntry, BookUnit, WordBook, utcnow,
)
from core.wordbook import dictionary, scheduler

MAX_NAME = 120
MAX_LABEL = 60
# 一次粘贴的上限。纸质书一页撑死几十个词，一次几千个多半是贴错了东西
# （整篇文章、整个 PDF），当场拦住比事后让他一条条删干净。
MAX_PASTE = 500


# ------------------------------------------------------------------ 词书

def list_books(s: Session) -> list[dict]:
    """所有词书 + 各自的进度。按创建时间倒序（新建的在最上面）。"""
    counts = _progress_by(s, BookUnit.book_id)
    due = _due_by(s, BookUnit.book_id)
    return [
        {
            "id": b.id, "name": b.name, "note": b.note or "",
            "source": b.source,
            "units": len(b.units),
            **counts.get(b.id, _EMPTY_PROGRESS),
            "due": due.get(b.id, 0),
        }
        for b in s.scalars(select(WordBook).order_by(WordBook.created_at.desc()))
    ]


def create_book(s: Session, name: str, note: str = "") -> dict | None:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        return None
    exists = s.scalar(select(WordBook).where(WordBook.name == name))
    if exists is not None:
        # 同名直接返回已有的那本，不报错：用户多半就是想往那本里继续加
        return _book_dict(s, exists)
    book = WordBook(name=name, note=(note or "").strip() or None)
    s.add(book)
    s.flush()
    return _book_dict(s, book)


def update_book(s: Session, book_id: int, *, name: str | None = None,
                note: str | None = None) -> dict | None:
    book = s.get(WordBook, book_id)
    if book is None:
        return None
    if name is not None and name.strip():
        book.name = name.strip()[:MAX_NAME]
    if note is not None:
        book.note = note.strip() or None
    return _book_dict(s, book)


def delete_book(s: Session, book_id: int) -> bool:
    book = s.get(WordBook, book_id)
    if book is None:
        return False
    s.delete(book)          # 单元和词条跟着级联删
    return True


# ------------------------------------------------------------------ 单元

def _idx_from_label(label: str) -> int | None:
    """从「List 07」里认出这是第 7 个单元。

    不认的话，排序就只能按录入先后——而用户是**背到哪录到哪**，先录 List 7、
    隔天补 List 3 是常态，于是 List 3 排在 List 7 后面。不报错，只是顺序错了，
    而顺序是这个功能唯一的卖点。

    取第一串数字而不是最后一串："Unit 3-A" 要的是 3 不是 1；
    "List 07" 要的是 7 不是 07 后面可能跟着的页码。
    认不出来（"核心词 Ⅱ"、"附录"）就返回 None 交给调用方排到末尾——
    猜一个数字比排到末尾更糟，那会把已有的单元挤乱。
    """
    import re

    m = re.search(r"\d+", label or "")
    return int(m.group()) if m else None


def list_units(s: Session, book_id: int) -> list[dict] | None:
    book = s.get(WordBook, book_id)
    if book is None:
        return None
    counts = _progress_by(s, BookEntry.unit_id, book_id=book_id)
    due = _due_by(s, BookEntry.unit_id, book_id=book_id)
    out = []
    for u in book.units:                      # relationship 已经按 idx 排好
        pages = sorted({e.page for e in u.entries if e.page is not None})
        out.append({
            "id": u.id, "label": u.label, "idx": u.idx, "note": u.note or "",
            "pages": pages,
            "page_range": _fmt_pages(pages),
            **counts.get(u.id, _EMPTY_PROGRESS),
            "due": due.get(u.id, 0),
        })
    return out


def create_unit(s: Session, book_id: int, label: str, idx: int | None = None) -> dict | None:
    book = s.get(WordBook, book_id)
    if book is None:
        return None
    label = (label or "").strip()[:MAX_LABEL]
    if not label:
        return None
    exists = s.scalar(
        select(BookUnit).where(BookUnit.book_id == book_id, BookUnit.label == label)
    )
    if exists is not None:
        return _unit_dict(exists)             # 同名沿用，理由同 create_book
    if idx is None:
        idx = _idx_from_label(label)
    if idx is None:
        # 标签里没有数字（"核心词 Ⅱ"、"附录"），只能排到最后
        top = s.scalar(select(func.max(BookUnit.idx)).where(BookUnit.book_id == book_id))
        idx = (top or 0) + 1
    unit = BookUnit(book_id=book_id, label=label, idx=int(idx))
    s.add(unit)
    s.flush()
    return _unit_dict(unit)


def update_unit(s: Session, unit_id: int, *, label: str | None = None,
                idx: int | None = None, note: str | None = None) -> dict | None:
    unit = s.get(BookUnit, unit_id)
    if unit is None:
        return None
    if label is not None and label.strip():
        unit.label = label.strip()[:MAX_LABEL]
    if idx is not None:
        unit.idx = int(idx)
    if note is not None:
        unit.note = note.strip() or None
    return _unit_dict(unit)


def delete_unit(s: Session, unit_id: int) -> bool:
    unit = s.get(BookUnit, unit_id)
    if unit is None:
        return False
    s.delete(unit)
    return True


# ------------------------------------------------------------------ 词条

def page_value(raw: object) -> int | None:
    """把外面传进来的页号收敛成 int 或 None（None = 这一页不写页号）。

    只此一处解释「什么算一个页号」，录入和改词条都走它——同一件事写两遍必然
    分叉（需要注意.md 第 20 条），而这两处原来确实是分叉的：录入用
    `.isdigit()` 判过，改词条直接 `int(...)`，于是词表视图里那个自由文本的
    页号框只要收到一个非数字（用户打成「43页」，或者前端送来 null），
    `int()` 当场抛 ValueError / TypeError —— 接口层没人接，变成 HTTP 500，
    而 db.session() 会把整次请求回滚：**同一次保存里的释义和笔记也一起丢了**。
    """
    text = "" if raw is None else str(raw).strip()
    return int(text) if text.isdigit() else None


def page_is_valid(raw: object) -> bool:
    """空 = 不写页号，纯数字 = 页号，其余都不是。

    单独给一个判据，是为了让调用方能把「用户打错了」和「用户有意留空」分开：
    前者该当场报出来。悄悄把认不出来的页号存成 None，页号就没了、这一页还会
    跑到末尾去，而用户什么提示都收不到——正是这个项目最不能接受的那种失败。
    """
    text = "" if raw is None else str(raw).strip()
    return not text or text.isdigit()


def _insert_at(s: Session, unit_id: int, page: int | None) -> int:
    """新的一页该插在这个单元的哪个 idx 上。

    按页码找位置，而不是一律追加到末尾：用户是「背到哪录到哪」，很可能先录了
    第 44 页、隔天才回头补第 43 页。无脑 append 会让 43 页排在 44 页后面——
    顺序错了，而这正是这个功能唯一要保证的东西。

    **返回的是 idx 值，不是列表下标。** 两者只在 idx 恰好是 0..n-1 连着排的
    时候才相等，而 replace_page 一删就把它们错开了：删掉的那几个位置留着空档，
    后面的行是整体 +len(items) 挪过去的，不重排。此后下标必然小于等于它那一行
    的 idx，而 add_entries 拿这个返回值同时干两件事——`idx >= at` 的整体后挪、
    以及新行的 `idx = at + offset`——**两件事都按 idx 算**，于是：

      · 该挪的没挪、不该挪的挪了。实测「43/44/45 三页 → 重贴 44 页 → 再录 46 页」
        之后，第 46 页整个排到了第 45 页前面；
      · 往中间插一页时新词会落在同页词的中间，而不是接在它们后面。

    错了不会抛异常，只是顺序不对——而这个功能唯一的卖点就是顺序，用户翻到那一页
    对不上，多半只会以为是自己录错了（需要注意.md 第一节说的那种沉默失败）。
    """
    rows = list(s.scalars(
        select(BookEntry).where(BookEntry.unit_id == unit_id).order_by(BookEntry.idx)
    ))
    if not rows:
        return 0
    if page is not None:
        # 第一个页码比它大的词条，就插在那之前（同页的已有词条排在新词前面）
        for e in rows:
            if e.page is not None and e.page > page:
                return e.idx
    return rows[-1].idx + 1


def add_entries(s: Session, unit_id: int, words: list[str], *,
                page: int | None = None, replace_page: bool = False) -> dict | None:
    """把一页词加进单元。返回加了几个、哪些词典里没有。

    顺序 = words 传进来的顺序，一个都不重排（见模块开头那条不变式）。
    """
    unit = s.get(BookUnit, unit_id)
    if unit is None:
        return None

    # annotate 而不是「只取查到的」：**查不到的词也要按原位录进去**。
    # 丢掉它，用户书上那一页 12 个词、应用里只有 11 个，而且后面全体错位——
    # 正好打碎这个功能唯一要保证的东西。释义留空，他可以在词表视图里自己补。
    #
    # 解析必须排在删除**前面**：原来是先删旧的再解析，于是「一个词都没解析出来」
    # 那一次会走到下面那条早退，返回一个 added: 0 就完事——而这一页已经删掉了。
    # 界面上看到的是「已录入 0 个词」，实际是那一页被清空了，
    # 而它是用户一页页敲进去的、补不回来的东西。
    items = dictionary.annotate(words[:MAX_PASTE])
    missing = [e["surface"] for e in items if not e["found"]]
    if not items:
        return {"added": 0, "missing": missing, "entries": [],
                "restored": 0, "truncated": len(words) > MAX_PASTE}

    replaced_status: dict[str, tuple[int, int, int]] = {}
    if replace_page and page is not None:
        # 重贴同一页（贴错了、OCR 漏了几个）时，把这一页原来的词条换掉，
        # 但**把已经背出来的进度按词形接回去**——重贴一次就把进度清零，
        # 用户会宁可忍着错误的顺序也不敢重贴。
        old = [e for e in unit.entries if e.page == page]
        for e in old:
            replaced_status[e.headword.lower()] = (e.status, e.right, e.wrong)
            s.delete(e)
        s.flush()

    at = _insert_at(s, unit_id, page)
    # 给插入点腾位置：它后面的全部往后挪
    for e in s.scalars(select(BookEntry).where(BookEntry.unit_id == unit_id,
                                               BookEntry.idx >= at)):
        e.idx += len(items)
    s.flush()

    restored = 0
    rows: list[BookEntry] = []
    for offset, item in enumerate(items):
        prev = replaced_status.get(item["surface"].lower())
        restored += bool(prev)
        row = BookEntry(
            unit_id=unit_id, idx=at + offset, page=page,
            headword=item["surface"][:80], lemma=item["word"][:80],
            phonetic=item["phonetic"][:120], translation=item["translation"],
            inflections=item["inflections"], derivatives=item["derivatives"],
            dict_extra={"collins": item["collins"], "frq": item["frq"],
                        "tags": item["tags"], "lemma_fallback": item["lemma_fallback"],
                        # 词典里没有它——词表视图靠这个把它标出来，
                        # 提醒用户这一条的释义要自己补
                        "no_dict": not item["found"]},
            status=prev[0] if prev else STATUS_NEW,
            right=prev[1] if prev else 0,
            wrong=prev[2] if prev else 0,
        )
        s.add(row)
        rows.append(row)
    s.flush()
    return {
        "added": len(rows), "missing": missing, "restored": restored,
        "truncated": len(words) > MAX_PASTE,
        "entries": [_entry_dict(e) for e in rows],
    }


def list_entries(s: Session, unit_id: int, *, page: int | None = None) -> list[dict] | None:
    unit = s.get(BookUnit, unit_id)
    if unit is None:
        return None
    rows = [e for e in unit.entries if page is None or e.page == page]
    return [_entry_dict(e) for e in rows]


def update_entry(s: Session, entry_id: int, patch: dict) -> dict | None:
    """改一个词条。释义和笔记都能改——书上的说法和字典不一样时以书为准。"""
    entry = s.get(BookEntry, entry_id)
    if entry is None:
        return None
    if "translation" in patch:
        entry.translation = str(patch["translation"] or "")
    if "phonetic" in patch:
        entry.phonetic = str(patch["phonetic"] or "")[:120]
    if "note" in patch:
        entry.note = (str(patch["note"] or "").strip() or None)
    if "page" in patch:
        entry.page = page_value(patch["page"])
    if "status" in patch:
        entry.status = _clamp_status(patch["status"])
        _sync_library(s, entry)
    return _entry_dict(entry)


def delete_entry(s: Session, entry_id: int) -> bool:
    entry = s.get(BookEntry, entry_id)
    if entry is None:
        return False
    s.delete(entry)
    return True


# ------------------------------------------------------------------ 背诵

def record_review(s: Session, entry_id: int, known: bool, mode: str = "flip") -> dict | None:
    """记一次复习结果。

    **这是背诵进度唯一的写入口。** 以后换成遗忘曲线（算 due_at / interval）、
    或者要逐次留痕（单开一张 review 表），改这一个函数就够了——
    调用方只告诉它「这次认不认识」，不知道也不该知道档位是怎么推的。
    mode 现在只是记着，留给以后区分「翻卡片认出来」和「拼写默出来」：
    后者是强得多的证据，同样一次「认识」不该算一样多。
    """
    entry = s.get(BookEntry, entry_id)
    if entry is None:
        return None
    if known:
        entry.right += 1
        entry.status = _advance(entry.status)
    else:
        entry.wrong += 1
        # 退回「刚认识」而不是原地不动：没答上来就是没记住，
        # 下一轮还得再见它。降一档的话，一个 5 档的词答错三次才回到重点复习区。
        entry.status = STATUS_LEARNING
    entry.last_reviewed_at = utcnow()

    # 排下一次什么时候再问。**档位和到期日是两件事，都留着**：
    # 档位（status）是给人看的「我背到哪了」，还被词库同步和进度条读；
    # 到期日是给队列用的。用档位反推间隔的话，「刚认识」的新词和一个背了
    # 五次又忘掉的词会排到同一天，而它们的遗忘曲线差得远。
    sched = scheduler.review(entry.fsrs or None, known)
    entry.fsrs = sched["fsrs"]
    entry.due_at = sched["due_at"]

    _sync_library(s, entry)
    return _entry_dict(entry)


def _advance(status: int) -> int:
    if status in (STATUS_KNOWN, STATUS_IGNORED):
        return status
    if status >= STATUS_LEARNING_MAX:
        return STATUS_KNOWN
    return max(STATUS_LEARNING, int(status or 0) + 1)


def _clamp_status(value: object) -> int:
    try:
        status = int(value)
    except (TypeError, ValueError):
        return STATUS_NEW
    if status in (STATUS_KNOWN, STATUS_IGNORED):
        return status
    return max(STATUS_NEW, min(STATUS_LEARNING_MAX, status))


def _sync_library(s: Session, entry: BookEntry) -> None:
    """标成「已掌握」的词，落一条进共用的词库。

    为什么只在 98 这一档同步，而不是背单词碰过的词全都进词库：
    词库那份数据被 `db.studied_lemmas()` 读去当**难度标尺的豁免名单**
    （需要注意.md 第 4e 条）。一本六级书 5407 个词，全灌进去等于告诉标尺
    「这些词读者都认得」，超纲检测当场失效——而界面上只会显示一个更好看的
    超纲率，没有任何人会发现。

    98 这一档是用户自己按下去的明确断言，正好就是那份名单要的证据。
    "刚认识" 不是。

    反过来也要成立：一个词从 98 掉下来（背错了），要把词库里那条降回学习中，
    否则豁免名单只进不出，越攒越松。
    """
    from core.store import db

    if entry.status == STATUS_KNOWN:
        word = db.get_or_create_word(s, entry.lemma or entry.headword)
        word.status = STATUS_KNOWN
        if not word.gloss and entry.translation:
            word.gloss = entry.translation
    elif entry.status < STATUS_KNOWN:
        from core.lexicon import cefr
        from core.store.models import Word

        key = cefr.resolve(entry.lemma or entry.headword)
        word = s.scalar(select(Word).where(Word.lemma == key))
        if word is not None and word.status == STATUS_KNOWN:
            word.status = max(STATUS_LEARNING, entry.status)


# ------------------------------------------------------------------ 内部

_EMPTY_PROGRESS = {"total": 0, "new": 0, "learning": 0, "known": 0, "due": 0}


def _due_by(s: Session, group_col, *, book_id: int | None = None) -> dict[int, int]:
    """今天该复习几个。单独一条语句，不塞进 _progress_by。

    两者的分组维度一样，但**筛选条件不一样**（那边按 status 分桶，这边按
    due_at 筛），硬并成一条要写成条件聚合，读起来比两条慢查询贵得多。
    这一页的数据量是一本书几千条，两条索引扫描的差别看不出来。

    `due_at IS NULL` 也算到期：老库补列之后是 NULL，没装 fsrs 时也一直是
    NULL。当成「不到期」的话那些词再也不会被问到，而单元卡上会显示 0——
    用户看到「今天没有要背的」，其实是整本书都被藏起来了。
    """
    from sqlalchemy import or_

    q = (
        select(group_col, func.count())
        # select_from 必须显式给：按书分组时 group_col 是 BookUnit.book_id，
        # 选择列里一个 BookEntry 都没有，SQLAlchemy 会把 FROM 推成 BookUnit，
        # 然后 join BookUnit 到它自己——报的是「Don't know how to join」。
        # _progress_by 碰不到这条只是因为它的选择列里带了 BookEntry.status。
        .select_from(BookEntry)
        .join(BookUnit, BookEntry.unit_id == BookUnit.id)
        .where(BookEntry.status < STATUS_KNOWN)
        .where(or_(BookEntry.due_at.is_(None), BookEntry.due_at <= utcnow().replace(tzinfo=None)))
        .group_by(group_col)
    )
    if book_id is not None:
        q = q.where(BookUnit.book_id == book_id)
    return dict(s.execute(q).all())


def _progress_by(s: Session, group_col, *, book_id: int | None = None) -> dict[int, dict]:
    """按单元或按书统计进度。一条 SQL 出结果，别在 Python 里遍历几千个词条。"""
    q = (
        select(group_col, BookEntry.status, func.count())
        .join(BookUnit, BookEntry.unit_id == BookUnit.id)
        .group_by(group_col, BookEntry.status)
    )
    if book_id is not None:
        q = q.where(BookUnit.book_id == book_id)
    out: dict[int, dict] = {}
    for key, status, n in s.execute(q):
        bucket = out.setdefault(key, dict(_EMPTY_PROGRESS))
        bucket["total"] += n
        if status == STATUS_NEW:
            bucket["new"] += n
        elif status in (STATUS_KNOWN, STATUS_IGNORED):
            bucket["known"] += n
        else:
            bucket["learning"] += n
    return out


def _fmt_pages(pages: list[int]) -> str:
    """[43,44,45,48] -> "P43–45, 48"。单元卡片上要一眼看出覆盖了书的哪几页。"""
    if not pages:
        return ""
    spans: list[tuple[int, int]] = []
    for p in pages:
        if spans and p == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], p)
        else:
            spans.append((p, p))
    return "P" + ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in spans)


def _book_dict(s: Session, book: WordBook) -> dict:
    counts = _progress_by(s, BookUnit.book_id).get(book.id, _EMPTY_PROGRESS)
    return {"id": book.id, "name": book.name, "note": book.note or "",
            "source": book.source, "units": len(book.units), **counts,
            "due": _due_by(s, BookUnit.book_id).get(book.id, 0)}


def _unit_dict(unit: BookUnit) -> dict:
    pages = sorted({e.page for e in unit.entries if e.page is not None})
    return {"id": unit.id, "book_id": unit.book_id, "label": unit.label,
            "idx": unit.idx, "note": unit.note or "",
            "pages": pages, "page_range": _fmt_pages(pages),
            "total": len(unit.entries), "new": 0, "learning": 0, "known": 0, "due": 0}


def _entry_dict(e: BookEntry) -> dict:
    from core.store.models import STATUS_LABELS

    from core.store.models import as_utc

    return {
        "id": e.id, "unit_id": e.unit_id, "idx": e.idx, "page": e.page,
        "headword": e.headword, "lemma": e.lemma,
        # 到期时间下发到前端，队列按它筛。补回 UTC 标记再发，
        # 否则不带偏移量的 ISO 串会被 new Date() 当本地时间读第二遍
        # （需要注意.md 第 12d 条）。
        "due_at": as_utc(e.due_at).isoformat() if e.due_at else "",
        "due": scheduler.is_due(e.due_at),
        "phonetic": e.phonetic, "translation": e.translation,
        "inflections": e.inflections or [], "derivatives": e.derivatives or [],
        "note": e.note or "",
        "status": e.status, "status_label": STATUS_LABELS.get(e.status, str(e.status)),
        "right": e.right, "wrong": e.wrong,
        "extra": e.dict_extra or {},
    }
