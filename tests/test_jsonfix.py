"""JSON 的四层兜底解析。

这个文件补的是一处空白：`jsonfix` 是「模型返回的东西不合规矩时」唯一的
防线，此前一条测试都没有。而它失效的样子恰好是这个项目最怕的那种——
**不报错，只是多花一次三十秒的调用**：解析不出来时调用方会重试，
于是「第 4 层从来没救回来过」和「第 4 层工作正常」在外面看起来一模一样。

第 4 层（截断补齐）的测试因此写成两条主张：
  1. 该救回来的必须救回来（否则白烧钱，而且没人看得出来）；
  2. 救回来的内容必须是原文的前缀，不能是编出来的（比白烧钱严重得多）。
"""
from __future__ import annotations

import json

import pytest

from core.llm import jsonfix

#  管线真正会产出的形状：一段里若干个句子，每句 en/zh/targets。
#  正文里逗号遍地都是——这正是老实现栽掉的地方。
PARAGRAPH = {
    "sentences": [
        {"en": "The shop was abandoned, and the silence stayed on.",
         "zh": "小店废弃了，寂静留了下来。",
         "targets": [{"lemma": "abandon", "surface": "abandoned"}]},
        {"en": "No one came, no one left, and the dust settled.",
         "zh": "没人来，也没人走，灰落了下来。",
         "targets": [{"lemma": "silence", "surface": "silence"}]},
    ]
}


