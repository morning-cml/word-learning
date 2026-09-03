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


def test_启动时把虚高的计数校回事实(temp_db):
    """delete_article 改成重算只管以后——**在那之前用旧代码删掉的文章，
    把计数永久留在了虚高的状态**，而重算只会在该词条又被别的删除路径碰到时
    才发生，碰不到就一直错下去。

    错的是词条面板的「见过 N 次」和文库页的「在多个语境中见过」——
    这个应用用来说明自己有用的那个指标，而且用户看到「见过 8 次」下面
    只列出 1 处语境时，多半只会以为是别的什么意思，不会来报。
    """
    from sqlalchemy import text

    save(temp_db)
    with temp_db._engine.begin() as conn:      # 复刻旧代码留下的脏数据
        conn.execute(text("UPDATE words SET times_seen = 8"))

    temp_db.init_db()

    with temp_db.session() as s:
        w = temp_db.word_detail(s, "abandon")
    assert w["times_seen"] == len(w["contexts"]) == 1


def test_老库补出来的列是空的也能校回(temp_db):
    """模拟一个「早于 times_seen 这个字段」的老库：_migrate 会用
    ALTER TABLE ADD COLUMN 把列补回来，而补出来的列没有默认值，
    已有的行全是 NULL。

    这里必须用 IS NOT 比：NULL != 0 在 SQL 里得到的是 NULL 而不是真，
    换成 != 的话这些行会被静静地跳过，「见过几次」就永远是空的。
    """
    import sqlite3

    from sqlalchemy import text

    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("DROP COLUMN 需要 SQLite 3.35+")

    save(temp_db)
    with temp_db._engine.begin() as conn:
        conn.execute(text("ALTER TABLE words DROP COLUMN times_seen"))

    temp_db.init_db()          # _migrate 补回列（全 NULL），_reconcile_counts 填上事实

    with temp_db.session() as s:
        w = temp_db.word_detail(s, "abandon")
    assert w["times_seen"] == len(w["contexts"]) == 1


def test_校正之前一定留得下改前的快照(temp_db, tmp_path):
    """`backup.run()` 有条短路：库自上次备份后没被写过就不重复留档。

    它恰好在最该留档的那一次生效——「库没被写过」正是「这次启动才要动它」
    的典型场景，于是唯一一次真正改数据的启动反而没有当次快照兜底。
    这条断言的是：校正动手之前，改前的状态确实被留下来了。
    """
    from sqlalchemy import text

    from core.store import backup

    save(temp_db)
    with temp_db._engine.begin() as conn:            # 复刻旧代码留下的脏数据
        conn.execute(text("UPDATE words SET times_seen = 8"))

    # 造出真实场景：库自上次备份后没再被写过（用户上次打开应用之后就没生成过）
    backup.run(temp_db.DB_PATH)
    before = len(backup.snapshots(temp_db.DB_PATH))
    assert before == 1
    assert backup.run(temp_db.DB_PATH)["made"] is False, "前提：这时普通备份会跳过"

    temp_db.init_db()

    snaps = backup.snapshots(temp_db.DB_PATH)
    assert len(snaps) == before + 1, "改数据之前必须多留一份"

    # 最新那份留的是「改之前」的样子，出事能退回去
    import sqlite3

    old = sqlite3.connect(str(snaps[0])).execute(
        "select times_seen from words limit 1").fetchone()[0]
    assert old == 8, "快照留的应该是校正前的状态"
    with temp_db.session() as s:
        assert temp_db.word_detail(s, "abandon")["times_seen"] == 1


def test_库是干净的时候一行都不写(temp_db):
    """每次启动都跑，所以正常情况下必须是零写入，不能每开一次应用就动一次库。"""
    save(temp_db)
    assert temp_db._reconcile_counts() == 0


def test_删之前先算清楚要丢什么(temp_db):
    """那个「删除」按钮从来没说过它会连着语境一起删。

    文章能重新生成，累计语境不能——而且删完连「本来有多少」都查不到了。
    所以代价必须在确认之前就摆出来，尤其是 orphaned：这些词只在这一篇里
    出现过，删了等于从没学过。
    """
    from tests.test_store import DOC, META      # noqa: PLC0415

    def doc(words):
        return {**DOC, "paragraphs": [{"sentences": [
            {"en": f"The {w} was there and it stayed on.", "zh": "在。",
             "targets": [{"lemma": w, "surface": w}]} for w in words], "audits": []}]}

    with temp_db.session() as s:
        a1 = temp_db.save_article(s, doc(["abandon", "silence"]), META)
        temp_db.save_article(s, doc(["abandon", "hesitate"]), META)

    with temp_db.session() as s:
        impact = temp_db.deletion_impact(s, a1.id)
    assert impact["contexts"] == 2
    assert impact["words"] == 2
    # abandon 另一篇里也有，不算孤儿；silence 只在这一篇里
    assert impact["orphaned"] == ["silence"]


