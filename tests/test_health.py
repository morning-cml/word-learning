"""四层模型检验，重点是 L4。

L1-L3 验的全是「产出的形状对不对」，恰好把唯一要紧的能力漏在外面：
这个产品的价值判定全压在语境线索审计那一次调用上，而它判错时用户
察觉不到——能看出「这个语境够不够推出词义」的人，本来就已经认识那个词了。
没有反馈回路的错误不会自我纠正，只能在入口一次性验掉。
"""
from __future__ import annotations

import pytest

from core import health


class Verdicts:
    """按测试给定的结论作答的假模型。"""

    def __init__(self, verdicts):
        self.verdicts = verdicts

    def json(self, messages, **kw):
        return {"audits": [{"lemma": k, "strength": v} for k, v in self.verdicts.items()]}


def problems(verdicts):
    """复刻 health.check 里 L4 的判定，返回它会报出的问题列表。"""
    got = health._calibrate(Verdicts(verdicts))
    out = []
    missing = [w for w in health.CALIBRATION_WORDS if not got.get(w)]
    if missing:
        out.append("漏审")
    if got.get("meticulous") and got["meticulous"] != "strong":
        out.append("强线索判错")
    if got.get("tedious") == "strong":
        out.append("无线索判 strong")
    return out


@pytest.mark.parametrize("name,verdicts,expect", [
    ("理想模型",              {"tedious": "none", "meticulous": "strong"}, []),
    ("无线索判 weak 也可接受", {"tedious": "weak", "meticulous": "strong"}, []),
    ("橡皮图章（全判 strong）", {"tedious": "strong", "meticulous": "strong"},
     ["无线索判 strong"]),
    ("过严（全判 none）",     {"tedious": "none", "meticulous": "none"}, ["强线索判错"]),
    ("漏审一个",              {"meticulous": "strong"}, ["漏审"]),
    ("全漏",                  {}, ["漏审"]),
])
def test_审计校准的判定矩阵(name, verdicts, expect):
    """两个方向都要拦住：见词就说 strong 的（审计变橡皮图章）
    和见词就说 none 的（每段白烧两轮补线索）。"""
    assert problems(verdicts) == expect, name


def test_定标文本同时含强线索与无线索的词():
    """放在一次调用里而不是两次，测的才是「分辨力」而不是「倾向」。"""
    assert set(health.CALIBRATION_WORDS) == {"tedious", "meticulous"}
    for w in health.CALIBRATION_WORDS:
        assert w in health.CALIBRATION_TEXT


def test_L4_用的是真正在跑的那段_prompt():
    """在 health.py 里另抄一份 prompt，就变成「校验通过但实际审计仍然失灵」。"""
    import inspect

    src = inspect.getsource(health._calibrate)
    assert "from tasks.article.prompts import audit_prompt" in src
    assert "coerce_audits" in src


def test_没填_Key_时不发请求():
    r = health.check("deepseek", "deepseek-v4-pro", "")
    assert r["ok"] is False
    assert r["steps"][0]["error"]
    assert [s["id"] for s in r["steps"]] == ["connect", "json", "task", "clue"]


# --------------------------------------------------------------- 检验本身不能崩

class _StubProvider:
    """L1/L2 都过的假端点，好让检验一路走到 L3。"""

    def __init__(self, spec):
        self.spec = spec

    def list_models(self):
        return ["deepseek-v4-pro"]

    def chat(self, messages, **kw):
        from core.provider.base import ChatResult

        return ChatResult(text='{"ok": true}', model="m", ms=1)


@pytest.fixture
def stub_llm(monkeypatch):
    """把 L3 的返回值换成指定形状，L4 一律给一个判得准的结果。"""
    from core.llm.client import LLM
    from core.provider import registry

    def apply(l3_payload):
        monkeypatch.setattr(
            registry, "build",
            lambda pid, key, **kw: _StubProvider(registry.get_spec(pid)))

        def fake_json(self, messages, **kw):
            if "river" in messages[-1]["content"]:        # L3
                return l3_payload
            return {"audits": [{"lemma": "tedious", "strength": "none"},
                               {"lemma": "meticulous", "strength": "strong"}]}

        monkeypatch.setattr(LLM, "json", fake_json)

    return apply


