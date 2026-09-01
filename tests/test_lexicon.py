"""词表与词形还原。

这里覆盖的两件事都属于「错了也不会报错，只会安静地给出错结果」那一类，
所以必须有测试盯着：

  · 超纲检测漏掉句首词  —— 模型只要把生词放句首就能绕开整把难度标尺
  · 词形还原把别的词判成目标词 —— 校验被骗过去，错的词还会写进词条库
"""
from __future__ import annotations

import pytest

from core.lexicon import cefr
from core.lexicon.lemma import IRREGULAR, forms_of, lemma_candidates, same_word


# --------------------------------------------------------------- 超纲检测

def offenders(text, level="B2", **kw):
    return {o["surface"] for o in cefr.scan(text, level, **kw)["offenders"]}


def test_句首生词不能逃过检测():
    """曾经的写法是「大写就跳过」，副作用是每句第一个词都不检查。"""
    assert "Meticulous" in offenders("Meticulous work matters. He was meticulous.")


def test_句首与句中判定一致():
    """同一个词换个位置，结论不该变；总词数也该一样。"""
    a = cefr.scan("Meticulous work matters. He was meticulous.", "B2")
    b = cefr.scan("he was meticulous. meticulous work matters.", "B2")
    assert a["total_words"] == b["total_words"]
    assert len(a["offenders"]) == len(b["offenders"])


@pytest.mark.parametrize("text", [
    "They walked to London with Maria and her dog.",     # 专有名词在句中
    "Maria walked home. London was quiet that night.",   # 专有名词在句首
])
def test_专有名词不算超纲(text):
    assert offenders(text, names={"Maria", "London"}) == set()


def test_声明了人名就能无条件检测句首词():
    """把「猜」换成「用选题阶段本来就有的信息」。

    Ubiquitous 不在 CEFR-J 词表里，单看一个 token 和人名分不开；
    但 plan 声明了 names 之后就不用分了。

    断言写成「包含 / 不包含」而不是精确集合：没下载 CEFR-J 时会退回内置兜底表，
    那张表只有两千来个高频词，screens 之类会被多判成超纲。
    多判几个不影响这条要验的主张，写死集合反而让测试依赖运行环境
    ——CI 上就是没有词表的。
    """
    text = "Meticulous work matters. Nora walked home. Ubiquitous screens filled Riverton."
    got = offenders(text, names={"Nora", "Riverton"})
    assert {"Meticulous", "Ubiquitous"} <= got, "句首生词必须被检出"
    assert not ({"Nora", "Riverton"} & got), "声明过的人名不该被判成超纲词"


def test_没声明人名时靠句中大写兜底():
    """模型漏报 names 时还有一层不依赖它的实据：同一个词在别处以大写出现在句中。"""
    text = "Nora opened it. Nora was tired. Tedious work filled her day with Nora."
    assert offenders(text) == {"Tedious"}


def test_目标词不算超纲():
    assert offenders("Meticulous work matters here.", allow={"meticulous"}) == set()


def test_超纲率的分母不含专有名词():
    r = cefr.scan("Maria walked home with London.", "B2", names={"Maria", "London"})
    assert r["total_words"] == 3        # walked / home / with


# --------------------------------------------------------------- 词形还原

@pytest.mark.parametrize("target,text,expect", [
    ("bring", "She brought a book.", "brought"),
    ("run",   "He ran to the door.", "ran"),
    ("go",    "He went home.",       "went"),
    ("child", "The children played.", "children"),
    ("abandon", "The station was abandoned.", "abandoned"),
    ("studies", "He made a study of it.", "study"),   # 用户直接输入变形时
])
def test_不规则与规则变形都认得出(target, text, expect):
    from tasks.article.task import _appears
    assert (_appears(target, text) or "").lower() == expect


@pytest.mark.parametrize("target,text", [
    ("better", "The weather was good today."),   # better 的词典父词条是 good
    ("people", "That person left."),             # people -> person
    ("rose",   "The sun will rise."),            # rose -> rise
    ("hesitate", "She did not pause at all."),   # 压根没出现
])
def test_不把别的词判成目标词(target, text):
    """方向性判据：token 能还原到 target 才算，反过来把 target 往上还原不算。

    判错的代价不只是校验被骗过去——那个词还会被当成 surface 写进 Encounter，
    词条面板里就会显示一句根本没有这个词的例句。
    """
    from tasks.article.task import _appears
    assert _appears(target, text) is None


def test_同一个词的两种形态互认():
    assert same_word("run", "ran")
    assert same_word("abandon", "abandoned")


def test_异干替补不算同一个词():
    assert not same_word("better", "good")
    assert not same_word("people", "person")


def test_IRREGULAR_只收词表里查不到的形态():
    """收进不该收的会让 resolve 改掉用户要学的那个词的名字。

    判据是学习目标而不是语言学：was → be 该并（没人会说「我要学 was」），
    better → good 不该并。同形异义（left/rose/saw/found）分不开，
    保留归并是权衡后的选择，见 lemma.py 的注释。
    """
    for form in ("better", "best", "worse", "worst", "people", "lay",
                 "them", "their", "his", "its", "me", "my"):
        assert form not in IRREGULAR, f"{form} 不该在 IRREGULAR 里"


@pytest.mark.parametrize("form,base", [
    ("ran", "run"), ("went", "go"), ("children", "child"),
    ("brought", "bring"), ("feet", "foot"), ("taught", "teach"),
])
def test_真正的不规则形态必须保留(form, base):
    assert IRREGULAR.get(form) == base
    assert cefr.resolve(form) == base


@pytest.mark.parametrize("word", ["better", "people", "worse", "lay"])
def test_异干替补的词不再被改名(word):
    assert cefr.resolve(word) == word


def test_词表本身是就绪的():
    """没下载 CEFR-J 时会退回内置兜底表，功能不中断但判定粗得多。

    这条不断言必须是真词表——CI 上可能没下载——只保证两种情况都能用。
    """
    assert cefr.size() > 0
    assert cefr.level_of("abandon") in {"A1", "A2", "B1", "B2", "C1", "C2", None}


def test_forms_of_覆盖常见变形():
    got = forms_of("stop")
    assert {"stops", "stopped", "stopping"} <= got


def test_lemma_candidates_不为空():
    assert lemma_candidates("running")
    assert lemma_candidates("") == []