def _dumped(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------- 前三层

def test_干净的_json_原样解析():
    assert jsonfix.loads(_dumped(PARAGRAPH)) == PARAGRAPH


def test_抠掉_code_fence():
    assert jsonfix.loads("```json\n" + _dumped(PARAGRAPH) + "\n```") == PARAGRAPH


def test_前后有解释文字也能截出来():
    assert jsonfix.loads("好的，这是你要的结果：\n" + _dumped(PARAGRAPH) + "\n希望有帮助") == PARAGRAPH


def test_尾随逗号():
    assert jsonfix.loads('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}


@pytest.mark.parametrize("junk", ["", "   ", "抱歉，我不能完成这个请求。", "{{{", '"just a string'])
def test_救不回来时抛的是_JsonParseError(junk):
    """必须是这一个异常——调用方（client.py / health.py）只接它。
    漏出别的异常会一路冒到接口层变成 HTTP 500。"""
    with pytest.raises(jsonfix.JsonParseError):
        jsonfix.loads(junk)


# --------------------------------------------------- 第 4 层：被 max_tokens 截断

def _truncate_at(text: str, marker: str) -> str:
    """在 marker 首次出现处砍断，模拟 max_tokens 用尽。"""
    i = text.index(marker)
    assert i > 0
    return text[:i]


def test_截在第二句中间时救回第一句():
    """管线里最常见的一次截断。老实现在这里一次都没救回来过：
    rfind(",") 找到的是第一句正文里的逗号，截点落在句子中间，
    而括号栈又是照末尾算的——补出来的必然不合法。"""
    cut = _truncate_at(_dumped(PARAGRAPH), "no one left")
    got = jsonfix.loads(cut)
    assert got["sentences"][0] == PARAGRAPH["sentences"][0]


@pytest.mark.parametrize("frag,expect", [
    # 末尾字符串里有逗号：截点必须落在字符串之外
    ('{"a": 1, "b": "hello, world"', {"a": 1, "b": "hello, world"}),
    ('{"words": ["a, b", "c"]',      {"words": ["a, b", "c"]}),
    # 截点和括号栈要取自同一处：这里末尾那个 } 会被截掉，
    # 若仍按末尾的栈收尾就会少补一个 }
    ('{"p": [{"s": "x, y"}, {"s": "q', {"p": [{"s": "x, y"}]}),
    # 截在冒号后 / 截在逗号后：末尾那半个键值对要丢掉
    ('{"a": 1, "b": ',               {"a": 1}),
    ('{"list": [{"a": 1},',          {"list": [{"a": 1}]}),
    # 没有可回退的逗号时，把引号补上总比什么都不救强
    ('{"en": "half a sen',           {"en": "half a sen"}),
    # 转义引号不能被当成字符串结束
    ('{"a": "he said \\"hi\\", then left", "b": "n', {"a": 'he said "hi", then left'}),
    # 前言 / code fence 和截断同时发生
    ('好的：{"a": 1, "b": "x, y"',    {"a": 1, "b": "x, y"}),
    ('```json\n{"a": 1, "b": "x, y"', {"a": 1, "b": "x, y"}),
])
def test_截断补齐(frag, expect):
    assert jsonfix.loads(frag) == expect


def test_救回来的内容只会是原文的前缀():
    """扫过每一个截断点：救回来的东西必须逐字来自模型的输出。

    补齐括号这件事一旦把内容也改了，得到的是一篇「看着正常、但不是模型写的」
    文章——比解析失败严重得多，而且没有任何地方会报出来。
    允许最后一句是残的（截断本来就砍在句子中间），但只能是前缀，
    不能凭空多出句子，也不能串位。
    """
    original = PARAGRAPH["sentences"]
    text = _dumped(PARAGRAPH)
    recovered = 0
    for i in range(1, len(text)):
        try:
            got = jsonfix.loads(text[:i])
        except jsonfix.JsonParseError:
            continue
        assert isinstance(got, dict), text[:i]
        sentences = got.get("sentences") or []
        assert len(sentences) <= len(original), text[:i]
        for k, sent in enumerate(sentences):
            for key in ("en", "zh"):
                if key in sent:
                    assert original[k][key].startswith(sent[key]), (i, k, key)
        recovered += 1
    assert recovered, "一个截断点都没救回来的话，这一层等于不存在"


def test_第一句完整之后每个截断点都救得回来():
    """这一层的全部价值：第一句已经写完了，就不该为了后半段的残缺
    把它一起扔掉——扔掉的代价是整段重来一次，三十秒起步，而且用户
    只会看到「重试第 1 次」，不会知道本来是救得回来的。"""
    text = _dumped(PARAGRAPH)
    first_done = text.index("}, {") + 1        # 第一句的对象闭合之后
    for i in range(first_done, len(text)):
        got = jsonfix.loads(text[:i])          # 救不回来就会抛，这条即不成立
        assert got["sentences"][0] == PARAGRAPH["sentences"][0], text[:i]


@pytest.mark.parametrize("frag", [
    "{",
    "[",
    '{"sentences": [',
    '{"audits": [',
    '{"sentences":[{',
    '{"sentences": [{"en": "',
])
def test_补出来的空壳不算救回来(frag):
    """截断点落在第一个值出现之前时，补齐只会造出一个空文档。

    实际拿到过的三种：`{` → `{}`、`{"sentences": [` → `{"sentences": []}`、
    `{"sentences":[{` → `{"sentences": [{}]}`——最后一种还凭空多造了一句，
    直接违反这一层自己那条「宁可少一句，也不要凭空多一句模型没写的」。

    代价不是「解析失败」这么轻，而是**沉默地花钱**：审计那次调用被截在开头时，
    `{"audits": []}` 会被 audit_clues 读成「所有词都没有线索」，于是一段本来
    合格的文章要挨两轮补线索改写，还可能被改坏，界面上只显示一句
    「第 N 段线索不足」。抛出去让 client.py 重试才是对的。
    """
    with pytest.raises(jsonfix.JsonParseError):
        jsonfix.loads(frag)


@pytest.mark.parametrize("frag,expect", [
    # 有一个字是模型真写的，就不能因为「其余是空的」把它一起扔掉
    ('{"a": 1, "b": ',                 {"a": 1}),
    ('{"sentences": [{"en": "x"',      {"sentences": [{"en": "x"}]}),
    ('{"audits": [{"lemma": "tedious", "strength": ',
     {"audits": [{"lemma": "tedious"}]}),
    ('{"a": 0}',                       {"a": 0}),      # 0 / false 是内容，不是空
    ('{"a": false}',                   {"a": False}),
])
def test_救回了内容就照常返回(frag, expect):
    """上一条那道闸只拦「一个字都没救到」，不能顺手把救到一点的也扔了。"""
    assert jsonfix.loads(frag) == expect


def test_没被截断的输入不进第_4_层():
    """第 4 层只负责救残缺的东西。对完整输入还去动它，
    等于给一条本来就对的路径加了一个改坏它的机会。"""
    assert jsonfix._repair_truncated(_dumped(PARAGRAPH)) == []
    assert jsonfix._repair_truncated('{"a": 1}') == []
    assert jsonfix._repair_truncated("这里面一个括号都没有") == []


# ------------------------------------------- 第 5 层：json_repair（可选依赖）

jsonrepair = pytest.mark.skipif(
    jsonfix._json_repair is None, reason="没装 json-repair，这一层是加分项")


@jsonrepair
@pytest.mark.parametrize("name,raw,expect", [
    ("结构位置的中文引号",   '{“ok”: 1}',                        {"ok": 1}),
    ("正文里没转义的引号",   '{"en": "He said "go", then left."}',
                             {"en": 'He said "go", then left.'}),
    ("正文里没转义的换行",   '{"zh": "第一行\n第二行"}',          {"zh": "第一行\n第二行"}),
    ("单引号",              "{'a': 1, 'b': 'x'}",                {"a": 1, "b": "x"}),
    ("Python 字面量",       '{"a": None, "b": True}',            {"a": None, "b": True}),
])
def test_第5层修得了前四层修不了的畸形(name, raw, expect):
    assert jsonfix.loads(raw) == expect, name


@jsonrepair
@pytest.mark.parametrize("zh", [
    "他说“走吧”，然后走了。",       # 正文里的中文双引号
    "他读了《小王子》，很喜欢。",     # 书名号
    "他停了一下——然后……走了。",     # 破折号与省略号
    "「引号」和『引号』都要留着。",   # 直角引号
])
def test_第5层不会动正文里的中文标点(zh):
    """本模块顶上那条「中文引号一律不管」的理由是「全局替换会改坏正文」——
    那条理由只对正则替换成立。真解析器分得清结构位置和字符串内部，
    所以这一层进来之后，正文里的中文标点必须一个字不动。
    """
    assert jsonfix.loads('{"zh": "' + zh + '", "en": "ok"}')["zh"] == zh


@jsonrepair
@pytest.mark.parametrize("junk", ["", "   ", "抱歉，我不能完成这个请求。",
                                  "I cannot help with that.", "这是文章：\n\n第一段……"])
def test_第5层不把纯文字变成一个空文档(junk):
    """json_repair 对纯文字返回的是 `""`——照单收下就等于把「模型拒绝回答」
    悄悄变成「成功解析出一个空文档」。这个项目宁可报错也不要这种成功
    （见 需要注意.md 第 2 条）。"""
    with pytest.raises(jsonfix.JsonParseError):
        jsonfix.loads(junk)


@jsonrepair
def test_截断的输入不交给第5层():
    """json_repair 修截断的办法是补默认值：实测 `{"sentences": [{"e`
    会被补成 `{"sentences": [["e"]]}`——把半个键名编成了一个值。
    第 4 层只丢不补，残缺的输入到它为止。"""
    with pytest.raises(jsonfix.JsonParseError):
        jsonfix.loads('{"sentences": [{"e')
