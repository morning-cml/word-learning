"""为前端测试准备夹具。

导出的是**服务端真实渲染的页面**和**真实接口的响应**，不是手写的假 HTML——
模板改了、接口字段改了，测试会跟着一起变，不会出现「测试还在测三个月前的页面」。

用法（在 tests/frontend 下）：
    python make_fixtures.py && npm test

产物放在 .fixtures/（已 gitignore）：
    index.html library.html words.html settings.html reader.html
    api.json          几个接口的真实响应，给 fetch 打桩用
    static/js/**      web/static/js 的逐字节副本 + 一个 {"type":"module"}

最后这一份副本是不得已：Node 判断 .js 是 ESM 还是 CommonJS 看最近的 package.json，
而 web/static/js 底下没有、也不该有一个 package.json（它会被当静态资源发出去）。
副本是跑测试时现拷的，所以测的仍然是仓库里那份真代码。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / ".fixtures"

sys.path.insert(0, str(ROOT))

PAGES = [("index", "/"), ("library", "/library"), ("words", "/words"),
         ("settings", "/settings"), ("reader", "/read/1")]

ENDPOINTS = ["/api/status", "/api/articles", "/api/words", "/api/settings", "/api/articles/1"]


def main() -> int:
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
        api[path] = r.json() if r.status_code == 200 else None

    # 词条详情要一个真实存在的词；库是空的时候就没有，测试那边会跳过相关断言
    first = (api.get("/api/words") or {}).get("words") or []
    if first:
        lemma = first[0]["lemma"]
        api[f"/api/words/{lemma}"] = client.get(f"/api/words/{lemma}").json()
        api["__lemma__"] = lemma

    (OUT / "api.json").write_text(json.dumps(api, ensure_ascii=False), encoding="utf-8")

    shutil.copytree(ROOT / "web" / "static", OUT / "static")
    (OUT / "static" / "js" / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")

    art = api.get("/api/articles/1")
    print(f"夹具已导出到 {OUT.relative_to(ROOT)}")
    print(f"  页面 {len(PAGES)} 个 · 接口 {len(ENDPOINTS)} 个 · "
          f"词条 {len(first)} 个 · 文章 {'有' if art else '无（相关断言会跳过）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
