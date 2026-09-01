"""SQLite 连接与常用查询。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from core.lexicon import cefr
from core.store import backup
from core.store.models import (
    Article, Base, Encounter, Sentence, Word, WordForm, utcnow,
)

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "app.db"

_engine = create_engine(
    f"sqlite:///{DB_PATH}",
    future=True,
    connect_args={"check_same_thread": False},
)
_Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)


# 一个词条实际有多少条语境。times_seen 应该永远等于它。
_ACTUAL = "(SELECT COUNT(*) FROM encounters WHERE encounters.word_id = words.id)"

# 最近一次备份的结果，供设置页显示。备份失败不该弹窗打断启动，
# 但也不能全无声息——用户会以为自己有备份，其实没有。
_backup_state: dict = {"ok": True, "made": False, "count": 0, "latest": "", "error": ""}


def backup_state() -> dict:
    return dict(_backup_state)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 先留快照再动结构：紧跟着的 _migrate() 会 ALTER TABLE，
    # 是这条路径上最可能把老库改坏的一步，要留的正是它动手之前的状态。
    _backup_state.update(backup.run(DB_PATH))
    Base.metadata.create_all(_engine)
    _migrate()

    # 先数、再备份、最后才改。顺序是有讲究的：上面那次 backup.run() 会因为
    # 「库自上次备份后没被写过」而跳过，而那恰好就是这次要动库的场景——
    # 于是唯一一次真正改数据的启动反而没有当次快照兜底。
    stale = _count_stale()
    if stale:
        _backup_state.update(backup.run(DB_PATH, force=True))
        fixed = _reconcile_counts()
        # 悄悄改用户的数据是这个项目最不该干的事，所以改了就说一声。
        print(f"[数据] 已把 {fixed} 个词条的「见过几次」校回实际语境条数"
              f"（改前的库已留档在 data/backups/）")


def _migrate() -> None:
    """给已存在的表补新增的列。

    create_all 只建表不改表，加字段后老库会缺列。这里做最小化处理：
    对比模型定义和实际表结构，缺哪个补哪个。够用到需要真正的迁移工具为止。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(_engine)
    with _engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(_engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
                )


def _count_stale() -> int:
    """有多少个词条的 times_seen 和事实对不上。只读，不写。

    单独分出来是为了让 init_db 能「先知道要不要动库，再决定要不要强制留档」。
    """
    from sqlalchemy import text

    with _engine.connect() as conn:
        return conn.execute(text(
            f"SELECT COUNT(*) FROM words WHERE times_seen IS NOT {_ACTUAL}"
        )).scalar_one()


def _reconcile_counts() -> int:
    """把 times_seen 校回事实：它必须等于该词条实际的语境条数。

    这个数字只有一个来源——record_encounter 每插一条 Encounter 加一次。
    所以「见过几次」和「列得出几处语境」永远应该相等，不等就是数据脏了。

    delete_article 已经改成按剩下的语境重算，但那只管以后：**在那之前用旧代码
    删掉的文章，把计数永久留在了虚高的状态**，而重算只会在该词条又被别的删除
    路径碰到时才发生——碰不到就一直错下去。本机库里 24 个词条有 5 个对不上
    （abandon 显示 10 次，实际只有 3 处语境）。

    这条不是小数点问题：times_seen 就是词条面板的「见过 N 次」和文库页的
    「在多个语境中见过」——这个应用用来说明自己有用的那个指标。而且它没有
    反馈回路，用户看到「见过 8 次」下面只列出 1 处语境时，多半只会以为
    是别的什么意思。

    放在 _migrate() 之后：init_db 第一步已经留好启动快照，出事能退回去。
    只更新对不上的行，所以库是干净的时候这是一条零写入的语句，每次启动跑不亏。
    last_seen_at 有意不动——它对不上只影响词库页的排序，而「这个时间到底
    该表示什么」是另一个问题，不该顺手在这里替用户定了。
    """
    from sqlalchemy import text

    with _engine.begin() as conn:
        # IS NOT 而不是 !=：times_seen 可能是 NULL（老库补列时没有默认值），
        # 而 NULL != 0 在 SQL 里是 NULL，不是真，那行就会被静静地跳过。
        return conn.execute(text(
            f"UPDATE words SET times_seen = {_ACTUAL} WHERE times_seen IS NOT {_ACTUAL}"
        )).rowcount


