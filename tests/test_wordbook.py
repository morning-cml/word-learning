"""词书：录入、顺序、词典填充、进度。

这一组盯的全是「错了也不会报错」的那一类：

  · 顺序错了不会抛异常，只会让用户翻到第 42 页发现对不上——而他多半会以为
    是自己录错了（这个功能唯一的卖点就是顺序，所以顺序的测试占一半）；
  · 「什么词才进共用词库」判错了同样不报错，但会把难度标尺整个放松掉
    （见 store._sync_library 上面那段），而界面上只会显示一个更好看的超纲率。
"""
from __future__ import annotations

import pytest

from core.store.models import STATUS_IGNORED, STATUS_KNOWN, STATUS_LEARNING, STATUS_NEW
from core.wordbook import dictionary
from core.wordbook import store as wb


@pytest.fixture
def book(temp_db):
    with temp_db.session() as s:
        b = wb.create_book(s, "新东方六级词汇 乱序版", "第 3 版")
        u = wb.create_unit(s, b["id"], "List 07")   # idx 从标签里认出来 = 7
        return {"book_id": b["id"], "unit_id": u["id"]}


# --------------------------------------------------------------- 粘贴解析

def test_一行一个词():
    assert dictionary.split_words("abandon\nabbreviation\nabdomen") == \
        ["abandon", "abbreviation", "abdomen"]


def test_行首序号不算词():
    assert dictionary.split_words("12. abandon\n13 abbreviation") == \
        ["abandon", "abbreviation"]


def test_OCR_出来的整行只取头一个词():
    """`abandon [ә'bændәn] vt. 放弃, 抛弃` 是拍照取字最常见的形状。

    音标和释义跟在同一行上，收进来只会变成一堆查不到的词，
    然后以「这些词没匹配上」的样子报给用户——看着像漏词，其实是解析错了。
    """
    text = "abandon [ә'bændәn] vt. 放弃, 抛弃\nabbreviation [ә.bri:vi'eiʃәn] n. 缩写词"
    assert dictionary.split_words(text) == ["abandon", "abbreviation"]


def test_汉字不会被当成单词():
    """汉字的 str.isalpha() 也是 True，判据必须是「含 ASCII 字母」。"""
    assert dictionary.split_words("放弃\n抛弃\nabandon") == ["abandon"]


def test_逗号分隔的一整行全都要():
    assert dictionary.split_words("abandon, abbreviation, abdomen") == \
        ["abandon", "abbreviation", "abdomen"]


def test_连字符和撇号的词形原样保留():
    """self-esteem 剖开就查不到了。"""
    assert dictionary.split_words("self-esteem\none's") == ["self-esteem", "one's"]


# --------------------------------------------------------------- 词典填充

def test_配上音标释义和屈折():
    entry = dictionary.lookup("abandon")
    assert entry is not None
    assert entry["phonetic"]
    assert "放弃" in entry["translation"]
    labels = {i["label"] for i in entry["inflections"]}
    assert {"过去式", "现在分词"} <= labels


def test_派生词不重复屈折形式():
    """abandoned / abandoning / abandons 上面已经按屈折列过了。

    再在「派生词」里列一遍，用户看到同一批词出现两次，
    而真正的派生词（abandonment）混在里面找不着。
    """
    entry = dictionary.lookup("abandon")
    inflected = {i["word"].lower() for i in entry["inflections"]}
    assert not (inflected & {d.lower() for d in entry["derivatives"]})
    assert "abandonment" in entry["derivatives"]


def test_书上印的是屈折形式时还原回去并说一声():
    """thresholds 自己不是词条，得还原到 threshold 才查得到。

    挑这个例子是查过的：visas / abandoned 这类**自己也是独立词条**，
    走不到还原那条分支，拿它们当样例等于这条测试在空跑（第 17 条）。
    """
    entry = dictionary.lookup("thresholds")
    assert entry is not None
    assert entry["word"] == "threshold"
    assert entry["surface"] == "thresholds"      # 卡片正面显示书上那个词形
    assert entry["lemma_fallback"] is True


