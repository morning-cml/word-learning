"""Web 层：页面路由、接口、以及生成的中止。

页面路由这几条看着琐碎，但它们守的是一条设计约定：
**加一个页面只要往 web/pages.py 的注册表里加一条**，路由和导航自动生成。
哪天有人把导航改回硬编码在模板里，这里会先挂。
"""
from __future__ import annotations

import re
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db):
    import main

    return TestClient(main.app)


# ------------------------------------------------------------------- 页面

PAGES = [("/", "index"), ("/library", "library"), ("/words", "words"),
         ("/settings", "settings"), ("/read/1", "reader")]


@pytest.mark.parametrize("path,page_id", PAGES)
def test_页面可访问且挂上了页面标识(client, path, page_id):
    """<body data-page> 是 main.js 按名字动态加载页面模块的依据，丢了整页脚本就不跑。"""
    r = client.get(path)
    assert r.status_code == 200
    assert f'data-page="{page_id}"' in r.text


def test_导航由注册表生成(client):
    from web.pages import nav_pages

    body = client.get("/").text
    for page in nav_pages():
        assert f'href="{page.path}"' in body
        assert page.label in body


@pytest.mark.parametrize("path,expect", [("/", "/"), ("/library", "/library"), ("/words", "/words")])
def test_当前页在导航里高亮(client, path, expect):
    active = re.findall(r'<a href="([^"]+)" class="active"', client.get(path).text)
    assert active == [expect]


def test_详情页不进导航(client):
    """/read/{id} 有 nav_match，不该在导航里出现，也不该高亮别人。"""
    assert not re.findall(r'<a href="[^"]*" class="active"', client.get("/read/1").text)


def test_不存在的文章页仍然返回页面(client):
    """由前端拿到 404 后显示「文章不存在」，而不是给一个 Starlette 的错误页。"""
    assert client.get("/read/999999").status_code == 200


@pytest.mark.parametrize("asset", [
    "/static/css/tokens.css", "/static/css/app.css", "/static/css/reader.css",
    "/static/js/main.js", "/static/js/core.js", "/static/js/api.js",
    "/static/js/components/reader.js", "/static/js/components/stats.js",
    "/static/js/pages/index.js", "/static/js/pages/library.js",
    "/static/js/pages/words.js", "/static/js/pages/reader.js",
    "/static/js/pages/settings.js",
])
def test_静态资源都在(client, asset):
    """每个 pages/<id>.js 都要存在，否则那一页的交互静默失效。"""
    assert client.get(asset).status_code == 200


# ------------------------------------------------------------------- 接口

def test_status_接口(client):
    d = client.get("/api/status").json()
    assert set(d) >= {"provider", "model", "has_key", "level", "cefr", "backup"}


def test_设置接口不下发_Key(client):
    """Key 只存在本地文件，接口返回的永远是掩码串。"""
    d = client.get("/api/settings").json()
    for p in d["providers"]:
        assert "masked_key" in p and "api_key" not in p
        assert not p["masked_key"] or "*" in p["masked_key"]


def test_词表接口(client):
    d = client.get("/api/words").json()
    assert set(d) == {"stats", "words"}
    assert set(d["stats"]) == {"total", "seen_once", "seen_multi"}


def test_文章列表带线索比(client, temp_db):
    """线索强度是这个应用的头号指标，文库列表里也该一眼看得到。"""
    from tests.test_store import DOC, META      # noqa: PLC0415  只有这条用得上

    with temp_db.session() as s:
        temp_db.save_article(s, DOC, {**META, "stats": {
            "word_count": 8, "clue_strength": {"strong": 2, "weak": 1, "none": 0}}})

    row = client.get("/api/articles").json()["articles"][0]
    assert row["clue"] == "2/3"


