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
