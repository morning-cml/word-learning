"""Word Learning —— 用文章记单词。

启动：
    .venv\\Scripts\\python.exe main.py
然后打开 http://127.0.0.1:8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core import settings
from core.lexicon import cefr
from core.store import db
from web import pages
from web.routes.article_api import router as article_router
from web.routes.settings_api import router as settings_router

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Word Learning", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "web" / "static")), name="static")
app.include_router(settings_router)
app.include_router(article_router)
# 页面路由由 web/pages.py 的注册表生成，加页面不用回来改这里
pages.register(app, templates)

db.init_db()   # 幂等；确保直接 import app 的场景（测试、ASGI 托管）也有表


@app.get("/api/status")
def status() -> dict:
    """顶栏和设置页都用它。加字段时记得前端 api.js 那边不用改——它只透传。"""
    provider, model = settings.active()
    return {
        "provider": provider,
        "model": model,
        "has_key": bool(settings.api_key(provider)),
        "level": settings.load().get("level", "B2"),
        "cefr": {"real_data": cefr.is_real_data(), "size": cefr.size()},
        "backup": db.backup_state(),
    }



if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
