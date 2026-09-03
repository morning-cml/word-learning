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


# ------------------------------------------------------- 模型列表的元素形状

def test_模型_id_不是字符串时不漏出_TypeError(respond):
    """这个函数声明返回 list[str]，调用方就照着这个用。

    四层检验的 L1 会把它拼进一句话：`"、".join(ids[:5])`。列表里混进一个
    数字或对象，抛的是 TypeError——不是 ProviderError，谁都没接，
    于是整次检验变成 HTTP 500，后面三层一层都跑不到。而这恰好只在
    base_url 指到了某个奇怪的代理上时才发生，也就是最需要它说人话的场合。
    """
    respond(lambda req: httpx.Response(200, json={"data": [
        {"id": "deepseek-v4-pro"},
        {"id": 123},
        {"id": {"name": "weird"}},
        {"id": None},
        {"no_id": "x"},
        "不是对象",
    ]}))

    ids = _provider().list_models()
    assert ids == ["deepseek-v4-pro"]
    assert all(isinstance(i, str) for i in ids)
    "、".join(ids[:5])          # L1 真正会做的事，不能抛


# ------------------------------------------------------------ 不认识的提供商

def test_未知提供商的报错不带引号():
    """这几个出口都是直接把 str(exc) 交给用户看的。

    KeyError.__str__ 返回的是 repr(args[0])，于是设置页上显示的是
    `'未知的模型提供商：kimi'`——中文被一对单引号裹着。这几个出口存在的
    理由恰恰是「显示人话」。仍然要是 KeyError 的子类，调用方那几处
    `except KeyError` 一行都不该改。
    """
    with pytest.raises(KeyError) as err:
        registry.get_spec("不存在的一家")
    assert str(err.value) == "未知的模型提供商：不存在的一家"
    assert "'" not in str(err.value)


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


# ------------------------------------------------- 能力按模型声明，不是按厂商

RAW = {
    "label": "X", "base_url": "https://x/v1",
    "models": [
        {"id": "reasoner"},
        {"id": "plain",
         "capabilities": {"json_schema": True},
         "reasoning": {"counts_toward_max_tokens": False, "headroom": 0,
                       "disable": {}, "disable_for": []},
         "temperatures": {"creative": 0.7}},
    ],
    "capabilities": {"json_object": True, "json_schema": False},
    "reasoning": {"counts_toward_max_tokens": True, "headroom": 12000,
                  "disable": {"thinking": {"type": "disabled"}}, "disable_for": ["probe"]},
    "temperatures": {"creative": 1.5, "probe": 0.0},
}


def _spec():
    from core.provider.registry import _spec_from_dict

    return _spec_from_dict("x", RAW)


def test_没单独声明的模型拿厂商那一份():
    """绝大多数模型没有 overrides，这条路径每次调用都会走到，
    所以它必须是零成本的——直接返回同一个对象，不是每次造一份新的。"""
    spec = _spec()
    assert spec.for_model("reasoner") is spec
    assert spec.for_model("从没听说过的模型") is spec


def test_模型级声明覆盖厂商级():
    """同一家的模型能力不一样。本仓库自己就有裂缝：deepseek 段落下三个模型
    共用一份 reasoning.disable，换一个不认识 thinking 参数的模型，
    探针就会发一个它不认识的字段，回来是一个含糊的 400。"""
    plain = _spec().for_model("plain")
    assert plain.capabilities.json_schema is True
    assert plain.reasoning.budget(3000) == 3000, "不该再追加思考预算"
    assert plain.reasoning.params_for("probe") == {}, "不该再发 thinking 参数"


def test_只覆盖写出来的那几个键():
    """浅合并：模型段里没写的键仍然继承厂商的，
    否则每加一个模型级 override 都要把整段抄一遍，抄漏了没人会发现。"""
    plain = _spec().for_model("plain")
    assert plain.capabilities.json_object is True, "厂商声明的 json_object 该留着"
    assert plain.temperature_for("creative") == 0.7, "模型自己写了的要生效"
    assert plain.temperature_for("probe") == 0.0, "模型没写的要继承厂商的"
    assert plain.base_url == "https://x/v1" and plain.label == "X"


def test_变体不再嵌套():
    """解析一层就够。变体自己再带 per_model 的话，for_model 会一路递归下去，
    而这个链条没有任何地方限制得住深度。"""
    assert _spec().for_model("plain").per_model == {}


def test_真实配置里每个模型都解析得出来():
    """providers.yaml 改坏了（比如某个模型段缩进错位）要在这里挂，
    而不是等用户选到那个模型时才在生成中途炸。"""
    from core.provider import registry

    for pid, spec in registry.load_specs().items():
        for model in spec.models:
            resolved = spec.for_model(model.id)
            assert resolved.base_url == spec.base_url, f"{pid}/{model.id}"
            assert isinstance(resolved.reasoning.budget(1000), int)
