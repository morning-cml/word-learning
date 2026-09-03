"""Word Learning —— 用文章记单词。

唯一入口。双击 run.bat 就是跑这个文件：起一个只监听 127.0.0.1 的本地服务，
再用 Edge 打开它。直接跑也一样：

    .venv\\Scripts\\python.exe main.py

界面是 web/ 那一套 HTML/CSS/JS，跑在浏览器里。这里不再有第二套 UI，
也不再有 Qt 窗口外壳——同一个产品维护两份界面的代价是每加一个功能写两遍，
而漏掉的那一遍必然是后写的那端。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

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

# 只接受以回环名字发来的请求。
#
# 服务只监听 127.0.0.1，听上去已经够了——但那挡不住 DNS rebinding：
# 攻击者把自己的域名解析到 127.0.0.1，浏览器就认为那是同源，于是你访问的
# 任意一个网页都能读走整个文库和词库、删文章、以及**发起生成把你的额度烧掉**。
# 同源策略在这里不设防，正是因为「同源」已经被 DNS 骗过去了。
#
# 代价这边是零：本机自用的正常入口只有 127.0.0.1 和 localhost，
# 一行配置不改变任何用户可见的行为；而攻击者控制不了这两个名字。
# Host 头里的端口会被中间件自己切掉，所以换端口（_pick_port）不受影响。
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
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


# ====================================================================== 启动
# 下面这些只在直接运行本文件时用得上。测试和 make_fixtures.py 都是
# `import main` 取 app，不会碰到这一段。

DEFAULT_PORT = 8000


def _pick_port() -> int:
    """优先用 8000，被占了就让系统随便给一个。

    写死端口的话，用户开着第二个实例、或者别的程序占了 8000，
    表现出来就是「双击没反应」——错误死在 uvicorn 启动里，控制台一闪而过。
    """
    for port in (DEFAULT_PORT, 0):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            # 这里到 uvicorn 真正 bind 之间有个极小的空窗，本机自用不值得为它加锁
            return sock.getsockname()[1]
    return DEFAULT_PORT


def _edge_path() -> str | None:
    """找 msedge.exe。

    不用 webbrowser 模块：它开的是系统默认浏览器，而这里要的就是 Edge。
    注册表 App Paths 是 Windows 记录「已安装程序在哪」的正规位置，
    比硬编码路径可靠——Edge 装在别的盘、或者以后换了目录都还能找到。
    """
    if (env := os.environ.get("WL_EDGE")) and Path(env).exists():
        return env

    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                key = winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")
                with key:
                    path = winreg.QueryValueEx(key, "")[0]
                if path and Path(path).exists():
                    return path
            except OSError:
                continue
    except ImportError:      # 非 Windows
        pass

    # 注意目录名是 "Microsoft\Edge" 不是 "Microsoft Edge"——写错了这两行会静默失效，
    # 而上面的注册表那层照样能找到，于是错误永远不会暴露出来。
    for guess in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(guess).exists():
            return guess

    from shutil import which
    return which("msedge")


def _open_in_edge(url: str) -> None:
    """打开浏览器。三层兜底，最后一层是把地址印出来让人自己点。

    找不到 Edge 不该让整个程序起不来——服务是好的，缺的只是「谁来显示」。
    """
    exe = _edge_path()
    if exe:
        try:
            subprocess.Popen([exe, url], close_fds=True)
            return
        except OSError as exc:
            print(f"[启动] 调用 Edge 失败（{exc}），改用默认浏览器。")

    try:
        import webbrowser

        if webbrowser.open(url):
            print("[启动] 没找到 Edge，已用系统默认浏览器打开。")
            return
    except Exception:        # noqa: BLE001  打不开浏览器不算致命
        pass

    print(f"[启动] 浏览器没能自动打开，请手动访问：{url}")


def _open_when_ready(port: int, timeout: float = 20.0) -> None:
    """等端口真能连上再打开浏览器。

    不等就打开的话，Edge 抢在服务前面，用户看到的是一个连接失败页，
    还得自己知道要刷新——「打开就报错」。多等这几十毫秒换一个干净的首屏。
    """
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                _open_in_edge(url)
                return
        except OSError:
            time.sleep(0.05)
    print(f"[启动] 服务在 {timeout:.0f} 秒内没起来，请看上面的报错。")


def _serve() -> int:
    import uvicorn

    port = _pick_port()
    url = f"http://127.0.0.1:{port}/"

    print("\n  Word Learning")
    print(f"  {url}")
    if port != DEFAULT_PORT:
        print(f"  （{DEFAULT_PORT} 被占用了，换到 {port}）")
    print("\n  正在用 Edge 打开。关掉这个窗口就退出程序。\n")

    threading.Thread(target=_open_when_ready, args=(port,), daemon=True).start()

    # log_level 压到 warning：正常运行时每个请求刷一行没有意义，
    # 而真出错的 traceback 仍然会打出来。
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_serve())
    except KeyboardInterrupt:
        sys.exit(0)
