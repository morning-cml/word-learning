"""测试共用的夹具。

两条贯穿全部测试的原则：

1. **不联网**。所有涉及模型的测试都用 FakeLLM，返回什么由测试自己决定。
   真去调模型的测试跑不稳、要花钱，而且验不了「模型返回畸形数据时会怎样」——
   而那恰恰是这个项目最需要覆盖的部分。

2. **不碰用户的库**。每个测试拿自己的临时 SQLite 文件，用完即弃。
   core/store/db.py 的引擎是模块级的，所以夹具在测试期间把它重绑到临时库，
   结束再还原——直接改 db.DB_PATH 是没用的，引擎在 import 时就建好了。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """把 core.store.db 重绑到一个临时库，测试结束自动还原。"""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from core.store import db

    path = tmp_path / "test.db"
    engine = sa.create_engine(
        f"sqlite:///{path}", future=True, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_Session", sessionmaker(
        bind=engine, expire_on_commit=False, future=True))
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return db


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    """接口层的测试客户端。

    除了把库换成临时的（temp_db），**设置文件也必须换掉**。上面那条
    「不碰用户的库」同样适用于 config/settings.local.json：它装着 API Key，
    而 POST /api/settings 是会真写进去的——一条测试跑完，用户存的用词上限
    就被改成了测试用的那个值，而且没有任何地方会说一声。
    这个文件和 data/app.db 是本机仅有的两份不可再生状态，两份都得隔离。
    """
    from fastapi.testclient import TestClient

    from core import settings

    monkeypatch.setattr(settings, "SETTINGS_PATH", tmp_path / "settings.local.json")
    import main

    # base_url 必须是回环名字：应用只认 127.0.0.1 / localhost（防 DNS rebinding），
    # TestClient 默认发的 Host 是 testserver，会被中间件挡成 400。
    # 把 testserver 加进白名单更省事，但那等于为了测试在生产配置里开个口子。
    return TestClient(main.app, base_url="http://127.0.0.1")


class FakeLLM:
    """按 prompt 内容分发的假模型。

    管线的每一步用的是不同的 prompt，靠里面的特征词就能认出来是哪一步，
    不用改生产代码去注入。calls 记录调用顺序，测「白烧了几次调用」时要用。
    """

    #  prompt 里出现这个词 -> 这是哪一步
    STEPS = {
        "请先做选题": "plan",
        "扮演一个完全不认识": "audit",
        "缺少足够的语境线索": "clue_fix",
        "校验发现以下问题": "repair",
        "写中文释义": "glossary",
    }

    def __init__(self, responses: dict):
        from core.llm.client import Usage

        self.responses = responses
        self.usage = Usage()
        self.calls: list[str] = []

    def _step(self, prompt: str) -> str:
        for marker, name in self.STEPS.items():
            if marker in prompt:
                return name
        return "write"

    def json(self, messages, **kwargs):
        step = self._step(messages[-1]["content"])
        self.calls.append(step)
        return self.responses[step]


@pytest.fixture
def fake_llm():
    return FakeLLM


# --------------------------------------------------------------------- 样例数据

#  一段合格的正文：两个目标词都出现，中英齐全，用词在 B2 以内
GOOD_PARAGRAPH = {
    "sentences": [{
        "en": "The shop was abandoned last spring, and the silence stayed on for months.",
        "zh": "小店去年春天废弃了，寂静留了好几个月。",
        "targets": [
            {"lemma": "abandon", "surface": "abandoned"},
            {"lemma": "silence", "surface": "silence"},
        ],
    }],
}

GOOD_AUDIT = {"audits": [
    {"lemma": "abandon", "strength": "strong", "clue": "stayed on for months", "why": ""},
    {"lemma": "silence", "strength": "strong", "clue": "", "why": ""},
]}

GOOD_PLAN = {
    "title_en": "The Quiet Shop", "title_zh": "安静的小店", "genre": "短篇小说",
    "names": [], "unplaced": [],
    "paragraphs": [{"focus": "开场", "words": ["abandon", "silence"]}],
}


@pytest.fixture
def happy_responses():
    """一次顺风生成：每一步都返回合格结果。测试按需覆盖其中某一步。"""
    return {
        "plan": GOOD_PLAN,
        "write": GOOD_PARAGRAPH,
        "audit": GOOD_AUDIT,
        "clue_fix": GOOD_PARAGRAPH,
        "repair": GOOD_PARAGRAPH,
        "glossary": {"glossary": [{"lemma": "abandon", "pos": "v.", "zh": "抛弃", "note": ""}]},
    }


def run_pipeline(llm, words=("abandon", "silence"), level="B2"):
    """跑完整条文章管线，返回 (document, stats, 全部事件)。"""
    from tasks.article.task import ArticleTask

    events, doc, stats = [], None, {}
    for event in ArticleTask().run(llm, {"words": list(words), "level": level}):
        events.append(event)
        if event["type"] == "done":
            doc, stats = event["document"], event["stats"]
    return doc, stats, events


@pytest.fixture
def cefr_table(monkeypatch):
    """把 CEFR 词表换成一张测试自己给的小表。

    有些判定只在词表长成某个样子时才走得到，最要紧的一种是
    **目标词的派生形式自己也是一个词条、而且等级比原形更高**
    （真词表里这样的组合有 983 对，其中 496 对更难）。
    直接依赖 data/cefr.csv 的话，这类测试在没下载词表的机器上（CI 就是）
    会因为退回内置兜底表而悄悄走到另一条分支上——看着过了，其实没测到。

    **词频回落也要一起换掉。** 标尺现在有两个数据源：CEFR-J 查不到时回落到
    ECDICT 的词频（见 cefr.FREQ_CUTOFFS）。只换前一个的话，这张小表之外的词
    会静静地去查那 30713 条真词频表——测试写着「词表里只有这两个词」，
    实际判定却由仓库里另一份数据决定，等于隔离没做全
    （需要注意.md 第 17c 条：隔离的范围要跟着状态走）。

    默认把词频表清空（= 词典缺失时的降级路径），要测回落的测试自己传
    `freq={...}` 进来。
    """
    from core.lexicon import cefr

    def apply(table: dict[str, str], freq: dict[str, int] | None = None) -> None:
        monkeypatch.setattr(cefr, "_load", lambda: (dict(table), True))
        monkeypatch.setattr(cefr, "_freq_table", lambda: dict(freq or {}))

    return apply