def test_历史耗时给出每次调用的中位数(client, temp_db):
    """首页要在刚开始生成时就给出「还需要多久」，那一刻还没有本次运行的
    任何测量值。拿用户自己前几篇的实测当先验——这个数在不同模型、
    不同网络下能差好几倍，写死一个常数只会给出误导性的倒计时。"""
    from tests.test_store import DOC, META      # noqa: PLC0415

    with temp_db.session() as s:
        for ms, calls in ((60_000, 6), (120_000, 6), (30_000, 6)):   # 每次 10 / 20 / 5 秒
            temp_db.save_article(s, DOC, {**META, "stats": {"ms": ms, "llm_calls": calls}})

    d = client.get("/api/timing").json()
    assert d["samples"] == 3
    assert d["sec_per_call"] == 10.0, "取中位数：一次特别慢不该把估算拉飞"


def test_没有历史时不编一个数出来(client):
    """宁可不显示剩余时间，也不要给一个编出来的数字让人据此安排。"""
    d = client.get("/api/timing").json()
    assert d == {"samples": 0, "sec_per_call": None, "scope": "none"}


def test_只统计存过用量的文章(client, temp_db):
    """老文章的 stats 里没有 ms / llm_calls，混进来会被当成 0。"""
    from tests.test_store import DOC, META      # noqa: PLC0415

    with temp_db.session() as s:
        temp_db.save_article(s, DOC, {**META, "stats": {}})               # 老文章
        temp_db.save_article(s, DOC, {**META, "stats": {"ms": 80_000, "llm_calls": 4}})

    d = client.get("/api/timing").json()
    assert d["samples"] == 1 and d["sec_per_call"] == 20.0


def test_用词上限各档带词汇量(client):
    """首页那四档原先只是四个字母。数字必须现算——没下载 CEFR-J 时标尺会
    退回内置兜底表，写死在模板里的数字就会和程序实际拦的东西对不上，
    而这种不一致用户没法自己发现。"""
    d = client.get("/api/levels").json()
    assert set(d) == {"using_real_data", "cumulative"}
    assert set(d["cumulative"]) == {"A1", "A2", "B1", "B2", "C1", "C2"}
    c = d["cumulative"]
    assert c["A2"] <= c["B1"] <= c["B2"] <= c["C1"], "累计值不能往回掉"


def test_篇幅预览在词太多时给出警告(client):
    d = client.post("/api/article/plan-preview", json={"words": " ".join(
        f"word{i}" for i in range(30))}).json()
    assert d["count"] == 30 and d["warning"]


def test_词数超上限直接拒绝(client):
    """超了 sizing() 会把每段压到十几个目标词，5 句话塞不下，
    校验必然不过——整段整段地烧修复预算才轮到下一段。"""
    from tasks.article.task import MAX_WORDS

    words = " ".join(f"word{i}" for i in range(MAX_WORDS + 5))
    events = client.post("/api/article/generate", json={"words": words}).text
    assert "一次最多" in events


def test_没配_Key_时给人话(client, monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "api_key", lambda pid: "")
    events = client.post("/api/article/generate", json={"words": "abandon"}).text
    assert "API Key" in events


# ------------------------------------------------------------------- 中止

@pytest.fixture
def live_server(temp_db):
    """真跑一个 uvicorn。

    中止这件事必须用真实连接测：TestClient 不经过 socket，客户端断开
    传导不到 StreamingResponse 的收尾逻辑，测出来的是 TestClient 的行为
    而不是应用的。这也是它值得多花几百毫秒起个服务的原因。
    """
    import socket
    import time

    import uvicorn

    import main

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(
        main.app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.05):
                break
        except OSError:
            time.sleep(0.02)
    else:
        pytest.fail("测试服务器没能起来")

    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _patch_slow_llm(monkeypatch, fake_llm, responses, calls):
    """把管线换成一个每次慢 0.25 秒的假模型，好在中途掐断。"""
    import time

    import web.routes.article_api as api

    class Slow(fake_llm):
        def json(self, messages, **kw):
            calls.append(time.time())
            time.sleep(0.25)
            return super().json(messages, **kw)

    monkeypatch.setattr(api, "LLM", lambda **kw: Slow(responses))
    monkeypatch.setattr(api.registry, "build", lambda pid, key, **kw: None)
    monkeypatch.setattr(api.settings, "api_key", lambda pid: "fake")