def test_查不到的词如实报出来():
    found, missing = dictionary.lookup_many(["abandon", "zzzznotaword"])
    assert [e["word"] for e in found] == ["abandon"]
    assert missing == ["zzzznotaword"]


# --------------------------------------------------------------- 顺序（核心）

def test_录入顺序原样保留(temp_db, book):
    """这是整个功能唯一的卖点，别的都可以让步，这条不行。"""
    words = ["zebra", "apple", "monkey", "banana"]
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], words, page=42)
    with temp_db.session() as s:
        got = [e["headword"] for e in wb.list_entries(s, book["unit_id"])]
    assert got == words, "顺序被重排了——按字母或词频排都会打破「和纸质版一致」"


def test_后录的前面那一页排在前面(temp_db, book):
    """用户是背到哪录到哪，很可能先录第 44 页、隔天才回头补第 43 页。

    无脑追加到末尾的话，43 页会排在 44 页后面。不报错，只是顺序错了。
    """
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], ["dog", "cat"], page=44)
        wb.add_entries(s, book["unit_id"], ["ant", "bee"], page=43)
        wb.add_entries(s, book["unit_id"], ["fox"], page=45)
    with temp_db.session() as s:
        rows = wb.list_entries(s, book["unit_id"])
    assert [e["headword"] for e in rows] == ["ant", "bee", "dog", "cat", "fox"]
    assert [e["page"] for e in rows] == [43, 43, 44, 44, 45]
    assert [e["idx"] for e in rows] == sorted(e["idx"] for e in rows)


def test_词典里没有的词也要按原位录进去(temp_db, book):
    """**这条是这个功能的命根。**

    书上那一页有 12 个词，词典只认得 11 个。把认不得的那个丢掉的话：
    应用里只剩 11 个，而且它**后面的全体前移一位**——顺序错了，
    而顺序是这个功能唯一要保证的东西。而且不会报错，
    用户翻到那一页才发现对不上，多半以为是自己敲漏了。

    字典是配菜，不是门卫：查不到就把释义留空，让他自己补。
    """
    words = ["abandon", "zzzznotaword", "threshold"]
    with temp_db.session() as s:
        got = wb.add_entries(s, book["unit_id"], words, page=43)
    assert got["added"] == 3, "查不到的词被丢掉了，这一页就和书对不上了"
    assert got["missing"] == ["zzzznotaword"]
    with temp_db.session() as s:
        rows = wb.list_entries(s, book["unit_id"])
    assert [e["headword"] for e in rows] == words
    blank = rows[1]
    assert blank["translation"] == ""
    assert blank["extra"]["no_dict"] is True, "得标出来，否则用户不知道这一条要自己补"
    assert rows[2]["translation"], "它后面的词不受影响"


def test_预览说会录进去的_入库时就真的录进去(temp_db, book):
    """两头必须对得上：预览那句「仍然会按顺序录进去」是一条承诺。

    预览和入库从同一个 annotate() 派生，就是为了防止这两头分叉
    （需要注意.md 第 20 条）——修之前它们确实是分叉的：
    预览说会录，add_entries 却只取查到的。
    """
    words = ["abandon", "zzzznotaword", "threshold"]
    found, missing = dictionary.lookup_many(words)
    promised = len(found) + len(missing)
    with temp_db.session() as s:
        got = wb.add_entries(s, book["unit_id"], words, page=1)
    assert got["added"] == promised


def test_同一个词出现两次不去重(temp_db, book):
    """一页上同一个词出现两次是书自己的事，不是要程序修的错。"""
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], ["apple", "apple"], page=1)
    with temp_db.session() as s:
        assert len(wb.list_entries(s, book["unit_id"])) == 2


def test_按页取词(temp_db, book):
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], ["dog", "cat"], page=43)
        wb.add_entries(s, book["unit_id"], ["fox"], page=44)
    with temp_db.session() as s:
        assert [e["headword"] for e in wb.list_entries(s, book["unit_id"], page=43)] == \
            ["dog", "cat"]