def test_没有语境的文章如实说没有(temp_db):
    from tests.test_store import DOC, META      # noqa: PLC0415

    bare = {**DOC, "paragraphs": [{"sentences": [
        {"en": "Nothing marked here.", "zh": "什么都没标。", "targets": []}], "audits": []}]}
    with temp_db.session() as s:
        art = temp_db.save_article(s, bare, META)
    with temp_db.session() as s:
        assert temp_db.deletion_impact(s, art.id) == {"contexts": 0, "words": 0, "orphaned": []}


def test_算代价不存在的文章返回_None(temp_db):
    with temp_db.session() as s:
        assert temp_db.deletion_impact(s, 12345) is None


def test_删之前一定留得下删除前的快照(temp_db):
    """和 init_db 里 _reconcile_counts 之前那次是同一条规矩：

    明知自己马上要动库的调用方必须自己传 force。放在 delete_article 里面
    而不是接口层——「每条删除路径都记得配一次」这种要求迟早会漏，
    和这个函数选择重算而不是做减法，理由是同一个。
    """
    from core.store import backup                # noqa: PLC0415
    from tests.test_store import DOC, META       # noqa: PLC0415

    with temp_db.session() as s:
        art = temp_db.save_article(s, DOC, META)
    assert backup.snapshots(temp_db.DB_PATH, tag=backup.BEFORE_DELETE) == []

    with temp_db.session() as s:
        temp_db.delete_article(s, art.id)

    saved = backup.snapshots(temp_db.DB_PATH, tag=backup.BEFORE_DELETE)
    assert len(saved) == 1
    assert temp_db.last_delete_backup()["made"] is True
    # 留的必须是**删之前**的状态
    import sqlite3                               # noqa: PLC0415
    con = sqlite3.connect(str(saved[0]))
    assert con.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    con.close()


def test_例行快照挤不掉删除前那一份(temp_db):
    """例行快照的窗口是「最近 5 次有写入的启动」，不是「最近几天」。

    误删一篇之后再生成 5 篇，删之前那份就被轮换掉了——而那正是唯一
    想找回来的一份。两种快照价值不一样，就不该抢同一批槽位。
    """
    from core.store import backup                # noqa: PLC0415
    from tests.test_store import DOC, META       # noqa: PLC0415

    with temp_db.session() as s:
        art = temp_db.save_article(s, DOC, META)
    with temp_db.session() as s:
        temp_db.delete_article(s, art.id)
    protected = backup.snapshots(temp_db.DB_PATH, tag=backup.BEFORE_DELETE)[0].name

    for _ in range(backup.KEEP * 2):             # 远超例行那条线的容量
        with temp_db.session() as s:
            temp_db.save_article(s, DOC, META)
        backup.run(temp_db.DB_PATH, force=True)

    assert len(backup.snapshots(temp_db.DB_PATH)) == backup.KEEP
    assert [p.name for p in backup.snapshots(temp_db.DB_PATH, tag=backup.BEFORE_DELETE)] == [protected]
    assert temp_db.backup_state()["protected"] == 1


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


def test_词库里的词能整份查出来(temp_db):
    """难度上限是「读者认不认得」的代用品；对词库里的词，这个应用有直接证据。

    不按 status 筛：本机词库里会被 B2 判成超纲的 8 个词**全都是 status 1**，
    按「只豁免已掌握(98)」去做在真实数据上覆盖 0 个词。status 是手动自评，
    多数词一辈子停在默认的 1；「在不在词库里」是行为，不是自评。
    Lute 也是这么算的：只有 status 0（人从没碰过）才算生词。
    """
    from tests.test_store import DOC, META      # noqa: PLC0415

    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == set()
        temp_db.save_article(s, DOC, META)

    with temp_db.session() as s:
        got = temp_db.studied_lemmas(s)
        assert got == {"abandon", "silence"}
        #  status 怎么改都还在里面——包括 99「忽略」
        temp_db.set_word_status(s, "abandon", 99)
        temp_db.set_word_status(s, "silence", 98)
    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == got


def test_改掌握程度(temp_db):
    save(temp_db)
    with temp_db.session() as s:
        r = temp_db.set_word_status(s, "abandon", 98)
    assert r["status_label"] == "已掌握"
    with temp_db.session() as s:
        assert temp_db.word_detail(s, "abandon")["status"] == 98
