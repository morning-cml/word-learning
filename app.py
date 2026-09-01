"""Word Learning 桌面端入口。

日常用法是双击 run.bat（它会自愈环境后用 pythonw 无控制台启动）。
直接跑也行：
    .venv\\Scripts\\python.exe app.py

界面就是 web/ 那一套。这里做的事情是：起一个只监听 127.0.0.1 的本地服务，
再用一个 Qt 窗口把它显示出来。用户看到的仍然是一个应用窗口——没有浏览器、
没有地址栏、不用先启动什么。

为什么不再用 Qt 控件自己画一套界面：那等于同一个产品维护两份 UI，
每加一个功能都要写两遍，而漏掉的那一遍必然是后写的那端。实际也确实漏了——
词库页、每个词的命中/未命中标记、残留超纲词都只有一边有。
代价是启动从 0.6 秒变成 2 秒左右，换掉长期双写，值得。

注意：用 pythonw 启动时没有控制台，任何异常都会静默消失。所以这里把整个
启动过程包在兜底里——出错时写日志 + 弹一个系统对话框，绝不无声退出。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "data" / "last-error.log"

# pythonw 启动时没有控制台，sys.stdout / sys.stderr 都是 None。
# 任何 print、任何往 stdout 装 handler 的库都会当场炸——uvicorn 就是其中之一。
# 指到空设备上，让第三方库按正常路径走。必须在 import 它们之前做。
for _name in ("stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

# 必须在 QtWebEngine 初始化之前设好。
# --disable-gpu 不设：本机有 GPU 时硬件加速能省下一点首帧时间；
# 真出问题（老显卡、远程桌面）时可以用环境变量覆盖这一行。
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-features=Translate,AutofillServerCommunication",
)


def _report(title: str, message: str) -> None:
    """把错误同时写进日志和弹窗。

    这里刻意不依赖 PySide6——如果挂的正是 PySide6 本身，Qt 弹窗也起不来。
    用 ctypes 直接调 Win32 的 MessageBoxW，零依赖。
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(message, encoding="utf-8")
    except OSError:
        pass

    sys.stderr.write(message + "\n")
    try:
        import ctypes

        shown = message if len(message) < 1500 else message[:1500] + "\n…（完整内容见日志）"
        ctypes.windll.user32.MessageBoxW(
            None, f"{shown}\n\n完整日志：{LOG_PATH}", title, 0x10)
    except Exception:  # noqa: BLE001  弹窗失败也不能再抛
        pass


def _free_port() -> int:
    """要一个空闲端口。不写死 8000：用户可能自己开着 main.py 在调试。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(app, port: int, failure: list) -> None:
    """在后台线程里跑本地服务，异常收集到 failure 里。

    log_config=None 不是可有可无：uvicorn 默认要往 stdout 装日志 handler，
    而 pythonw 下 stdout 是 None，装的时候直接抛。线程里的异常没人接，
    表现出来只是「端口一直连不上」——真正的原因被完全盖住。
    这个坑只在双击 run.bat（pythonw）时出现，用 python.exe 跑是好的，
    所以必须按真实启动方式测，不能只测 python app.py。
    """
    try:
        import uvicorn

        uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_config=None, access_log=False,
        )).run()
    except BaseException:  # noqa: BLE001  线程里的异常必须带出去，否则无从排查
        failure.append(traceback.format_exc())


def _wait_ready(port: int, timeout: float = 20.0) -> bool:
    """等端口真的能连上再加载页面。

    直接 load 也能用（Chromium 会显示一个连接失败页再让人手动刷新），
    但用户看到的就是「打开就报错」。多等这几十毫秒换一个干净的首屏。
    """
    from time import monotonic, sleep

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                return True
        except OSError:
            sleep(0.02)
    return False


def _run() -> int:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    from core.store import db

    db.init_db()

    app = QApplication(sys.argv)
    app.setApplicationName("Word Learning")
    app.setOrganizationName("Word Learning")

    # 服务先起：QApplication 构造和 Chromium 初始化要花一点时间，
    # 让它和服务启动并行，省掉一段串行等待。
    import main as backend

    port = _free_port()
    failure: list[str] = []
    threading.Thread(target=_serve, args=(backend.app, port, failure), daemon=True).start()

    class Page(QWebEnginePage):
        """只放行本地页面，外链交给系统浏览器。

        设置页上有「去 DeepSeek 申请 Key」这类外链。放任它在窗口内打开的话，
        用户会被带离应用而且回不来（没有地址栏和后退键）；
        target=_blank 的链接在嵌入视图里则是直接没反应。
        """

        def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame) -> bool:  # noqa: N802
            if url.host() in ("127.0.0.1", "localhost") or url.scheme() in ("data", "about"):
                return True
            QDesktopServices.openUrl(url)
            return False

        def createWindow(self, _type):  # noqa: N802  target=_blank 也走系统浏览器
            holder = QWebEnginePage(self)
            holder.urlChanged.connect(
                lambda u: (QDesktopServices.openUrl(u), holder.deleteLater()))
            return holder

    window = QMainWindow()
    window.setWindowTitle("Word Learning")
    window.resize(1240, 820)
    view = QWebEngineView(window)
    view.setPage(Page(view))
    window.setCentralWidget(view)

    if not _wait_ready(port):
        raise RuntimeError("本地服务没能起来。\n\n" + (
            failure[0] if failure else
            f"127.0.0.1:{port} 一直连不上，可能是端口被占用或被安全软件拦截。"))

    view.load(QUrl(f"http://127.0.0.1:{port}/"))
    window.show()
    return app.exec()


def main() -> int:
    try:
        return _run()
    except ImportError as exc:
        _report("Word Learning 启动失败", (
            f"缺少依赖：{exc}\n\n"
            "多半是依赖没装全或虚拟环境坏了。\n"
            "解决办法：删掉项目里的 .venv 目录，再双击 run.bat 重建。\n\n"
            + traceback.format_exc()))
        return 2
    except BaseException:  # noqa: BLE001  无控制台环境下绝不能静默退出
        _report("Word Learning 出错", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