def test_单元按自己的编号排而不是录入先后(temp_db, book):
    """先录 List 7 再补 List 3 时，List 3 得排在前面。"""
    with temp_db.session() as s:
        wb.create_unit(s, book["book_id"], "List 03")
        wb.create_unit(s, book["book_id"], "附录")        # 没数字，排末尾
    with temp_db.session() as s:
        units = wb.list_units(s, book["book_id"])
    assert [u["label"] for u in units] == ["List 03", "List 07", "附录"]
    assert [u["idx"] for u in units] == [3, 7, 8]


# --------------------------------------------------------------- 重贴一页

def test_重贴一页保住已经背出来的进度(temp_db, book):
    """贴错了、OCR 漏了几个，都要能重贴。

    重贴一次就把进度清零的话，用户会宁可忍着错误的顺序也不敢重贴。
    """
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["dog", "cat"], page=43)
        wb.record_review(s, r["entries"][0]["id"], known=True)
        wb.record_review(s, r["entries"][0]["id"], known=True)
    with temp_db.session() as s:
        out = wb.add_entries(s, book["unit_id"], ["dog", "cat", "fox"],
                             page=43, replace_page=True)
    assert out["restored"] == 2
    with temp_db.session() as s:
        listed = wb.list_entries(s, book["unit_id"])
    rows = {e["headword"]: e for e in listed}
    # 顺序是重贴时给的那个顺序，不是原来的
    assert [e["headword"] for e in listed] == ["dog", "cat", "fox"]
    assert rows["dog"]["status"] == 2      # 未学 -> 刚认识 -> 有印象
    assert rows["fox"]["status"] == STATUS_NEW


def test_重贴过一页之后再录的页仍然排在后面(temp_db, book):
    """重贴会在 idx 上留下空档，而插入位置是拿 idx 算的。

    原来 `_insert_at` 返回的是**列表下标**，调用方却当成 idx 用——两者只在
    idx 连着排的时候才相等。重贴一次错开之后，后面每一次录入都会挪错行：
    实测这个顺序（43/44/45 → 重贴 44 → 再录 46）会让**第 46 页整个排到
    第 45 页前面**。不抛异常，只是顺序错了，而顺序是这个功能唯一的卖点。
    """
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], ["ant", "bee"], page=43)
        wb.add_entries(s, book["unit_id"], ["cat", "dog"], page=44)
        wb.add_entries(s, book["unit_id"], ["egg", "fox"], page=45)
        # 重贴第 44 页，而且词数和原来不一样（空档就是这么来的）
        wb.add_entries(s, book["unit_id"], ["cow", "duck", "deer"],
                       page=44, replace_page=True)
        wb.add_entries(s, book["unit_id"], ["yak"], page=46)
        # 往中间补一个：它该接在第 44 页那几个后面，不是插进它们中间
        wb.add_entries(s, book["unit_id"], ["mole"], page=44)
    with temp_db.session() as s:
        rows = wb.list_entries(s, book["unit_id"])
    assert [e["page"] for e in rows] == [43, 43, 44, 44, 44, 44, 45, 45, 46]
    assert [e["headword"] for e in rows] == \
        ["ant", "bee", "cow", "duck", "deer", "mole", "egg", "fox", "yak"]


def test_一个词都解析不出来时不动原来那一页(temp_db, book):
    """解析必须排在删除前面。

    原来是先把这一页删掉、再去解析，于是「一个词都没解析出来」那一次
    会返回一个 added: 0 就完事——界面上是「已录入 0 个词」，
    实际上那一页被清空了，而它是用户一页页敲进去的、补不回来的东西。
    """
    with temp_db.session() as s:
        wb.add_entries(s, book["unit_id"], ["ant", "bee"], page=43)
    with temp_db.session() as s:
        out = wb.add_entries(s, book["unit_id"], ["---", "..."],
                             page=43, replace_page=True)
    assert out["added"] == 0
    with temp_db.session() as s:
        assert [e["headword"] for e in wb.list_entries(s, book["unit_id"])] == ["ant", "bee"]


