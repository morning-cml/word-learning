"""间隔重复调度（FSRS）。

在这之前，背诵页每一轮把所有 status < 98 的词**全部再问一遍**：昨天刚背到
「很熟」的和今天第一次见的待遇一样。对着一本 5407 词的六级书，那一页是个复读机。

用 FSRS 而不是自己拍一套间隔：间隔重复是个**有正确答案**的问题（DSR 模型，
有论文有实测），自己拍出来的曲线只会更差，而且差在哪儿看不出来——
背单词的效果没有反馈回路，用户不会因为间隔排得不好来报错，他只会觉得
「背了半天没记住」。这正是这个项目最该避免的那类失败。

**这一层只做翻译。** 它把这个应用的语汇（认识 / 不认识）翻成 FSRS 的语汇
（Rating.Good / Rating.Again），再把 FSRS 的 Card 摊平成能落库的东西。
档位怎么推、什么时候到期，一概不在这里决定——那是库的事。

没装 fsrs 也能跑：`available()` 返回 False，调用方退回原来的行为。
和 json-repair、CEFR 词表缺失是同一个处理方式：少一样东西不该让功能中断。
"""
from __future__ import annotations

from datetime import datetime, timezone

try:                          # 可选依赖：装了就有间隔重复，没装退回「每轮全过一遍」
    from fsrs import Card, Rating, Scheduler
except ImportError:           # pragma: no cover - 取决于装没装
    Card = Rating = Scheduler = None  # type: ignore[assignment]

_scheduler = None


def available() -> bool:
    return Scheduler is not None


def _sched():
    global _scheduler
    if _scheduler is None and Scheduler is not None:
        # 默认参数。优化过的参数要用户自己的复习历史才训得出来，而这个应用
        # 目前只存最后一次复习——真要做，先加一张 review 表（见 models.py
        # BookEntry 上面那段），不是在这里拍一组数。
        _scheduler = Scheduler()
    return _scheduler


def review(state: dict | None, known: bool, now: datetime | None = None) -> dict:
    """记一次复习，返回新的调度状态。

    state 是上一次存下来的那份（entry.fsrs），第一次复习传 None 或 {}。
    返回 `{"fsrs": {...}, "due_at": datetime|None}`，调用方原样存。

    只用 Good / Again 两档：界面上就两个按钮。FSRS 还有 Hard / Easy，
    但**没有按钮就不该有档**——凭空把「认识」拆成三档，等于替用户做了他
    没做过的判断，而那个判断会一路影响之后所有的间隔。
    要加档就先加按钮，两头一起改。
    """
    if not available():
        return {"fsrs": dict(state or {}), "due_at": None}

    card = Card.from_dict(state) if state else Card()
    rating = Rating.Good if known else Rating.Again
    # fsrs 要求 review_datetime 是**带时区且为 UTC** 的，传 naive 会直接抛
    # ValueError。而这个库里所有时间列存的都是 naive UTC（见 models.as_utc），
    # 所以「从库里读一个时间再传进来」是最自然的写法，也是必然会踩的那一脚。
    # 在这里补上标记，别让调用方记这条规矩。
    card, _log = _sched().review_card(card, rating, review_datetime=_aware_utc(now))
    return {"fsrs": card.to_dict(), "due_at": _naive_utc(card.due)}


def _aware_utc(dt: datetime | None) -> datetime | None:
    """naive 一律按 UTC 解释（库里存的就是 UTC），aware 的换算到 UTC。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """FSRS 给的是 aware UTC，而库里那一列存的是 naive UTC。

    直接把 aware 的塞进去，SQLite 存下来是带偏移量的字符串，读出来还是 naive——
    但**数值已经不是 UTC 了**，之后所有的「到期没有」都会差一个时区。
    库里所有时间列都是 naive UTC（见 models.as_utc 那段），这里跟着来。
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def is_due(due_at: datetime | None, now: datetime | None = None) -> bool:
    """到期了没有。**没有 due_at 一律算到期**。

    这个默认是有意的：老库补列之后 due_at 是 NULL，没装 fsrs 的机器上也一直是
    NULL。要是把「没有 due_at」当成「不到期」，那些词就再也不会被问到——
    用户看到的是一个空的背诵页，而他的词一个没少。
    往「多问一次」的方向兜底，不往「悄悄少问」的方向。
    """
    if due_at is None:
        return True
    return due_at <= (now or datetime.utcnow())
