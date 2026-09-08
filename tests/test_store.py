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


def test_收一个不是目标词的词进词库(temp_db):
    """在这之前 Word 行只有一条来路：save_article 从目标词建。

    于是「忽略（专有名词等）」这个状态对它设计时写的那个用例是空的——
    Nora 根本进不了词库，也就无从标起。
    """
    with temp_db.session() as s:
        got = temp_db.add_word(s, "Nora")
        assert got["lemma"] == "nora" and got["status"] == 99

    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == {"nora"}
        #  没建 Encounter：这个词没出现在任何被记录的语境里，凭空造一条会把
        #  「见过 N 次」和「列得出几处语境」这条不变式弄脏（第 10 条）
        assert temp_db.word_detail(s, "nora")["contexts"] == []
        assert temp_db.word_detail(s, "nora")["times_seen"] == 0


def test_重复收同一个词只会改状态(temp_db):
    with temp_db.session() as s:
        temp_db.add_word(s, "Nora")
        temp_db.add_word(s, "nora", 98)
    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == {"nora"}
        assert temp_db.word_detail(s, "nora")["status"] == 98


@pytest.mark.parametrize("junk", ["", "   ", "123", "!!!", "x" * 81, None])
def test_收不了的词返回_None(temp_db, junk):
    """长度上限对齐 Word.lemma 的 String(80)——SQLite 不管，别的库会管。"""
    with temp_db.session() as s:
        assert temp_db.add_word(s, junk) is None
    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == set()


def test_改掌握程度(temp_db):
    save(temp_db)
    with temp_db.session() as s:
        r = temp_db.set_word_status(s, "abandon", 98)
    assert r["status_label"] == "已掌握"
    with temp_db.session() as s:
        assert temp_db.word_detail(s, "abandon")["status"] == 98


# --------------------------------------------------------------- 导出
#
# `data/app.db` 是这个项目唯一不可再生的资产，而在这之前它一个出口都没有。
# 导出这一层不改任何东西，所以它的风险不在「会不会写坏库」，
# 在**导出来的东西是不是完整的**——少了一部分不会报错，
# 而用户拿它当备份，等到需要它那天才发现缺。


def test_词库导出一行一处语境(temp_db):
    """一个词在不同故事里出现多次，正是这个产品声称最有效的机制。

    压成一行只留一句例句，等于在导出这一步把核心资产丢掉了。
    """
    import csv
    import io

    from core.store import export

    def doc(en, zh, surface):
        return {**DOC, "paragraphs": [{
            "sentences": [{"en": en, "zh": zh,
                           "targets": [{"lemma": "abandon", "surface": surface}]}],
            "audits": [{"lemma": "abandon", "strength": "strong", "clue": "空了很久"}],
        }]}

    with temp_db.session() as s:
        temp_db.save_article(s, doc("The shop was abandoned.", "小店废弃了。", "abandoned"), META)
        temp_db.save_article(s, doc("They abandon the plan.", "他们放弃计划。", "abandon"), META)
    with temp_db.session() as s:
        text = export.words_csv(s)

    assert text.startswith("\ufeff"), "少了 BOM，Excel 会把中文列读成乱码"
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0][0] == "词" and rows[0][-1] == "线索强度"
    abandon = [r for r in rows[1:] if r[0] == "abandon"]
    assert len(abandon) == 2, "两处语境要出两行，不能压成一行"
    assert {r[9] for r in abandon} == {"The shop was abandoned.", "They abandon the plan."}
    assert all(r[12] == "充分" for r in abandon), "线索强度要翻成人看得懂的"
    assert all(r[4] == "2" for r in abandon), "见过次数"
    assert all(r[5] == "2" for r in abandon), "出现在几篇"


def test_没有语境的词也要出一行(temp_db):
    """标成「忽略」的词一条 Encounter 都没有。导出时整个消失的话，
    用户下次导入 / 核对时会以为自己从没标过它。"""
    import csv
    import io

    from core.store import export

    with temp_db.session() as s:
        temp_db.add_word(s, "nora", 99)
    with temp_db.session() as s:
        rows = list(csv.reader(io.StringIO(export.words_csv(s).lstrip("\ufeff"))))
    nora = [r for r in rows[1:] if r[0] == "nora"]
    assert len(nora) == 1
    assert nora[0][3] == "忽略"
    assert nora[0][9] == "", "没有语境的那几列留空，而不是这一行不出现"


def test_导出的行序每次都一样(temp_db):
    """导出是拿去归档和 diff 的。按「最近见过」排的话，两次导出的差异里
    混着一堆纯粹的位置变动，真正改了什么反而看不出来。"""
    import csv
    import io

    from core.store import export

    save(temp_db, 2)
    with temp_db.session() as s:
        first = export.words_csv(s)
        second = export.words_csv(s)
    assert first == second
    rows = list(csv.reader(io.StringIO(first.lstrip("\ufeff"))))[1:]
    words = [r[0] for r in rows]
    assert words == sorted(words), "按词条字母序，不按最近见过"