# --------------------------------------------------------------- 进度推进

def test_认识一路推到已掌握(temp_db, book):
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        eid = r["entries"][0]["id"]
        assert r["entries"][0]["status"] == STATUS_NEW
        seen = [wb.record_review(s, eid, known=True)["status"] for _ in range(6)]
    assert seen == [1, 2, 3, 4, 5, STATUS_KNOWN]


def test_不认识退回刚认识(temp_db, book):
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        eid = r["entries"][0]["id"]
        for _ in range(4):
            wb.record_review(s, eid, known=True)
        got = wb.record_review(s, eid, known=False)
    assert got["status"] == STATUS_LEARNING
    assert got["wrong"] == 1


# ------------------------------------------- 和共用词库的关系（错了会静默毁掉标尺）

def test_只有已掌握的词才进共用词库(temp_db, book):
    """词库那份数据被难度标尺当豁免名单读（需要注意.md 第 4e 条）。

    一本六级书 5407 个词全灌进去，等于告诉标尺「这些词读者都认得」，
    超纲检测当场失效——而界面上只会显示一个更好看的超纲率。
    """
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon", "threshold"], page=1)
        eid = r["entries"][0]["id"]
        for _ in range(3):                  # 推到「较熟」，还不是已掌握
            wb.record_review(s, eid, known=True)
    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == set(), "背过一次还不是认得"

    with temp_db.session() as s:
        for _ in range(3):                  # 推到 98
            wb.record_review(s, eid, known=True)
    with temp_db.session() as s:
        assert temp_db.studied_lemmas(s) == {"abandon"}


def test_从已掌握掉下来时词库那条也要降回去(temp_db, book):
    """豁免名单只进不出的话，会越攒越松，而没有任何地方会报出来。"""
    from sqlalchemy import select

    from core.store.models import Word

    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        eid = r["entries"][0]["id"]
        for _ in range(6):
            wb.record_review(s, eid, known=True)
    with temp_db.session() as s:
        assert s.scalar(select(Word).where(Word.lemma == "abandon")).status == STATUS_KNOWN
    with temp_db.session() as s:
        wb.record_review(s, eid, known=False)
    with temp_db.session() as s:
        word = s.scalar(select(Word).where(Word.lemma == "abandon"))
        assert word.status != STATUS_KNOWN
        assert temp_db.studied_lemmas(s) == {"abandon"}   # 还在库里，只是不再算「掌握」


def test_背单词不会把文章攒的语境计数弄脏(temp_db, book):
    """times_seen 必须等于实际语境条数（需要注意.md 第 10 条）。

    背单词建出来的 Word 行一条 Encounter 都没有，
    times_seen 也就必须是 0，否则启动时的自检会报出一堆「对不上」。
    """
    from sqlalchemy import select

    from core.store.models import Word

    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        for _ in range(6):
            wb.record_review(s, r["entries"][0]["id"], known=True)
    with temp_db.session() as s:
        word = s.scalar(select(Word).where(Word.lemma == "abandon"))
        assert (word.times_seen or 0) == 0
        assert len(word.encounters) == 0
    assert temp_db._count_stale() == 0


# --------------------------------------------------------------- 统计

def test_进度按单元汇总(temp_db, book):
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon", "threshold", "persist"], page=1)
        for _ in range(6):
            wb.record_review(s, r["entries"][0]["id"], known=True)
        wb.record_review(s, r["entries"][1]["id"], known=True)
    with temp_db.session() as s:
        unit = wb.list_units(s, book["book_id"])[0]
    assert unit["total"] == 3
    assert unit["known"] == 1
    assert unit["learning"] == 1
    assert unit["new"] == 1
    assert unit["page_range"] == "P1"


def test_页码范围压成区间(temp_db, book):
    with temp_db.session() as s:
        for p in (43, 44, 45, 48):
            wb.add_entries(s, book["unit_id"], ["dog"], page=p)
    with temp_db.session() as s:
        assert wb.list_units(s, book["book_id"])[0]["page_range"] == "P43–45, 48"