@contextmanager
def session() -> Iterator[Session]:
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ------------------------------------------------------------------- 词条操作

def get_or_create_word(s: Session, lemma: str) -> Word:
    key = cefr.resolve(lemma)
    word = s.scalar(select(Word).where(Word.lemma == key))
    if word is None:
        word = Word(lemma=key, cefr=cefr.level_of(key))
        s.add(word)
        s.flush()
    return word


def record_form(s: Session, word: Word, surface: str) -> None:
    """把文中出现的变形挂到父词条下（Lute 的 parent term 思路）。"""
    sfc = (surface or "").strip().lower()
    if not sfc or sfc == word.lemma:
        return
    exists = s.scalar(
        select(WordForm).where(WordForm.word_id == word.id, WordForm.surface == sfc)
    )
    if exists is None:
        s.add(WordForm(word_id=word.id, surface=sfc))


def record_encounter(s: Session, word: Word, sentence: Sentence, surface: str,
                     audit: dict | None = None) -> None:
    exists = s.scalar(
        select(Encounter).where(
            Encounter.word_id == word.id, Encounter.sentence_id == sentence.id
        )
    )
    if exists is not None:
        return
    audit = audit or {}
    s.add(Encounter(
        word_id=word.id, sentence_id=sentence.id, surface=surface,
        clue=audit.get("clue") or None,
        clue_strength=(audit.get("strength") or "").lower() or None,
    ))
    word.times_seen = (word.times_seen or 0) + 1
    word.last_seen_at = utcnow()


# ------------------------------------------------------------------ 文章持久化

def save_article(s: Session, doc: dict, meta: dict) -> Article:
    """把生成结果落库，同时更新所有相关词条的学习状态。"""
    article = Article(
        title_en=doc.get("title_en", ""),
        title_zh=doc.get("title_zh", ""),
        topic=doc.get("topic", ""),
        genre=doc.get("genre", ""),
        level=meta.get("level", "B2"),
        provider=meta.get("provider", ""),
        model=meta.get("model", ""),
        target_words=meta.get("target_words", []),
        stats=meta.get("stats", {}),
    )
    s.add(article)
    s.flush()

    glossary = {k.lower(): v for k, v in (meta.get("glossary") or {}).items()}

    for p_i, para in enumerate(doc.get("paragraphs", [])):
        # 这一段的线索审计结论，按词挂到对应的 encounter 上
        audits = {
            (a.get("lemma") or "").lower(): a for a in (para.get("audits") or [])
        }
        for s_i, sent in enumerate(para.get("sentences", [])):
            row = Sentence(
                article_id=article.id, para_idx=p_i, sent_idx=s_i,
                en=sent.get("en", ""), zh=sent.get("zh", ""),
            )
            s.add(row)
            s.flush()
            for tgt in sent.get("targets", []):
                lemma = (tgt.get("lemma") or "").strip()
                if not lemma:
                    continue
                word = get_or_create_word(s, lemma)
                if not word.gloss and lemma.lower() in glossary:
                    word.gloss = glossary[lemma.lower()]
                record_form(s, word, tgt.get("surface", ""))
                record_encounter(s, word, row, tgt.get("surface", ""),
                                 audits.get(lemma.lower()))
    return article


