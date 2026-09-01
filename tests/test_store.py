"""词条 / 文章 / 学习状态的持久化。

这里的每一条都关系到同一件事：**库是这个应用唯一不可再生的资产**。
文章能重新生成，掌握程度和累计语境不能——它们是一次次阅读攒出来的。
所以计数错了、词条并错了都不会报错，只会安静地把这份资产变脏。
"""
from __future__ import annotations

import pytest

DOC = {
    "title_en": "The Quiet Shop", "title_zh": "安静的小店",
    "topic": "一间废弃的小店", "genre": "短篇小说",
    "paragraphs": [{
        "sentences": [
            {"en": "The shop was abandoned.", "zh": "小店废弃了。",
             "targets": [{"lemma": "abandon", "surface": "abandoned"}]},
            {"en": "Silence filled the room.", "zh": "寂静充满房间。",
             "targets": [{"lemma": "silence", "surface": "Silence"}]},
        ],
        "audits": [{"lemma": "abandon", "strength": "strong", "clue": "空了很久"}],
    }],
}

META = {"level": "B2", "provider": "deepseek", "model": "m",
        "target_words": ["abandon", "silence"], "stats": {"word_count": 8},
        "glossary": {"abandon": "v. 抛弃"}}


def save(db, n=1):
    ids = []
    for _ in range(n):
        with db.session() as s:
            ids.append(db.save_article(s, DOC, META).id)
    return ids


def test_落库与回读(temp_db):
    (aid,) = save(temp_db)
    with temp_db.session() as s:
        doc = temp_db.article_to_doc(temp_db.get_article(s, aid))

    assert doc["title_en"] == "The Quiet Shop"
    assert len(doc["paragraphs"]) == 1
    assert len(doc["paragraphs"][0]["sentences"]) == 2
    assert doc["paragraphs"][0]["sentences"][0]["targets"] == [
        {"lemma": "abandon", "surface": "abandoned"}]


def test_句子按段落与句序排列(temp_db):
    """乱序插入也要按 (para_idx, sent_idx) 读出来，否则文章会串行。"""
    from core.store.models import Article, Sentence

    with temp_db.session() as s:
        a = Article(title_en="X")
        s.add(a)
        s.flush()
        for p, i in [(2, 1), (0, 1), (1, 0), (0, 0), (2, 0), (1, 1)]:
            s.add(Sentence(article_id=a.id, para_idx=p, sent_idx=i, en=f"p{p}s{i}", zh="z"))
        aid = a.id

    with temp_db.session() as s:
        doc = temp_db.article_to_doc(temp_db.get_article(s, aid))
    assert [[x["en"] for x in p["sentences"]] for p in doc["paragraphs"]] == [
        ["p0s0", "p0s1"], ["p1s0", "p1s1"], ["p2s0", "p2s1"]]


def test_释义与词形挂到词条上(temp_db):
    save(temp_db)
    with temp_db.session() as s:
        w = temp_db.word_detail(s, "abandon")
    assert w["gloss"] == "v. 抛弃"
    assert w["forms"] == ["abandoned"]
    assert w["contexts"][0]["clue_strength"] == "strong"


def test_同一篇里重复的词只记一次语境(temp_db):
    """Encounter 对 (word_id, sentence_id) 唯一，同句出现两次不该记两笔。"""
    save(temp_db)
    with temp_db.session() as s:
        assert temp_db.word_detail(s, "abandon")["times_seen"] == 1


@pytest.mark.parametrize("n", [1, 3])
def test_删文章后计数按事实重算(temp_db, n):
    """times_seen 是一次次加出来的，删文章时却没人减。

    放着不管，词条面板会显示「见过 3 次」却只列得出 1 处语境，
    文库页的「在多个语境中见过」也会一直虚高——而那正是这个应用
    用来说明自己有用的那个指标。
    """
    ids = save(temp_db, n)
    for i, aid in enumerate(ids):
        with temp_db.session() as s:
            assert temp_db.delete_article(s, aid) is True
        with temp_db.session() as s:
            w = temp_db.word_detail(s, "abandon")
            assert w["times_seen"] == len(w["contexts"]) == n - i - 1


def test_删光之后词条本身保留(temp_db):
    """掌握程度是用户攒出来的学习状态，不该跟着文章一起没。"""
    (aid,) = save(temp_db)
    with temp_db.session() as s:
        temp_db.set_word_status(s, "abandon", 98)
    with temp_db.session() as s:
        temp_db.delete_article(s, aid)
    with temp_db.session() as s:
        w = temp_db.word_detail(s, "abandon")
    assert w is not None and w["status"] == 98 and w["times_seen"] == 0


def test_删不存在的文章返回_False(temp_db):
    with temp_db.session() as s:
        assert temp_db.delete_article(s, 999999) is False


def test_跨文章累计语境(temp_db):
    """同一个词在不同故事里反复出现，是这个应用真正的记忆杠杆。"""
    save(temp_db, 2)
    with temp_db.session() as s:
        w = temp_db.word_detail(s, "abandon")
        stats = temp_db.word_stats(s)
    assert w["times_seen"] == 2 and w["distinct_articles"] == 2
    assert stats["seen_multi"] == 2 and stats["seen_once"] == 0


def test_词条按用户给的词存不改名(temp_db):
    """用户说要学 better，词条面板却标着 good 就是错的。"""
    with temp_db.session() as s:
        assert temp_db.get_or_create_word(s, "better").lemma == "better"
        assert temp_db.get_or_create_word(s, "people").lemma == "people"
        assert temp_db.get_or_create_word(s, "ran").lemma == "run"      # 屈折形态照旧归并


def test_改掌握程度(temp_db):
    save(temp_db)
    with temp_db.session() as s:
        r = temp_db.set_word_status(s, "abandon", 98)
    assert r["status_label"] == "已掌握"
    with temp_db.session() as s:
        assert temp_db.word_detail(s, "abandon")["status"] == 98