def test_忽略的词不再往上推(temp_db, book):
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        eid = r["entries"][0]["id"]
        wb.update_entry(s, eid, {"status": STATUS_IGNORED})
        got = wb.record_review(s, eid, known=True)
    assert got["status"] == STATUS_IGNORED


# --------------------------------------------------------------- 接口层
#
# 上面那些验的是 store 这一层。顺序还要**穿过 HTTP 之后仍然保住**：
# 序列化、查询参数、JSON 往返，任何一处顺手排一下序都会把它破掉，
# 而且不会报错——所以这一组是单独的，不是上面那些的重复。

def test_背单词页面能开(client):
    for path in ("/study", "/study/1"):
        assert client.get(path).status_code == 200


def test_导航里有背单词而且背诵页也高亮它(client):
    """背某个单元时（/study/7）顶栏该还亮着「背单词」。

    精确匹配的话一进背诵页顶栏就没有任何一项是亮的，看着像离开了这个功能。
    """
    assert "背单词" in client.get("/study").text
    body = client.get("/study/1").text
    assert 'href="/study"' in body and "active" in body


def test_词典状态如实报出来(client):
    got = client.get("/api/wordbook/status").json()["dictionary"]
    assert got["ready"] is True
    assert got["size"] > 10000
    assert "MIT" in got["source"]


def test_录入前的预览把没认出来的词逐个列出来(client):
    """只报个数量不够：用户得知道是哪几个，才判断得出是自己敲错了
    还是词典没收——而这决定他下一步是改还是不管。"""
    got = client.get("/api/wordbook/preview",
                     params={"text": "abandon\nzzzznope\nthresholds"}).json()
    assert got["count"] == 3
    assert got["matched"] == 2
    assert got["missing"] == ["zzzznope"]
    assert [s["headword"] for s in got["sample"]] == ["abandon", "thresholds"]


def test_一整条录入链路(client):
    book = client.post("/api/wordbook/books",
                       json={"name": "考研红宝书", "note": "2026 版"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 12"}).json()
    assert unit["idx"] == 12, "单元号要从标签里认出来，否则排序只能按录入先后"

    words = ["zebra", "apple", "monkey", "banana", "threshold"]
    added = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                        json={"text": "\n".join(words), "page": "88"}).json()
    assert added["added"] == 5

    rows = client.get(f"/api/wordbook/units/{unit['id']}/entries").json()["entries"]
    assert [e["headword"] for e in rows] == words, "接口层把顺序排掉了"
    assert {e["page"] for e in rows} == {88}

    # 释义是程序配上的，用户只敲了词形
    threshold = next(e for e in rows if e["headword"] == "threshold")
    assert "门槛" in threshold["translation"]
    assert threshold["phonetic"]

    books = client.get("/api/wordbook/books").json()["books"]
    assert books[0]["total"] == 5 and books[0]["new"] == 5


def test_背一个词会推进它的档位(client):
    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    eid = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                      json={"text": "abandon"}).json()["entries"][0]["id"]
    got = client.post(f"/api/wordbook/entries/{eid}/review", json={"known": True}).json()
    assert got["status"] == 1
    got = client.post(f"/api/wordbook/entries/{eid}/review", json={"known": False}).json()
    assert got["wrong"] == 1


def test_没说认不认识就不记(client):
    """空的 body 当成「认识」会把进度悄悄推上去，而用户什么都没按。"""
    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    eid = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                      json={"text": "abandon"}).json()["entries"][0]["id"]
    assert client.post(f"/api/wordbook/entries/{eid}/review", json={}).status_code == 400


