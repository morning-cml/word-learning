"""把库里的东西导成 CSV。

为什么值得有：`data/app.db` 是这个项目唯一不可再生的资产，而在这之前它
**一个出口都没有**——快照也只是同一种格式的另一份拷贝。SQLite 本身是通用格式，
所以这不是「锁死了」那种紧急问题；导出真正解决的是另外两件：

  · **校对。** 词书顺序是用户一页页敲进去的，而「和纸质版一致」这条承诺目前
    只能靠在界面上一行行翻来验。导成一张表，他可以对着书扫一眼。
  · **互操作。** 词 + 释义 + 例句正好是 Anki 的导入形状。累计语境是这个应用
    攒出来的东西，没有理由只能在这个应用里看。

两条贯穿的决定：

**一、词库导出是一行一处语境，不是一行一个词。** 一个词在不同故事里出现多次
正是这个产品声称最有效的机制；压成一行只留一句例句，等于把它的核心资产
在导出这一步丢掉。重复的那几列是 join 的正常形状，Anki 那边多几张卡也恰好
是对的（多语境 = 多张卡）。没有语境的词照样出一行，字段留空。

**二、词书导出严格按 (单元 idx, 词条 idx) 排。** 那就是书上的顺序，也是这个
功能唯一的卖点。任何一处按别的字段排，这份导出就失去了校对的价值——
而且错得很安静。

编码带 BOM：这个应用的用户在 Windows 上，导出来八成是双击用 Excel 打开的，
不带 BOM 的 UTF-8 会被 Excel 按本地代码页读，整列中文变成乱码。带上 BOM
是三个字节的事，而少了它这份导出对多数人直接没用。
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.store.models import (
    STATUS_LABELS, BookEntry, BookUnit, Word, WordBook,
)

#: Excel 在 Windows 上靠它认出这是 UTF-8。没有它，中文列全是乱码。
BOM = "﻿"

WORDS_HEADER = (
    "词", "CEFR", "释义", "掌握程度", "见过次数", "出现在几篇", "文中形态",
    "出处文章", "句中形态", "英文", "中文", "语境线索", "线索强度",
)

BOOK_HEADER = (
    "词书", "单元", "单元序号", "序号", "页号", "词形", "原形", "音标",
    "释义", "笔记", "掌握程度", "答对", "答错",
)

#: 线索强度在库里是英文枚举，导出面向的是人，翻成中文
_STRENGTH = {"strong": "充分", "weak": "偏弱", "none": "无"}


def _sheet(header, rows) -> str:
    """拼一张 CSV。lineterminator 写死 \\r\\n：Excel 认这个，而 csv 模块
    在不同平台上默认值不一样——导出的文件在谁的机器上生成不该有区别。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return BOM + buf.getvalue()


def words_csv(s: Session) -> str:
    """词库：一行一处语境。

    排序按词条的字母序，而不是「最近见过」——导出是拿去核对和归档的，
    每次导出的行序应该一样，界面上那个「最近在前」的排法在这里只会
    让两次导出的 diff 看不出到底改了什么。
    """
    rows: list[tuple] = []
    words = list(s.scalars(select(Word).order_by(Word.lemma)))
    for word in words:
        forms = "、".join(sorted({f.surface for f in word.forms}))
        label = STATUS_LABELS.get(word.status, str(word.status))
        contexts = sorted(word.encounters, key=lambda e: e.id)
        seen = word.times_seen or 0
        articles = len({
            e.sentence.article_id for e in contexts if e.sentence is not None
        })
        base = (word.lemma, word.cefr or "", word.gloss or "", label, seen, articles, forms)
        if not contexts:
            rows.append((*base, "", "", "", "", "", ""))
            continue
        for enc in contexts:
            sent = enc.sentence
            if sent is None:
                continue
            art = sent.article
            rows.append((
                *base,
                art.title_en if art else "",
                enc.surface or "",
                sent.en or "",
                sent.zh or "",
                enc.clue or "",
                _STRENGTH.get(enc.clue_strength or "", ""),
            ))
    return _sheet(WORDS_HEADER, rows)


def book_csv(s: Session, book_id: int) -> str | None:
    """一本词书：严格按书上的顺序。查不到这本书返回 None。"""
    book = s.get(WordBook, book_id)
    if book is None:
        return None
    rows: list[tuple] = []
    # relationship 上已经声明了 order_by（BookUnit.idx / BookEntry.idx），
    # 这里不再排一次——排序判据只该有一处，多一处就多一个分叉的机会。
    for unit in book.units:
        for entry in unit.entries:
            rows.append((
                book.name,
                unit.label,
                unit.idx,
                entry.idx,
                entry.page if entry.page is not None else "",
                entry.headword,
                entry.lemma or "",
                entry.phonetic or "",
                entry.translation or "",
                entry.note or "",
                STATUS_LABELS.get(entry.status, str(entry.status)),
                entry.right,
                entry.wrong,
            ))
    return _sheet(BOOK_HEADER, rows)


def book_filename(s: Session, book_id: int) -> str:
    """给这本书的导出文件起个名。

    书名是用户自己填的，可能带斜杠、冒号、引号——这些在 Windows 上不能做
    文件名，而且原样塞进 Content-Disposition 还会把那个头本身弄坏。
    """
    book = s.get(WordBook, book_id)
    name = (book.name if book else "wordbook").strip() or "wordbook"
    safe = "".join("_" if c in '\\/:*?"<>|\r\n' else c for c in name)
    return f"{safe[:60]}.csv"


def _unit_count(s: Session, book_id: int) -> int:
    return len(list(s.scalars(select(BookUnit).where(BookUnit.book_id == book_id))))


def book_entry_count(s: Session, book_id: int) -> int:
    return len(list(s.scalars(
        select(BookEntry).join(BookUnit, BookEntry.unit_id == BookUnit.id)
        .where(BookUnit.book_id == book_id)
    )))
