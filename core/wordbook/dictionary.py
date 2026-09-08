"""背单词页的参考词典：给一个词形，配上音标、释义、屈折形式、派生词。

数据是 `data/wordbook/dict.csv`（生成物，来源与许可见同目录的 README）。
文件缺失时整个模块降级成「查不到」而不是抛异常——和 CEFR 词表缺失时退回内置
兜底表是同一个处理方式：**少一样东西不该让页面打不开**。

这一层只回答「这个词字典里怎么说」。**它不决定顺序**——顺序是用户从自己那本
纸质书里录进来的，存在 data/app.db 里，见 core/store/models.py 的 BookEntry。
两件事分开是有意的：词典可再生（重跑脚本就有），顺序不可再生（要人再敲一遍）。
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

from core.lexicon.lemma import lemma_candidates

DICT_PATH = Path(__file__).resolve().parents[2] / "data" / "wordbook" / "dict.csv"

# exchange 字段的编码，取自 ECDICT 的文档。
# 不收 "0"（原形）和 "1"（原形的变换类型）：那两个是往上指的，
# 而卡片背面要显示的是「这个词能变成什么」，方向反了就成了另一个词的信息。
_EXCHANGE_LABELS = {
    "p": "过去式", "d": "过去分词", "i": "现在分词", "3": "三单",
    "r": "比较级", "t": "最高级", "s": "复数", "f": "原级",
}
_EXCHANGE_ORDER = ("s", "3", "p", "d", "i", "r", "t", "f")


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """词形（小写）-> 词条。文件不在就返回空表。"""
    if not DICT_PATH.is_file():
        return {}
    table: dict[str, dict] = {}
    with DICT_PATH.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            word = (row.get("word") or "").strip()
            if not word:
                continue
            table.setdefault(word.lower(), row)
    return table


def is_available() -> bool:
    return bool(_load())


def size() -> int:
    return len(_load())


def _inflections(exchange: str) -> list[dict[str, str]]:
    """把 `p:abandoned/i:abandoning` 拆成能直接渲染的列表。

    按 _EXCHANGE_ORDER 排而不是按字符串里的顺序：同一个词在不同来源里字段顺序
    不一样，卡片上「复数」一会儿在前一会儿在后，看着像两张不同的卡。
    """
    got: dict[str, str] = {}
    for part in (exchange or "").split("/"):
        key, _, val = part.partition(":")
        key, val = key.strip(), val.strip()
        if key in _EXCHANGE_LABELS and val:
            got.setdefault(key, val)
    return [{"label": _EXCHANGE_LABELS[k], "word": got[k]}
            for k in _EXCHANGE_ORDER if k in got]


def _entry(row: dict, *, matched: str, asked: str) -> dict:
    tags = (row.get("tag") or "").split()
    word = row.get("word") or matched
    inflections = _inflections(row.get("exchange") or "")
    # 派生词里要**减掉屈折形式**：abandoned / abandoning / abandons 上面那一行
    # 已经按「过去式 / 现在分词 / 三单」列过了，再在「派生词」里列一遍，
    # 用户看到的是同一批词出现两次，而真正的派生词（abandonment）混在里面。
    # 构建脚本不做这个减法是有意的——那一步只管「哪些词存在」，
    # 显示口径的事归这里。
    shown = {i["word"].lower() for i in inflections} | {word.lower()}
    derivatives = [w for w in (row.get("derivatives") or "").split(",")
                   if w and w.lower() not in shown]
    return {
        "word": word,
        # 用户敲进来的原样词形。和 word 不一定相同（他敲 abandoned、字典条目是
        # abandon），卡片正面要显示的是**书上印的那个**，不是字典的原形。
        "surface": asked,
        "phonetic": row.get("phonetic") or "",
        "translation": row.get("translation") or "",
        "derivatives": derivatives,
        "inflections": inflections,
        "collins": int(row["collins"]) if (row.get("collins") or "").isdigit() else 0,
        "frq": int(row["frq"]) if (row.get("frq") or "").isdigit() else 0,
        "tags": tags,
        # 书上印的词形和字典条目对不上时要说一声（abandoned -> abandon）。
        # 不说的话用户会以为字典收的就是他敲的那个，而卡片背面显示的是别的词。
        "lemma_fallback": matched.lower() != asked.lower(),
    }


def normalize(word: str) -> str:
    """粘贴进来的一个 token 收敛成可查的词形。

    只去首尾的标点和序号残留，**不动大小写以外的内部结构**：连字符词
    （self-esteem）和撇号词（one's）在书上就是那么印的，剖开就查不到了。
    """
    return (word or "").strip().strip(".,;:!?\"'()[]{}<>·、，。；：！？…—-").strip()


def lookup(word: str) -> dict | None:
    """查一个词。查不到就沿着词形还原再试一轮，还不行返回 None。"""
    asked = normalize(word)
    if not asked:
        return None
    table = _load()
    row = table.get(asked.lower())
    if row is not None:
        return _entry(row, matched=asked, asked=asked)
    # 书上印的是派生/屈折形式时（visas / abandoned），还原回去再查一次。
    # 走 lemma_candidates 而不是另写一套：这个项目对「这是不是同一个词」
    # 只有一个判据（需要注意.md 第 6 条），背单词页没有理由再立一个。
    for cand in lemma_candidates(asked):
        row = table.get(cand)
        if row is not None:
            return _entry(row, matched=cand, asked=asked)
    return None


def _blank(asked: str) -> dict:
    """词典里没有这个词时的占位条目。

    存在的理由：**查不到不等于不要**。用户敲进来的是他书上印着的词，
    词典收没收是词典的事——丢掉它，那一页就少一个词，而且后面全体错位，
    这正好打碎这个功能唯一要保证的东西。释义留空让他自己补（词表视图能改）。
    """
    return {
        "word": asked, "surface": asked, "phonetic": "", "translation": "",
        "derivatives": [], "inflections": [], "collins": 0, "frq": 0, "tags": [],
        "lemma_fallback": False, "found": False,
    }


def annotate(words: list[str]) -> list[dict]:
    """按给定顺序给每个词配上词典信息，**一个都不丢**。

    查不到的位置留一个 found=False 的占位条目，而不是从列表里消失——
    这是「录入 = 你书上那一页」这条承诺的落点。调用方靠 found 分辨
    「配上了」和「没配上」，两种视图（预览、入库）从同一份结果派生，
    不会出现「预览说会录进去、实际没录」这种两头对不上
    （需要注意.md 第 20 条）。

    顺序原样保留，一个都不排序、不去重。重复的词也照留：
    一页上同一个词出现两次是书自己的事，不是要程序修的错。
    """
    out: list[dict] = []
    for raw in words:
        asked = normalize(raw)
        if not asked:
            continue
        entry = lookup(asked)
        if entry is None:
            out.append(_blank(asked))
        else:
            out.append({**entry, "found": True})
    return out


def lookup_many(words: list[str]) -> tuple[list[dict], list[str]]:
    """annotate 的两分view：(查到的, 没查到的词形)。给预览用。"""
    items = annotate(words)
    return ([e for e in items if e["found"]],
            [e["surface"] for e in items if not e["found"]])


def _is_english(token: str) -> bool:
    """这个 token 是不是一个英文词形。

    判据必须是「含 **ASCII** 字母」，不能是 `str.isalpha()`：汉字的 isalpha()
    也是 True，于是 `vt. 放弃, 抛弃` 里的「抛弃」会被当成一个待查的单词收进来，
    最后出现在「没匹配上」的名单里让人以为是漏词。踩过一次，留着。
    """
    return any("a" <= c.lower() <= "z" for c in token)


def _first_word(chunk: str) -> str:
    """一小段文本里的头一个英文词形。

    OCR 出来的一行常常是 `abandon [ə'bændən] vt. 放弃`，音标和释义跟在后面——
    它们收进来只会变成一堆查不到的词，所以每段只取头一个。
    行首的序号（`12. abandon`、`12 abandon`）先跳过。
    """
    for tok in chunk.split():
        tok = normalize(tok)
        if not tok or tok.isdigit():
            continue
        if _is_english(tok):
            return tok
    return ""


# 一行里出现这些，说明它是「单词 + 音标 / 释义」那种字典行，不是「一行好几个词」
_GLOSS_MARKERS = ("[", "［", "/")


def _looks_like_gloss(line: str) -> bool:
    if any(m in line for m in _GLOSS_MARKERS):
        return True
    # 有汉字 = 释义已经跟在同一行上了
    return any("一" <= c <= "鿿" for c in line)


def split_words(text: str) -> list[str]:
    """把粘进来的一段文本切成词形序列，顺序不变。

    容忍的输入形状是照着「手机 OCR 出来的一页」定的：一行一个词最常见，
    但也有人把一整页贴成逗号分隔的一长串。两种都要吃下去，而且**都不能重排**。

    怎么区分这两种：**先看这一行里有没有释义**（汉字、音标方括号、斜杠）。
    有 = 字典行，整行只取头一个词；没有 = 才允许按逗号切成多个词。
    顺序反过来（先按逗号切）会把 `abandon [ə'bændən] vt. 放弃, 抛弃` 切成两段，
    第二段「抛弃」被当成一个查不到的单词报出来——看着像漏词，其实是解析错了。
    """
    out: list[str] = []
    for line in (text or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if _looks_like_gloss(line):
            chunks = [line]
        elif any(sep in line for sep in (",", "，", "、", ";", "；")):
            for sep in ("，", "、", ";", "；"):
                line = line.replace(sep, ",")
            chunks = line.split(",")
        else:
            chunks = [line]
        for chunk in chunks:
            word = _first_word(chunk)
            if word:
                out.append(word)
    return out