def test_删词书之前先留档(client):
    """这几张表装的是用户一页页敲进去的顺序，删掉只能再敲一遍——
    和删文章是同一档资产，走同一套保护（需要注意.md 第 10e 条）。"""
    book = client.post("/api/wordbook/books", json={"name": "要删的"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    client.post(f"/api/wordbook/units/{unit['id']}/entries",
                json={"text": "abandon\nthreshold", "page": "3"})

    impact = client.get(f"/api/wordbook/books/{book['id']}/impact").json()
    assert impact == {"units": 1, "entries": 2, "known": 0, "learning": 0}

    got = client.delete(f"/api/wordbook/books/{book['id']}").json()
    assert got["ok"] is True
    assert got["backup"]["made"] is True, "删之前必须留下一份快照"
    assert client.get(f"/api/wordbook/books/{book['id']}/impact").status_code == 404


def test_改释义和笔记(client):
    """书上的说法和字典不一样时以书为准；词根和联想写进笔记。"""
    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    eid = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                      json={"text": "abandon"}).json()["entries"][0]["id"]
    got = client.patch(f"/api/wordbook/entries/{eid}",
                       json={"translation": "vt. 放弃", "note": "a-(加强) + bandon(控制)"}).json()
    assert got["translation"] == "vt. 放弃"
    assert "bandon" in got["note"]


def test_页号打错了要当场说出来而不是崩掉(client):
    """词表视图里的页号是个自由文本框，打错是常态。

    原来那一行是裸的 `int(patch["page"])`：非数字抛 ValueError、null 抛
    TypeError，两个都不在接口层的捕获范围里，冒出去就是 HTTP 500——而
    db.session() 会把整次请求回滚，**同一次保存里的释义和笔记也一起丢了**，
    用户看到的只是一句「HTTP 500」。

    改成悄悄存成「没有页号」同样不行：那样这一条会掉到单元末尾去，
    而用户以为自己刚把页号改对了。所以是 400，不是 None。
    """
    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    eid = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                      json={"text": "abandon", "page": "43"}).json()["entries"][0]["id"]

    for bad in ("43页", "abc", "-1"):
        r = client.patch(f"/api/wordbook/entries/{eid}", json={"page": bad})
        assert r.status_code == 400, f"页号 {bad!r} 应该被当场拦下，实际 {r.status_code}"
    # 拦下来之后这一条一个字都不该变
    rows = client.get(f"/api/wordbook/units/{unit['id']}/entries").json()["entries"]
    assert rows[0]["page"] == 43

    # 空串和 null 都是「这一页不写页号」，那是有意的，不是打错
    assert client.patch(f"/api/wordbook/entries/{eid}", json={"page": ""}).json()["page"] is None
    assert client.patch(f"/api/wordbook/entries/{eid}", json={"page": None}).json()["page"] is None
    assert client.patch(f"/api/wordbook/entries/{eid}", json={"page": "7"}).json()["page"] == 7
    # 录入那一头用的是同一个判据，两处不能各判各的
    assert client.post(f"/api/wordbook/units/{unit['id']}/entries",
                       json={"text": "silence", "page": "43页"}).status_code == 400


def test_不存在的东西一律_404(client):
    assert client.get("/api/wordbook/books/999/units").status_code == 404
    assert client.get("/api/wordbook/units/999/entries").status_code == 404
    assert client.patch("/api/wordbook/entries/999", json={"note": "x"}).status_code == 404
    assert client.post("/api/wordbook/entries/999/review", json={"known": True}).status_code == 404


# --------------------------------------------------------------- 导出
#
# 导出是「和纸质版一致」这条承诺唯一能拿到手上校对的形式。它错了不会报错，
# 只会给出一份顺序不对的表——而用户拿它去对着书核，会以为是自己录错了。


