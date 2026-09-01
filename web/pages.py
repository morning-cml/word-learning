"""页面注册表。

新增一个页面的完整流程：

    1. 在下面的 PAGES 里加一条；
    2. 建 web/templates/<id>.html（继承 base.html）；
    3. 需要交互就建 web/static/js/pages/<id>.js，导出一个 init()；不需要就不建。

    结束——main.py、base.html 一行都不用改：路由由 register() 循环注册，
    导航栏由 base.html 遍历 nav_pages() 渲染。

这么做是因为参考实现里导航是在模板里硬编码的，七个入口写了七遍
`class="{{ 'active' if request.path == '/x' }}"`——加第八个要改三处，
而且总有一处会忘。把「有哪些页面」收成一份数据，这类遗漏就不存在了。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


@dataclass(frozen=True)
class Page:
    id: str                     # 也是模板名与 pages/<id>.js 的文件名
    path: str                   # 路由。支持 Starlette 的路径参数写法
    label: str                  # 导航文字
    title: str                  # 浏览器标题
    icon: str = ""              # 导航前的小图标，留空则不显示
    in_nav: bool = True         # 详情页这类不进导航
    nav_match: str = ""         # 高亮判定的前缀，留空则用 path 精确匹配


PAGES: tuple[Page, ...] = (
    Page(id="index",    path="/",                    label="生成", title="生成文章", icon="✎"),
    Page(id="library",  path="/library",             label="文库", title="文库",     icon="▤"),
    Page(id="words",    path="/words",               label="词库", title="词库",     icon="◈"),
    Page(id="settings", path="/settings",            label="设置", title="设置",     icon="⚙"),
    Page(id="reader",   path="/read/{article_id:int}", label="阅读", title="阅读",
         in_nav=False, nav_match="/read/"),
)


def nav_pages() -> list[Page]:
    return [p for p in PAGES if p.in_nav]


def active_id(path: str) -> str:
    """当前路径命中哪个页面。给模板判断导航高亮用。"""
    for page in PAGES:
        if page.nav_match:
            if path.startswith(page.nav_match):
                return page.id
        elif path == page.path:
            return page.id
    return ""


def register(app, templates: Jinja2Templates) -> None:
    """把 PAGES 注册成路由。

    走 Starlette 的 add_route 而不是 FastAPI 的 add_api_route：后者会拿函数签名
    去校验路径参数，而这里的视图是通用的（只收 Request），声明不了 article_id。
    路径参数照样能从 request.path_params 里拿到。
    """
    for page in PAGES:
        app.add_route(page.path, _view(page, templates), methods=["GET"], name=page.id)


def _view(page: Page, templates: Jinja2Templates):
    def view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            f"{page.id}.html",
            {
                "page": page,
                "params": dict(request.path_params),
                # 导航所需的两项在这里注入，模板里就不用各自去 import 注册表
                "nav_pages": nav_pages(),
                "active": active_id(request.url.path),
            },
        )
    return view
