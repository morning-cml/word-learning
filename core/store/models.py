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
STATUS_LEARNING = 1
STATUS_KNOWN = 98
STATUS_IGNORED = 99
STATUS_LABELS = {
    1: "刚认识", 2: "有印象", 3: "较熟", 4: "很熟", 5: "接近掌握",
    98: "已掌握", 99: "忽略",
}


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
