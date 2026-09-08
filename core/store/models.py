"""数据模型。

设计上有两条线，都是为了「多功能集成」而不是只服务文章生成：

1. 父词条 / 词形（借鉴 Lute v3 的 parent term）
   Word 存原形，WordForm 存文中出现过的变形。abandoned / abandonment
   都挂在 abandon 下，共享释义与例句。以后加任何模块都认同一个词。

2. 共享学习状态
   Encounter 记录「这个词在哪句话里被你读到过」。文章模块只管写，
   将来的测验 / SRS / 关联图谱模块直接读——这是集成应用与功能堆砌的分界线。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """给要下发到前端的时间补回 UTC 标记。

    存进去的是 UTC（utcnow()），但 SQLite 的 DateTime 列**不保留 tzinfo**，
    读出来是 naive 的。naive 的 isoformat() 长这样：`2026-08-31T15:31:39`，
    不带偏移量——而 ES 规范规定，不带偏移量的 date-time 形式按**本地时间**解释。
    于是 `new Date()` 又把它当本地时间读了一遍，界面上每个时间都差一个时区：
    东八区差 8 小时，凌晨生成的文章连日期都会退到前一天。

    为什么没人报：差多少取决于用户在哪个时区，而唯一能发现它的办法是
    「记得自己到底几点点的生成」。CI 跑在 UTC 上，差值是 0，测试也照样绿。

    两种输入都要吃：库里读出来的是 naive（按 UTC 解释），本次会话新建的对象
    带 aware（expire_on_commit=False，见 delete_article 上面那段注释）。
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


# 词汇状态沿用 Lute 的分档：1-5 学习中，98 已掌握，99 忽略（专有名词等）
#
# 0 是背单词页加的：那一页里「这个词还没轮到我背」和「背过一次、刚认识」
# 是两件事，而原来的分档最低就是 1，两者只能挤在一起——单元进度会从一开始
# 就显示成 100% 在学。Lute 自己也用 0 表示「人从没碰过」（见 studied_lemmas
# 那段注释），所以这不是新造一档，是把它那一档补回来。
# Word 行不会以 0 存在（进词库本身就说明碰过了），它只用在 BookEntry 上。
STATUS_NEW = 0
STATUS_LEARNING = 1
STATUS_KNOWN = 98
STATUS_IGNORED = 99
STATUS_LABELS = {
    0: "未学",
    1: "刚认识", 2: "有印象", 3: "较熟", 4: "很熟", 5: "接近掌握",
    98: "已掌握", 99: "忽略",
}
# 学习中的那一段，背单词页推进掌握程度时要知道边界在哪
STATUS_LEARNING_MAX = 5


class Word(Base):
    """一个词条（原形）。"""

    __tablename__ = "words"

    id: Mapped[int] = mapped_column(primary_key=True)
    lemma: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[int] = mapped_column(Integer, default=STATUS_LEARNING)
    cefr: Mapped[str | None] = mapped_column(String(4), nullable=True)
    gloss: Mapped[str | None] = mapped_column(Text, nullable=True)      # 中文释义
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    forms: Mapped[list["WordForm"]] = relationship(
        back_populates="word", cascade="all, delete-orphan"
    )
    encounters: Mapped[list["Encounter"]] = relationship(
        back_populates="word", cascade="all, delete-orphan"
    )

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, str(self.status))


