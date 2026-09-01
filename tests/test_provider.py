"""OpenAI 兼容端点这一层。

这一层对外只有一条约定：**任何失败都变成一个带上下文的 ProviderError**，
设置页才有人话可显示、四层检验才报得出是哪一层不行。
所以这里测的不是「正常时能不能解析」，而是「不正常时会不会漏出去一个
别人接不住的异常」——漏出去的那个会一路冒到接口层变成 HTTP 500，
用户看到的是「检验请求失败」，而不是「base_url 指错了」。
"""
from __future__ import annotations

import httpx
import pytest

from core.provider import registry
from core.provider.base import ProviderError

SPEC = "deepseek"

OK_BODY = {
    "model": "deepseek-v4-pro",
    "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
    "usage": {"total_tokens": 12, "completion_tokens": 4},
}


@pytest.fixture
def respond(monkeypatch):
    """让端点按测试指定的方式作答。"""

    real_client = httpx.Client      # 先抓住真的那个，否则替身会调用自己

    def apply(handler):
        monkeypatch.setattr(
            httpx, "Client",
            lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)))

    return apply


def _provider():
    return registry.build(SPEC, "sk-test")


def _chat():
    return _provider().chat([{"role": "user", "content": "hi"}], model="deepseek-v4-pro")


# ------------------------------------------------------------- 不是 JSON 的响应

def test_200_但返回网页时报人话而不是抛_ValueError(respond):
    """base_url 指到网页 / 代理 / 另一种协议上时，拿回来的常常是一个 200 的
    HTML 页。r.json() 抛的 ValueError 不在任何调用方的捕获范围里。"""
    respond(lambda req: httpx.Response(200, text="<html><body>登录</body></html>"))

    with pytest.raises(ProviderError) as err:
        _chat()
    assert "不是 JSON" in str(err.value)
    assert err.value.body, "要把原样的响应体带上，否则没法判断到底连到了哪"


def test_列模型时同样不漏出去(respond):
    respond(lambda req: httpx.Response(200, text="not json at all"))

    with pytest.raises(ProviderError):
        _provider().list_models()


def test_顶层不是对象时报人话(respond):
    respond(lambda req: httpx.Response(200, json=[{"choices": []}]))

    with pytest.raises(ProviderError) as err:
        _chat()
    assert "顶层不是对象" in str(err.value)


# ------------------------------------------------------------- 形状不对但合法

@pytest.mark.parametrize("body", [
    {},                                        # 什么都没有
    {"choices": []},                           # 空数组
    {"choices": "nope"},                       # 不是数组
    {"choices": ["文本"]},                     # 元素不是对象
    {"choices": [{"message": None}]},          # message 是 null
    {"choices": [{"message": {"content": 5}}], "usage": "x"},   # 类型全错
])
def test_合法但形状不对的响应不抛(respond, body):
    """空响应是 DeepSeek 官方承认的已知抖动，由 llm/client.py 负责重试。
    在这一层抛出来的话，走的就不是重试那条路了。"""
    respond(lambda req, b=body: httpx.Response(200, json=b))

    res = _chat()
    assert res.text == ""
    assert isinstance(res.usage, dict)


def test_正常响应照常解析(respond):
    respond(lambda req: httpx.Response(200, json=OK_BODY))

    res = _chat()
    assert res.text == '{"ok": true}'
    assert res.finish_reason == "stop"
    assert res.total_tokens == 12


# ------------------------------------------------------------------ 状态码翻译

@pytest.mark.parametrize("status,keyword", [
    (401, "Key"), (402, "余额"), (404, "下线"), (429, "限流"),
])
def test_状态码翻译成人话(respond, status, keyword):
    """先判状态码再读 body：500 配一个 HTML 错误页时，该报的是「服务端错误」，
    不是「返回的不是 JSON」。"""
    respond(lambda req: httpx.Response(status, text="<html>error</html>"))

    with pytest.raises(ProviderError) as err:
        _chat()
    assert keyword in str(err.value)
    assert err.value.status == status
