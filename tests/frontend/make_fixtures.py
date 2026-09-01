"""为前端测试准备夹具。

导出的是**服务端真实渲染的页面**和**真实接口的响应**，不是手写的假 HTML——
模板改了、接口字段改了，夹具会跟着变，不会出现「测试还在测三个月前的页面」。

**数据是这里现造的，不用开发者机器上那份库。** 第一版直接读了真实的
data/app.db，结果是：本地有七篇文章所以测试全过，CI 上库是空的就崩了——
那种「因为你碰巧有数据所以过」的测试不算测试。现在往临时库里塞一篇内容
固定的文章，谁在哪跑都一样，测试也就能对具体内容下断言。

用法（在 tests/frontend 下）：
    python make_fixtures.py && npm test

产物放在 .fixtures/（已 gitignore）：
    index.html library.html words.html settings.html reader.html
    api.json          几个接口的真实响应，给 fetch 打桩用
    static/js/**      web/static/js 的逐字节副本 + 一个 {"type":"module"}

最后这份副本是不得已：Node 判断 .js 是 ESM 还是 CommonJS 看最近的 package.json，
而 web/static/js 底下没有、也不该有一个（它会被当静态资源发出去）。
副本是跑测试时现拷的，所以测的仍然是仓库里那份真代码。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / ".fixtures"

sys.path.insert(0, str(ROOT))

PAGES = [("index", "/"), ("library", "/library"), ("words", "/words"),
         ("settings", "/settings"), ("reader", "/read/1")]

ENDPOINTS = ["/api/status", "/api/articles", "/api/words", "/api/settings", "/api/articles/1"]

#  一篇内容固定的文章。目标词、线索强度、掌握程度都是测试会断言的具体值。
SEED_DOC = {
    "title_en": "The Wrong Number",
    "title_zh": "打错的电话",
    "topic": "一位深夜电台主持人接到一通打错的电话",
    "genre": "短篇小说",
    "paragraphs": [{
        "sentences": [
            {"en": "The studio had been abandoned by everyone but him, "
                   "and the chairs were still warm.",
             "zh": "除了他，所有人都离开了直播间，椅子还是温的。",
             "targets": [{"lemma": "abandon", "surface": "abandoned"}]},
            {"en": "No one spoke, and the silence pressed against the glass like a hand.",
             "zh": "没人说话，寂静像一只手抵着玻璃。",
             "targets": [{"lemma": "silence", "surface": "silence"}]},
        ],
        "audits": [
            {"lemma": "abandon", "strength": "strong",
             "clue": "by everyone but him", "why": "同句里点明了只剩他一个"},
            {"lemma": "silence", "strength": "strong",
             "clue": "No one spoke", "why": "前半句直接给出了同义改写"},
        ],
    }],
}

SEED_META = {
    "level": "B2", "provider": "deepseek", "model": "deepseek-v4-pro",
    "target_words": ["abandon", "silence"],
    "stats": {
        "word_count": 26, "sentence_count": 2,
        "targets_total": 2, "targets_hit": 2, "targets_missed": [], "unplaced": [],
        "offender_rate": 0.0, "offenders": [], "using_real_cefr": True,
        "clue_strength": {"strong": 2, "weak": 0, "none": 0},
        "llm_calls": 4, "tokens": 4210,
    },
    "glossary": {"abandon": "v. 抛弃；离开", "silence": "n. 寂静；沉默"},
}


#  第二篇。和第一篇共用 abandon，好让「多语境 / 只见过一次」两个筛选都有东西可筛：
#  abandon 出现在两篇里，silence 和 confess 各只出现一次。
SECOND_DOC = {
    "title_en": "Boxes in the Attic",
    "title_zh": "阁楼里的箱子",
    "topic": "整理旧物时翻出一封没寄出的信",
    "genre": "回忆散文",
    "paragraphs": [{
        "sentences": [
            {"en": "The attic had been abandoned since the move, and dust lay on everything.",
             "zh": "搬家之后阁楼就荒着，什么上面都积了灰。",
             "targets": [{"lemma": "abandon", "surface": "abandoned"}]},
            {"en": "He had to confess he had never opened the last box.",
             "zh": "他不得不承认，最后那个箱子他从没打开过。",
             "targets": [{"lemma": "confess", "surface": "confess"}]},
        ],
        "audits": [
            {"lemma": "abandon", "strength": "strong",
             "clue": "dust lay on everything", "why": "积灰点明了长期没人动"},
            {"lemma": "confess", "strength": "weak",
             "clue": "", "why": "只有方向性提示"},
        ],
    }],
}

SECOND_META = {
    "level": "B2", "provider": "deepseek", "model": "deepseek-v4-pro",
    "target_words": ["abandon", "confess"],
    "stats": {
        "word_count": 24, "sentence_count": 2,
        "targets_total": 2, "targets_hit": 2, "targets_missed": [], "unplaced": [],
        "offender_rate": 0.0, "offenders": [], "using_real_cefr": True,
        "clue_strength": {"strong": 1, "weak": 1, "none": 0},
        "llm_calls": 5, "tokens": 5120,
    },
    "glossary": {"confess": "v. 坦白；承认"},
}


def seed_database() -> Path:
    """把 core.store.db 指向一个临时库并塞进样例数据。

    必须在 import main 之前做：db 的引擎是模块级的，import 时就建好了。
    """
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from core.store import db

    path = Path(tempfile.mkdtemp(prefix="wl-fixtures-")) / "app.db"
    db._engine = sa.create_engine(
        f"sqlite:///{path}", future=True, connect_args={"check_same_thread": False})
    db._Session = sessionmaker(bind=db._engine, expire_on_commit=False, future=True)
    db.DB_PATH = path
    db.init_db()

    with db.session() as s:
        db.save_article(s, SEED_DOC, SEED_META)
        db.save_article(s, SECOND_DOC, SECOND_META)
        db.set_word_status(s, "abandon", 3)      # 让掌握程度那一列有非默认值
        db.set_word_status(s, "confess", 98)     # 「已掌握」筛选也要有东西可筛
    return path


def main() -> int:
    seed_database()

    from fastapi.testclient import TestClient

    import main as backend

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    client = TestClient(backend.app)

    for name, path in PAGES:
        (OUT / f"{name}.html").write_text(client.get(path).text, encoding="utf-8")

    api: dict = {}
    for path in ENDPOINTS:
        r = client.get(path)
        if r.status_code != 200:
            raise SystemExit(f"夹具接口 {path} 返回 {r.status_code}，数据没塞进去？")
        api[path] = r.json()

    for lemma in ("abandon", "silence", "confess"):
        api[f"/api/words/{lemma}"] = client.get(f"/api/words/{lemma}").json()
    api["__lemma__"] = SEED_META["target_words"][0]

    (OUT / "api.json").write_text(json.dumps(api, ensure_ascii=False), encoding="utf-8")

    shutil.copytree(ROOT / "web" / "static", OUT / "static")
    (OUT / "static" / "js" / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")

    print(f"夹具已导出到 {OUT.relative_to(ROOT)}")
    print(f"  页面 {len(PAGES)} 个 · 接口 {len(api) - 1} 个 · "
          f"文章《{api['/api/articles/1']['title_en']}》 · "
          f"词条 {len(api['/api/words']['words'])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