@pytest.mark.parametrize("name,payload", [
    ("顶层是数组",         [{"en": "The river kept its promise and would return.", "zh": "河流守约。"}]),
    ("sentences 里是字符串", {"sentences": ["river promise return"]}),
    ("顶层是字符串",       "抱歉我不能完成"),
    ("顶层是数字",         5),
    # 上面四条守的是**容器**的形状。下面这几条守的是**值**——容器对、
    # 但 en / zh 不是字符串。schema.py 的 _text 就是为这件事存在的
    # （「模型偶尔把 title_en 写成对象」），而 health.py 另起了一套取值方式，
    # 那道防线没跟过来：`(s.get("en") or "").strip()` 抛的 AttributeError
    # 不在 except 的捕获范围里，整次检验变成 HTTP 500，L4 一次都跑不到。
    ("en 是对象",          {"sentences": [{"en": {"text": "river"}, "zh": "河。"}]}),
    ("zh 是对象",          {"sentences": [{"en": "The river returns.", "zh": {"t": "河。"}}]}),
    ("en 是数字",          {"sentences": [{"en": 123, "zh": "河。"}]}),
    ("en 是数组",          {"sentences": [{"en": ["river", "promise"], "zh": "河。"}]}),
    ("zh 是 null",         {"sentences": [{"en": "The river returns.", "zh": None}]}),
])
def test_L3_拿到畸形形状时报告而不是崩(stub_llm, name, payload):
    """检验在它该报告问题的那一刻崩掉，等于这一层不存在。

    「顶层是数组」「sentences 里躺着字符串」都是真实发生过的形状。
    原先这里直接 doc.get()，抛出的 AttributeError 不在 except 的捕获范围里，
    会一路冒到接口层变成 HTTP 500：用户看到「检验请求失败」，
    而不是「任务验收没过、原因是顶层不是对象」。
    """
    stub_llm(payload)
    r = health.check("deepseek", "deepseek-v4-pro", "sk-test")

    steps = {s["id"]: s for s in r["steps"]}
    assert steps["task"]["ok"] is False, name
    assert steps["task"]["error"], f"{name}：没过就得说清为什么"


@pytest.mark.parametrize("payload", [
    {"sentences": [{"en": {"text": "river"}, "zh": "河。"}]},
    {"sentences": [{"en": "The river returns.", "zh": {"t": "河。"}}]},
    {"sentences": [{"en": 123, "zh": 456}]},
])
def test_值不是字符串时_L4_照样跑得到(stub_llm, payload):
    """和下面那条同一个道理，换成「值畸形」这一类。

    L4 是这个产品唯一验「审计判不判得准」的地方。L3 因为一个不是字符串的
    en 就把整次请求打断的话，用户看到的只有一句「检验请求失败」——
    最该跑的那一层反而永远跑不到。
    """
    stub_llm(payload)
    steps = {s["id"]: s for s in health.check("deepseek", "deepseek-v4-pro", "sk-test")["steps"]}
    assert steps["task"]["ok"] is False
    assert steps["clue"]["ok"] is True, "L4 应该照常跑完并给出结论"


def test_L3_挂掉不影响最要紧的_L4(stub_llm):
    """L4 验的是「审计判不判得准」——这个产品的价值判定全压在那次调用上。
    L3 那边形状不对就把整次检验打断的话，它一次都跑不到。"""
    stub_llm([{"en": "x", "zh": "y"}])
    steps = {s["id"]: s for s in health.check("deepseek", "deepseek-v4-pro", "sk-test")["steps"]}
    assert steps["task"]["ok"] is False
    assert steps["clue"]["ok"] is True, "L4 应该照常跑完并给出结论"


def test_L3_验收标准():
    """L3 要拦的是「模型吐得出格式但干不了活」——中文整片漏掉是最常见的一种。"""
    assert health._audit("不是对象") == ["顶层不是 json 对象"]
    assert health._audit({}) == ["缺少 sentences 数组"]

    only_en = {"sentences": [{"en": "The river kept its promise and did not return.",
                              "targets": [{"lemma": "river", "surface": "river"}]}]}
    assert any("缺中文" in p for p in health._audit(only_en))

    missing_word = {"sentences": [{"en": "Nothing here.", "zh": "什么都没有。",
                                   "targets": [{"lemma": "x", "surface": "x"}]}]}
    assert any("目标词未命中" in p for p in health._audit(missing_word))

    good = {"sentences": [{"en": "The river kept its promise and would return.",
                           "zh": "河流守住承诺，还会回来。",
                           "targets": [{"lemma": "river", "surface": "river"}]}]}
    assert health._audit(good) == []


@pytest.mark.parametrize("value", [{"text": "x"}, ["x"], 5, None, True])
def test_取值对任意类型都不抛(value):
    """_field 是 health.py 这一侧的 `_text`：非字符串一律当空，绝不抛。

    这一层的存在理由就是「模型可能吐出形状不对的东西」，所以它自己对输入
    不能有任何假设——校验代码比被校验的代码更不能有假设（需要注意.md 第 1 条）。
    """
    assert health._field({"en": value}, "en") == ""
    assert health._field(value, "en") == ""
    # 崩不崩是关键；报出来的内容对不对是顺带
    issues = health._audit({"sentences": [{"en": value, "zh": value}]})
    assert any("英文为空" in p for p in issues)
    assert any("缺中文" in p for p in issues)