def test_客户端断开时后台停下且不落库(
        live_server, temp_db, monkeypatch, fake_llm, happy_responses):
    """「点停止」和「直接关页面」走同一条路：前端断开连接，
    后端从流被关闭这件事本身察觉。不置位取消标志的话，工作线程会把
    剩下几次模型调用跑完再落库——用户以为停了，其实还在烧 token，
    最后还多出一篇没人要的文章。"""
    import httpx

    calls: list[float] = []
    responses = {**happy_responses, "plan": {
        **happy_responses["plan"],
        "paragraphs": [{"focus": f"f{i}", "words": ["abandon", "silence"]}
                       for i in range(6)]}}          # 六段，够长到能中途掐断
    _patch_slow_llm(monkeypatch, fake_llm, responses, calls)

    with httpx.Client(timeout=30) as c:
        with c.stream("POST", live_server + "/api/article/generate",
                      json={"words": "abandon silence", "level": "B2"}) as r:
            seen = 0
            for line in r.iter_lines():
                if line.startswith("data: "):
                    seen += 1
                if seen >= 4:
                    break                            # 模拟点「停止」

    at_abort = len(calls)
    threading.Event().wait(2.0)                      # 给后台线程充分的时间继续跑

    assert len(calls) - at_abort <= 1, "取消后除了在飞的那次调用，不该再有新的"
    with temp_db.session() as s:
        assert temp_db.list_articles(s) == [], "半篇文章不该落库"


def test_不中断时照常跑完并落库(client, temp_db, monkeypatch, fake_llm, happy_responses):
    import web.routes.article_api as api

    monkeypatch.setattr(api, "LLM", lambda **kw: fake_llm(happy_responses))
    monkeypatch.setattr(api.registry, "build", lambda pid, key, **kw: None)
    monkeypatch.setattr(api.settings, "api_key", lambda pid: "fake")

    body = client.post("/api/article/generate",
                       json={"words": "abandon silence", "level": "B2"}).text
    assert '"done"' in body and '"saved"' in body
    with temp_db.session() as s:
        assert len(temp_db.list_articles(s)) == 1


def test_预览的篇幅和任务层用同一个公式(client):
    """预览是在调模型**之前**告诉用户「这批词大概会写多长」。

    公式写两份的话，改了 sizing() 的人不会想到还要回来改接口里那一行，
    于是预览说 170 词、实际写出 340 词——而「界面说的」和「程序实际做的」
    不一致，正是用户没法自己发现的那类错（见 CLAUDE.md 开头那两条）。
    """
    from tasks.article.task import estimated_words, sizing

    for n in (1, 3, 6, 12, 20):
        words = " ".join(f"word{i}" for i in range(n))
        d = client.post("/api/article/plan-preview", json={"words": words}).json()
        n_para, _per_para, n_sent = sizing(n)
        assert d["count"] == n
        assert (d["paragraphs"], d["sentences_per_paragraph"]) == (n_para, n_sent)
        assert d["estimated_words"] == estimated_words(n_para, n_sent)


def test_预览的篇幅跟着词数走(client):
    """用户报的就是这个：给 1 个词和给 6 个词，提示都是「2 段约 170 词」。"""
    def est(n):
        words = " ".join(f"word{i}" for i in range(n))
        return client.post("/api/article/plan-preview",
                           json={"words": words}).json()["estimated_words"]

    assert est(1) < est(3) < est(6) < est(12), "篇幅必须随词数增长"
    assert est(1) != est(6), "一个词和六个词不能给出同样的篇幅"