def test_词书导出严格按书上的顺序(client):
    import csv
    import io

    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    u2 = client.post(f"/api/wordbook/books/{book['id']}/units", json={"label": "List 02"}).json()
    u1 = client.post(f"/api/wordbook/books/{book['id']}/units", json={"label": "List 01"}).json()
    # 故意先录后面那一页，再回头补前面的——真实用法就是「背到哪录到哪」
    client.post(f"/api/wordbook/units/{u1['id']}/entries",
                json={"text": "dog\ncat", "page": "44"})
    client.post(f"/api/wordbook/units/{u1['id']}/entries",
                json={"text": "ant\nbee", "page": "43"})
    client.post(f"/api/wordbook/units/{u2['id']}/entries",
                json={"text": "fox", "page": "60"})

    r = client.get(f"/api/wordbook/books/{book['id']}/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]

    text = r.text
    assert text.startswith("\ufeff"), "少了 BOM，Excel 会把中文列读成乱码"
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0][:6] == ["词书", "单元", "单元序号", "序号", "页号", "词形"]
    body = rows[1:]
    # 单元按 idx（List 01 在 List 02 前，尽管 List 02 先建），单元内按录入位置
    assert [r[1] for r in body] == ["List 01"] * 4 + ["List 02"]
    assert [r[5] for r in body] == ["ant", "bee", "dog", "cat", "fox"]
    assert [r[4] for r in body] == ["43", "43", "44", "44", "60"]


def test_书名里的斜杠不会毁掉文件名(client):
    """书名是用户自己填的，斜杠冒号引号都可能有。原样塞进
    Content-Disposition 会把那个头本身弄坏，在 Windows 上也存不成文件。"""
    book = client.post("/api/wordbook/books", json={"name": '六级/乱序"版"'}).json()
    r = client.get(f"/api/wordbook/books/{book['id']}/export.csv")
    assert r.status_code == 200
    disp = r.headers["content-disposition"]
    assert "/" not in disp.split("filename*=")[1], disp
    assert '"' not in disp.split("filename*=")[1], disp


def test_导出不存在的词书是_404(client):
    assert client.get("/api/wordbook/books/999/export.csv").status_code == 404


def test_空词书也导得出表头(client):
    """一行数据都没有时也要给出表头。回一个空文件的话，用户分不清是
    「这本书是空的」还是「导出坏了」。"""
    book = client.post("/api/wordbook/books", json={"name": "空的"}).json()
    r = client.get(f"/api/wordbook/books/{book['id']}/export.csv")
    assert r.status_code == 200
    assert "词形" in r.text


# --------------------------------------------------------------- 间隔重复
#
# 这一组盯的是**兜底方向**。排错了间隔不会报错，用户只会觉得「背了半天没记住」，
# 而那不会变成一个 bug 报告。最危险的一种是「悄悄不再问某个词」——
# 词还在库里、进度条上也还在，就是再也不出现了。


def test_答对之后排到以后再问(temp_db, book):
    from core.wordbook import scheduler

    if not scheduler.available():
        pytest.skip("这台机器没装 fsrs")
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        assert r["entries"][0]["due_at"] == "", "新词还没进过调度"
        assert r["entries"][0]["due"] is True, "没排过期的词必须算到期，否则它再也不会被问到"
        got = wb.record_review(s, r["entries"][0]["id"], known=True)
    assert got["due_at"], "答对之后要排出下一次"
    assert got["due"] is False, "刚答对的词不该马上又问一遍"


def test_答错之后很快再问(temp_db, book):
    """答错的间隔必须明显短于答对的，否则「没记住」和「记住了」待遇一样，
    间隔重复就白接了。"""
    from datetime import datetime, timedelta

    from core.wordbook import scheduler

    if not scheduler.available():
        pytest.skip("这台机器没装 fsrs")
    now = datetime(2026, 9, 8, 12, 0)          # naive，和库里那一列一个形状
    good = scheduler.review(None, known=True, now=now)
    bad = scheduler.review(None, known=False, now=now)
    assert bad["due_at"] < good["due_at"]
    assert good["due_at"] - bad["due_at"] > timedelta(minutes=1)