def delete_article(s: Session, article_id: int) -> bool:
    """删掉一篇文章，并按剩下的语境重算受影响词条的计数。

    times_seen 是一次次加出来的，删文章时却没人减。放着不管，词条面板会
    显示「见过 3 次」却只列得出 1 处语境，文库页的「在多个语境中见过」
    也会一直虚高——而那正是这个应用用来说明自己有用的那个指标。

    重算而不是逐个减：减法要求每条删除路径都记得配一次，重算只依赖
    删完之后的事实，以后再多几条删除路径也不会重新错。
    """
    article = s.get(Article, article_id)
    if article is None:
        return False

    affected = set(s.scalars(
        select(Encounter.word_id)
        .join(Sentence, Encounter.sentence_id == Sentence.id)
        .where(Sentence.article_id == article_id)
    ))
    s.delete(article)
    s.flush()               # 先让级联删除落到库里，再按剩下的重算

    for word_id in affected:
        word = s.get(Word, word_id)
        if word is None:
            continue
        rows = list(s.scalars(
            select(Encounter).where(Encounter.word_id == word_id).order_by(Encounter.id)
        ))
        word.times_seen = len(rows)
        # 按 id 取最后一条，不比较 datetime：库里读出来的是 naive，
        # 本次会话新建的是 aware，混在一起比较会直接抛 TypeError。
        word.last_seen_at = rows[-1].created_at if rows else None
    return True


def list_articles(s: Session, limit: int = 50) -> list[Article]:
    return list(
        s.scalars(select(Article).order_by(Article.created_at.desc()).limit(limit))
    )


def get_article(s: Session, article_id: int) -> Article | None:
    return s.get(Article, article_id)


def article_to_doc(article: Article) -> dict:
    """把库里的文章还原成阅读器要的嵌套结构。"""
    paras: dict[int, list[dict]] = {}
    for sent in article.sentences:
        paras.setdefault(sent.para_idx, []).append(
            {
                "id": sent.id,
                "en": sent.en,
                "zh": sent.zh,
                "targets": [
                    {"lemma": e.word.lemma, "surface": e.surface} for e in sent.encounters
                ],
            }
        )
    return {
        "id": article.id,
        "title_en": article.title_en,
        "title_zh": article.title_zh,
        "topic": article.topic,
        "genre": article.genre,
        "level": article.level,
        "provider": article.provider,
        "model": article.model,
        "target_words": article.target_words or [],
        "stats": article.stats or {},
        "created_at": article.created_at.isoformat() if article.created_at else "",
        "paragraphs": [
            {"sentences": paras[k]} for k in sorted(paras)
        ],
    }


def word_detail(s: Session, lemma: str) -> dict | None:
    """一个词的全部语境——跨所有文章。

    这是整个应用里最直接服务记忆的一个查询：同一个词在不同故事里的多次出现，
    比在单词书上重复看十遍有效得多（varied context / 多语境重复）。
    每条语境都带着当时的线索审计结论，能一眼看出哪次是真能帮你记住的。
    """
    word = s.scalar(select(Word).where(Word.lemma == cefr.resolve(lemma)))
    if word is None:
        return None

    contexts = []
    for enc in sorted(word.encounters, key=lambda e: e.id):
        sent = enc.sentence
        if sent is None:
            continue
        art = sent.article
        contexts.append({
            "article_id": art.id if art else None,
            "article_title": art.title_en if art else "",
            "surface": enc.surface,
            "en": sent.en,
            "zh": sent.zh,
            "clue": enc.clue,
            "clue_strength": enc.clue_strength,
        })

    return {
        "lemma": word.lemma,
        "cefr": word.cefr,
        "gloss": word.gloss,
        "status": word.status,
        "status_label": word.status_label,
        "times_seen": word.times_seen or 0,
        "forms": sorted({f.surface for f in word.forms}),
        "contexts": contexts,
        "distinct_articles": len({c["article_id"] for c in contexts}),
    }


def set_word_status(s: Session, lemma: str, status: int) -> dict | None:
    word = s.scalar(select(Word).where(Word.lemma == cefr.resolve(lemma)))
    if word is None:
        return None
    word.status = status
    return {"lemma": word.lemma, "status": word.status, "status_label": word.status_label}


def word_stats(s: Session) -> dict:
    words = list(s.scalars(select(Word)))
    return {
        "total": len(words),
        "seen_once": sum(1 for w in words if (w.times_seen or 0) == 1),
        "seen_multi": sum(1 for w in words if (w.times_seen or 0) > 1),
    }
