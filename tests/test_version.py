"""版本号只有一处，而且忘记更新时要能被发现。

版本号天然要在好几个地方出现（顶栏、接口、CHANGELOG），而这个项目已经吃过
「同一件事写两遍必然分叉」的亏（需要注意.md 第 20 条）。版本号分叉的表现
尤其难看：界面说 0.2.0、CHANGELOG 最新条目写着 0.3.0，用户报 bug 时给的
那个号对不上任何一次实际发布——**而这件事没有任何人会报**，
因为看得出对不上的人本来就知道该看哪一个。

所以这几条不是形式主义：它们是这条规矩唯一的执行者。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.version import VERSION

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"


def test_版本号是合法的语义化版本():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION), VERSION


def test_CHANGELOG_里有这一版的条目():
    """发版就是「改 core/version.py + 在 CHANGELOG 里开一节」两件事。
    只做前一件，用户看到一个新号却查不到它改了什么。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(VERSION)}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
                     text, re.M), f"CHANGELOG 里没有 [{VERSION}] 这一节"


def test_CHANGELOG_留着未发布那一节():
    """下一版的改动要有地方落。这一节被顺手删掉的话，
    下次改动就会被记进已经发出去的那一版里。"""
    assert re.search(r"^## \[未发布\]$", CHANGELOG.read_text(encoding="utf-8"), re.M)


def test_最新的版本号排在最前():
    """CHANGELOG 是倒序的。新版本被追加到文件末尾的话，读的人第一眼
    看到的是最老的那一版，而他想知道的永远是「最近改了什么」。"""
    text = CHANGELOG.read_text(encoding="utf-8")
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    assert versions, "CHANGELOG 里一个版本号都没有"
    assert versions[0] == VERSION, f"最前面的是 {versions[0]}，而当前版本是 {VERSION}"
    parsed = [tuple(int(n) for n in v.split(".")) for v in versions]
    assert parsed == sorted(parsed, reverse=True), f"版本顺序乱了：{versions}"


@pytest.mark.parametrize("path", ["/", "/library", "/words", "/settings", "/read/1"])
def test_每个页面的顶栏都带着版本号(client, path):
    """顶栏是 base.html 渲的，所有页面共用——但「共用」是靠 pages.py 注入
    的那个上下文，漏了哪一页只有那一页不显示，而没人会逐页去看。"""
    assert f"v{VERSION}" in client.get(path).text


def test_模板里不许写死版本号():
    """写死了就等于又开了一处来源。这条盯的是「以后有人图省事」。"""
    for tpl in (ROOT / "web" / "templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"v\d+\.\d+\.\d+", text), f"{tpl.name} 里写死了版本号"


def test_status_接口也给得出版本号(client):
    """截图里的那个号取不出来。报问题、比对行为都需要程序上拿得到。"""
    assert client.get("/api/status").json()["version"] == VERSION