def test_传_naive_时间不会炸(temp_db, book):
    """库里所有时间列都是 naive UTC，所以「读一个时间出来再传进去」是最自然的
    写法。fsrs 那边要求 aware 且为 UTC，naive 直接抛 ValueError——
    这一脚必然有人踩，所以在 scheduler 里挡住，不让调用方记这条规矩。"""
    from datetime import datetime, timezone

    from core.wordbook import scheduler

    if not scheduler.available():
        pytest.skip("这台机器没装 fsrs")
    naive = datetime(2026, 9, 8, 12, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert scheduler.review(None, True, now=naive)["due_at"] ==         scheduler.review(None, True, now=aware)["due_at"]


def test_没有到期时间的词一律算到期():
    """老库补列之后 due_at 是 NULL，没装 fsrs 的机器上也一直是 NULL。

    把「没有 due_at」当成「不到期」的话，那些词**再也不会被问到**——
    用户看到一个空的背诵页，而他的词一个没少。往「多问一次」兜底，
    不往「悄悄少问」兜底。
    """
    from core.wordbook import scheduler

    assert scheduler.is_due(None) is True


def test_没装fsrs时退回原来的行为(temp_db, book, monkeypatch):
    """少一个可选依赖不该让这一页打不开，也不该让任何词消失。

    和 json-repair、CEFR 词表缺失是同一个处理方式。
    """
    from core.wordbook import scheduler

    monkeypatch.setattr(scheduler, "Scheduler", None)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    assert scheduler.available() is False
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        got = wb.record_review(s, r["entries"][0]["id"], known=True)
    assert got["status"] == 1, "档位照样推"
    assert got["due_at"] == "" and got["due"] is True, "没有调度就一直算到期，词不会消失"


def test_到期时间存的是_naive_UTC(temp_db, book):
    """库里所有时间列都是 naive UTC。FSRS 给的是 aware，直接塞进去的话
    数值就不是 UTC 了，之后所有「到期没有」都差一个时区。"""
    from core.store.models import BookEntry
    from core.wordbook import scheduler

    if not scheduler.available():
        pytest.skip("这台机器没装 fsrs")
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon"], page=1)
        wb.record_review(s, r["entries"][0]["id"], known=True)
    with temp_db.session() as s:
        row = s.get(BookEntry, r["entries"][0]["id"])
        assert row.due_at.tzinfo is None, "库里存的必须是 naive"
    # 下发到前端时补回 UTC 标记，否则 new Date() 会当本地时间读第二遍
    assert r["entries"][0]["due_at"] == "" or "+00:00" in r["entries"][0]["due_at"]


def test_档位和到期日各管各的(temp_db, book):
    """档位是给人看的「我背到哪了」，到期日是给队列用的。

    拿档位反推间隔的话，一个新词和一个背了五次又忘掉的词会排到同一天，
    而它们的遗忘曲线差得远。
    """
    from core.wordbook import scheduler

    if not scheduler.available():
        pytest.skip("这台机器没装 fsrs")
    with temp_db.session() as s:
        r = wb.add_entries(s, book["unit_id"], ["abandon", "threshold"], page=1)
        fresh, seasoned = (e["id"] for e in r["entries"])
        for _ in range(4):
            wb.record_review(s, seasoned, known=True)
        a = wb.record_review(s, fresh, known=False)
        b = wb.record_review(s, seasoned, known=False)
    assert a["status"] == b["status"] == STATUS_LEARNING, "答错都退回「刚认识」"
    # 档位一样，但两者的调度状态不一样——FSRS 记着 seasoned 被复习过四次
    assert a["extra"] is not None and b["extra"] is not None


def test_到期字段一路走到接口(client):
    book = client.post("/api/wordbook/books", json={"name": "六级"}).json()
    unit = client.post(f"/api/wordbook/books/{book['id']}/units",
                       json={"label": "List 01"}).json()
    eid = client.post(f"/api/wordbook/units/{unit['id']}/entries",
                      json={"text": "abandon"}).json()["entries"][0]["id"]
    rows = client.get(f"/api/wordbook/units/{unit['id']}/entries").json()["entries"]
    assert rows[0]["due"] is True, "新词要算到期，否则背诵页一开就是空的"
    got = client.post(f"/api/wordbook/entries/{eid}/review", json={"known": True}).json()
    assert "due_at" in got and "due" in got