def test_词库导出接口(client):
    r = client.get("/api/words/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("\ufeff")


def test_导出路由没被词条详情吃掉(client):
    """`/words/{lemma}` 注册在前的话，`export.csv` 会被当成一个词，
    回一个 404「词条 export.csv 不存在」——而这种错只有访问导出时才现形。"""
    r = client.get("/api/words/export.csv")
    assert r.status_code == 200, "被 /words/{lemma} 抢走了"
    assert "词条" not in r.text[:200]


# --------------------------------------------------------------- 阅读走势
#
# 这一组盯的是**按天分组的时区**。created_at 存的是 UTC，直接 .date() 就是按
# UTC 切天——东八区凌晨读的那篇会算到前一天，「连续几天」跟着断。差一整天，
# 而且没有任何报错。tz 参数就是为了让这条在 UTC 的机器上（CI 就是）也验得了：
# 拿本机时区去验本机时区永远是空跑（第 17 条）。


def _at(db, when, words=100):
    """造一篇 created_at 落在指定时刻的文章。"""
    from core.store.models import Article

    with db.session() as s:
        a = Article(title_en="x", stats={"word_count": words})
        s.add(a)
        s.flush()
        a.created_at = when.replace(tzinfo=None)     # 库里存的是 naive UTC


def test_按天分组用的是本地时区不是UTC(temp_db):
    """东八区凌晨 00:30 那篇，UTC 上是前一天 16:30。

    按 UTC 切天的话它会掉到前一天去——用户早上打开看到「昨天读了」，
    而他记得自己是今天凌晨读的。差一整天，不报错。
    """
    from datetime import datetime, timedelta, timezone

    east8 = timezone(timedelta(hours=8))
    # 东八区 2026-09-08 00:30 == UTC 2026-09-07 16:30
    when = datetime(2026, 9, 8, 0, 30, tzinfo=east8).astimezone(timezone.utc)
    assert when.date().isoformat() == "2026-09-07", "样例构造错了，两边日期得不一样"
    _at(temp_db, when)

    with temp_db.session() as s:
        east = temp_db.reading_history(s, days=3650, tz=east8)
        utc = temp_db.reading_history(s, days=3650, tz=timezone.utc)
    assert [d["date"] for d in east["days"]] == ["2026-09-08"]
    assert [d["date"] for d in utc["days"]] == ["2026-09-07"], "对照组：UTC 下确实是前一天"


def test_同一天的多篇合并成一天(temp_db):
    from datetime import timezone

    from core.store.models import utcnow

    now = utcnow()
    _at(temp_db, now, 210)
    _at(temp_db, now, 180)
    with temp_db.session() as s:
        h = temp_db.reading_history(s, tz=timezone.utc)
    assert len(h["days"]) == 1
    assert h["days"][0]["words"] == 390 and h["days"][0]["articles"] == 2
    assert h["total_words"] == 390


def test_累计值是一路加上去的(temp_db):
    from datetime import timedelta, timezone

    from core.store.models import utcnow

    now = utcnow()
    for d, w in ((4, 100), (2, 200), (0, 300)):
        _at(temp_db, now - timedelta(days=d), w)
    with temp_db.session() as s:
        h = temp_db.reading_history(s, tz=timezone.utc)
    assert [d["words"] for d in h["days"]] == [100, 200, 300]
    assert [d["total"] for d in h["days"]] == [100, 300, 600]


def test_连续天数断在空档上(temp_db):
    """连续几天是这一块唯一有「坚持」含义的数字，多算一天就是在骗人。"""
    from datetime import timedelta, timezone

    from core.store.models import utcnow

    now = utcnow()
    for d in (0, 1, 2, 5):          # 今天、昨天、前天连着；第 5 天单独一天
        _at(temp_db, now - timedelta(days=d))
    with temp_db.session() as s:
        assert temp_db.reading_history(s, tz=timezone.utc)["streak"] == 3


def test_今天还没读时从昨天往回数(temp_db):
    """不这么算的话，早上打开应用看到的永远是 0——而昨天明明读了。"""
    from datetime import timedelta, timezone

    from core.store.models import utcnow

    now = utcnow()
    for d in (1, 2):
        _at(temp_db, now - timedelta(days=d))
    with temp_db.session() as s:
        assert temp_db.reading_history(s, tz=timezone.utc)["streak"] == 2


def test_老文章没有字数记录时如实报出来(temp_db):
    """早期文章没存过 word_count。算 0 但不说的话，那几天画出来是空的，
    看着像那天没读——而他明明读了。"""
    from datetime import timezone

    from core.store.models import Article, utcnow

    _at(temp_db, utcnow(), 200)
    with temp_db.session() as s:
        s.add(Article(title_en="old", stats={}))
    with temp_db.session() as s:
        h = temp_db.reading_history(s, tz=timezone.utc)
    assert h["missing_word_count"] == 1
    assert h["total_words"] == 200


def test_空库不报错(temp_db):
    from datetime import timezone

    with temp_db.session() as s:
        h = temp_db.reading_history(s, tz=timezone.utc)
    assert h == {"days": [], "streak": 0, "total_words": 0,
                 "total_articles": 0, "missing_word_count": 0}


def test_阅读走势接口(client):
    r = client.get("/api/reading/history?days=30")
    assert r.status_code == 200
    assert set(r.json()) == {"days", "streak", "total_words",
                             "total_articles", "missing_word_count"}
    # days 收敛到合理范围，别让一个 days=99999 把整库扫成一张巨表
    assert client.get("/api/reading/history?days=0").status_code == 200
    assert client.get("/api/reading/history?days=100000").status_code == 200
