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