class WordForm(Base):
    """词条在真实文本里出现过的变形（abandoned / abandonment -> abandon）。"""

    __tablename__ = "word_forms"
    __table_args__ = (UniqueConstraint("word_id", "surface", name="uq_form"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("words.id", ondelete="CASCADE"), index=True)
    surface: Mapped[str] = mapped_column(String(80), index=True)

    word: Mapped[Word] = relationship(back_populates="forms")


class Article(Base):
    """一篇生成的文章。"""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_en: Mapped[str] = mapped_column(String(300), default="")
    title_zh: Mapped[str] = mapped_column(String(300), default="")
    topic: Mapped[str] = mapped_column(Text, default="")
    genre: Mapped[str] = mapped_column(String(80), default="")
    level: Mapped[str] = mapped_column(String(4), default="B2")
    provider: Mapped[str] = mapped_column(String(40), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    target_words: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)   # 用量、超纲率、命中率
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    sentences: Mapped[list["Sentence"]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        order_by="(Sentence.para_idx, Sentence.sent_idx)",
    )

    @property
    def word_count(self) -> int:
        return sum(len(s.en.split()) for s in self.sentences)


class Sentence(Base):
    """句子级中英对齐——整个双语覆盖交互的地基。

    对齐关系在生成阶段就由模型一次性产出，绝不事后切句再翻译：
    英文句号切分会在 Mr. / U.S. / 引号内句号上翻车，
    而事后整篇翻译时模型经常合并或拆分句子，对齐直接崩掉。
    """

    __tablename__ = "sentences"
    __table_args__ = (Index("ix_sent_article_pos", "article_id", "para_idx", "sent_idx"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    para_idx: Mapped[int] = mapped_column(Integer, default=0)
    sent_idx: Mapped[int] = mapped_column(Integer, default=0)
    en: Mapped[str] = mapped_column(Text, default="")
    zh: Mapped[str] = mapped_column(Text, default="")

    article: Mapped[Article] = relationship(back_populates="sentences")
    encounters: Mapped[list["Encounter"]] = relationship(
        back_populates="sentence", cascade="all, delete-orphan"
    )


class Encounter(Base):
    """某个词在某句话里出现过一次——所有模块共用的学习状态。

    clue / clue_strength 存的是生成时那次语境线索审计的结论。
    读者在词条面板里能看到「这一处为什么能猜出来」，
    也能一眼分辨哪些语境是真能帮你记住的、哪些只是词路过了一次。
    """

    __tablename__ = "encounters"
    __table_args__ = (UniqueConstraint("word_id", "sentence_id", name="uq_encounter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    word_id: Mapped[int] = mapped_column(ForeignKey("words.id", ondelete="CASCADE"), index=True)
    sentence_id: Mapped[int] = mapped_column(
        ForeignKey("sentences.id", ondelete="CASCADE"), index=True
    )
    surface: Mapped[str] = mapped_column(String(80), default="")
    clue: Mapped[str | None] = mapped_column(Text, nullable=True)
    clue_strength: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    word: Mapped[Word] = relationship(back_populates="encounters")
    sentence: Mapped[Sentence] = relationship(back_populates="encounters")


# ---------------------------------------------------------------------------
# 词书：照着一本纸质书录进来的顺序
#
# 三层 词书 / 单元 / 词条，对应纸质书的 书 / List / 页上的一行。分成三层不是
# 为了整齐，是因为**用户要能分别控制这三个名字**：书名、单元号、页号，
# 三者凑齐才对得上他手里那一本。
#
# 顺序是这几张表存在的**全部理由**：`BookUnit.idx` 和 `BookEntry.idx` 是录入
# 时的位置，任何查询都必须按它排。一处按别的字段排（字母序、词频、id），
# 「和纸质版一致」这条就断了，而且断得很安静——用户翻到第 42 页对不上，
# 多半只会以为自己录错了。
#
# **这几张表装的是不可再生的东西。** 词典能重新生成（跑一次
# scripts/build_wordbook_dict.py），顺序不能——那是人对着书一页页敲进去的。
# 所以它们和 Word / Encounter 一样受 data/backups/ 的启动快照保护。
#
# 留给以后的扩展：
#   · 释义等字段是**录入当时的快照**而不是每次现查词典，所以用户可以改它
#     （书上的释义和字典不一样时以书为准），重新生成词典也不会把他改的冲掉；
#   · dict_extra 是 JSON，词典以后多给几个字段（词根、例句、音频）不用改表结构；
#   · 复习记录目前压成 right / wrong / last_reviewed_at 三个数。要做遗忘曲线时
#     再加 due_at / interval 两列即可（_migrate 会自动补列）；真要逐次留痕
#     就单开一张 review 表，record_review 是唯一的写入口，改一处就够。
# ---------------------------------------------------------------------------


class WordBook(Base):
    """一本词书。名字由用户自己填，要和他书架上那本对得上。"""

    __tablename__ = "wordbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # 版次 / ISBN / 「乱序版」这类，用户自己写，程序不解释
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 这本书是怎么来的。现在只有 manual（手敲），留着是因为以后一定会有
    # 别的来路（导入文件、从文章生成的生词表反建一本），到时候要分得开。
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    units: Mapped[list["BookUnit"]] = relationship(
        back_populates="book", cascade="all, delete-orphan", order_by="BookUnit.idx"
    )


class BookUnit(Base):
    """书里的一个单元（List / Unit / 第几课）。"""

    __tablename__ = "book_units"
    __table_args__ = (UniqueConstraint("book_id", "label", name="uq_unit_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    book_id: Mapped[int] = mapped_column(
        ForeignKey("wordbooks.id", ondelete="CASCADE"), index=True
    )
    # 书里的第几个单元。**排序只认它**，不认 id——用户可能先录 List 7 再补 List 3。
    idx: Mapped[int] = mapped_column(Integer, default=0)
    # 单元号原样存字符串而不是整数："List 07"、"Unit 3-A"、"核心词 Ⅱ" 都得放得下。
    # 存成整数就等于替用户规定他的书该怎么编号。
    label: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    book: Mapped[WordBook] = relationship(back_populates="units")
    entries: Mapped[list["BookEntry"]] = relationship(
        back_populates="unit", cascade="all, delete-orphan", order_by="BookEntry.idx"
    )


class BookEntry(Base):
    """书上的一个词：它在第几页、单元里排第几，以及背到什么程度。"""

    __tablename__ = "book_entries"
    __table_args__ = (Index("ix_entry_unit_pos", "unit_id", "idx"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("book_units.id", ondelete="CASCADE"), index=True
    )
    # 单元内的顺序 = 录入顺序 = 书上的顺序。见上面那段。
    idx: Mapped[int] = mapped_column(Integer, default=0)
    # 页码。允许为空：有人只按 List 录，不关心页。
    page: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 用户敲进来的词形，**原样保留**。字典命中的原形另存在 lemma 里——
    # 书上印的是 abandoned 时，卡片正面要显示 abandoned，不是 abandon。
    headword: Mapped[str] = mapped_column(String(80), index=True)
    lemma: Mapped[str] = mapped_column(String(80), default="", index=True)

    # 以下是录入当时从词典抄来的快照，用户可以改（见上面「留给以后的扩展」）
    phonetic: Mapped[str] = mapped_column(String(120), default="")
    translation: Mapped[str] = mapped_column(Text, default="")
    inflections: Mapped[list] = mapped_column(JSON, default=list)
    derivatives: Mapped[list] = mapped_column(JSON, default=list)
    # 词典以后多给什么就往这里放，不用动表结构
    dict_extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # 用户自己写的：书上的词根拆解、联想、例句——那些东西只在他的书里，
    # 程序不去别处抓，留个地方让他填。
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 进度。用的是全应用同一套分档（STATUS_LABELS），这样词库页和背单词页
    # 说的是同一种话，不用在两处各造一套「熟练度」。
    status: Mapped[int] = mapped_column(Integer, default=STATUS_NEW, index=True)
    right: Mapped[int] = mapped_column(Integer, default=0)
    wrong: Mapped[int] = mapped_column(Integer, default=0)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---- 间隔重复（FSRS）------------------------------------------------
    # 上面那句「要做遗忘曲线时再加 due_at / interval 两列」兑现在这里。
    #
    # due_at 单独出来一列而不是塞进 fsrs 这个 JSON 里：**要按它筛**（今天该复习
    # 哪些），JSON 里的字段查不了也建不了索引。fsrs 里装的是调度器自己的状态
    # （stability / difficulty / state / step），这一层不解释它，原样存原样取——
    # 换算法或者升级库时不用动表结构。
    #
    # 两列都可空，而且**为空就是「还没进过调度」**：老库补列时是 NULL，
    # 没装 fsrs 的机器上也一直是 NULL。这两种情况下背诵页退回原来的行为
    # （按书序过一遍所有没背熟的），不报错也不丢东西。
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    fsrs: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    unit: Mapped[BookUnit] = relationship(back_populates="entries")
