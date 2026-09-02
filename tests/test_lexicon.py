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


def test_累计词汇量是单调不减的():
    """首页拿它给「用词上限」四档标词汇量。

    「B2」是天花板不是区间——B2 以下的词当然也能用，所以显示的必须是
    累计值。写成本级词数的话 C1 会显示得比 B2 还少（CEFR-J 里 C1 只有
    914 个词条），读起来就成了「C1 比 B2 简单」。
    """
    counts = cefr.level_counts()
    assert set(counts) == set(cefr.LEVELS)
    values = [counts[lv] for lv in cefr.LEVELS]
    assert values == sorted(values), "累计值不能往回掉"
    assert values[-1] == cefr.size(), "最高一级的累计值就是整张表"


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


# ------------------------------------------------- 目标词的派生形式不算超纲

#  一张小词表，复刻真词表的关键特征：派生形式自己就是词条，且等级更高。
#  其余都是最简单的功能词，好让断言里只剩下要验的那一个词。
DERIVED = {
    "the": "A1", "a": "A1", "was": "A1", "he": "A1", "she": "A1", "it": "A1",
    "house": "A1", "door": "A1", "spoke": "A1", "at": "A1", "very": "A1",
    "abandon": "B1", "abandoned": "B2",       # -ed 分词自己也是词条
    "reluctant": "B2", "reluctantly": "C1",   # -ly 副词自己也是词条
    "annoy": "A2", "annoyed": "B1",
}


@pytest.mark.parametrize("target,derived,text,level", [
    ("abandon",   "abandoned",   "The house was abandoned.",  "B1"),
    ("reluctant", "reluctantly", "He spoke reluctantly.",     "B2"),
    ("annoy",     "annoyed",     "She was annoyed.",          "A2"),
])
def test_目标词的派生形式不算超纲(cefr_table, target, derived, text, level):
    """abandon 写进文章时长的是 abandoned——管线自己的 prompt 样例就是这么写的。

    而 `abandoned` 在 CEFR-J 里是一个独立词条（B2），于是
    `resolve("abandoned")` 得到的是 `abandoned` 而不是 `abandon`，
    和 allow 里那个 `abandon` 永远碰不上头。

    代价不是「少判一个词」：check_paragraph 会据此判 too_hard，
    接着拿修复预算要求模型「把 abandoned 换成 B2 以内的说法」——
    **花钱让它删掉这篇文章要教的那个词**；stats 还会把目标词本身
    列进「文中仍有超纲词」。而用户看到的只是「生成完成」。
    """
    cefr_table(DERIVED)
    assert offenders(text, level) == {derived}, "前提：不给 allow 时它确实超纲"
    assert offenders(text, level, allow={target}) == set()


def test_目标词不会把别的词一起放行(cefr_table):
    """放行必须和 _appears 用同一个有方向的判据。

    拿「两边指向同一个词根」来放行的话，目标词 better 会把文中的 good
    一起放过去——难度标尺被开了个口子，而这个口子没有任何地方会报出来。
    """
    cefr_table({"the": "A1", "was": "A1", "weather": "A1",
                "better": "A1", "good": "C2"})
    assert offenders("The weather was good.", "B1", allow={"better"}) == {"good"}


def test_放行判据和目标词命中判据是同一个():
    """一边说「目标词出现了」、一边说「这个词超纲」，是两处用了不同判据。

    这条不比具体词，比的是两个判据本身对得上——以后谁改了任何一边，
    这里会先挂。
    """
    from tasks.article.task import _appears

    for target, text in [("abandon", "The house was abandoned."),
                         ("reluctant", "He spoke reluctantly."),
                         ("study", "She studies at night.")]:
        surface = _appears(target, text)
        assert surface, f"{target} 应当被判成出现了"
        assert same_word(target, surface), f"{target}/{surface}：两处判据对不上"


# ------------------------------------------------- 短词的规则变形也要还原得了

@pytest.mark.parametrize("base,form", [
    ("use", "used"), ("use", "using"), ("go", "going"), ("age", "aged"),
    ("owe", "owed"), ("tie", "tied"), ("ice", "iced"), ("ape", "aping"),
])
def test_四五个字母的规则变形也认得出(base, form):
    """`-ed` / `-ing` 的还原门槛原先是「去掉后缀还剩三个字母」，
    于是 used / aged / owed / tied（4 字母）和 using / going / dying（5 字母）
    一个都还原不了——而这是英语里最常见的一批词形。

    后果不是「少认一个变形」：目标词 go 写成 going 时 same_word 说不是同一个词，
    超纲检测于是把 going 判成超纲词，修复指令要求模型「把 going 换成
    B2 以内的说法」。整条链子一次都不报错。
    """
    assert same_word(base, form), f"{base} → {form} 应当算同一个词"


def test_目标词的短变形不算超纲(cefr_table):
    """接上一条，验的是它落在超纲检测上的后果。

    词表收的是原形（go），文中出现的是 going。还原不了的话，going 查不到等级，
    而「查不到等级一律视为超纲」——于是目标词自己的形态被判成超纲词，
    修复预算被拿去要求模型删掉它。
    """
    cefr_table({"he": "A1", "was": "A1", "home": "A1", "go": "C1"})
    text = "He was going home."
    assert offenders(text, "B2") == {"going"}, "前提：go 是 C1，不给 allow 时确实超纲"
    assert offenders(text, "B2", allow={"go"}) == set()


# ------------------------------------------------- 生成的不规则形态表

def test_生成表补上了规则法推不出的形态():
    """规则法还原不了真·不规则形态。认不出来的代价不只是多烧一次修复调用——
    `_normalize` 补不上这个 target，**这一处语境就不会写进 Encounter**，
    而累计语境是这个应用唯一不可再生的资产。
    """
    from tasks.article.task import _appears

    for base, form in [("arise", "arose"), ("arise", "arisen"),
                       ("awake", "awoke"), ("awake", "awoken"),
                       ("analysis", "analyses"), ("crisis", "crises"),
                       ("phenomenon", "phenomena"), ("calf", "calves"),
                       ("wolf", "wolves"), ("knife", "knives")]:
        assert same_word(base, form), f"{base} → {form} 应当算同一个词"
        assert _appears(base, f"The {form} were there.") == form


@pytest.mark.parametrize("word", [
    # 异干替补：用户说要学 better，词条面板标着 good 就是错的（第 5 条）
    "better", "best", "worse", "worst", "people", "lay", "more", "most", "elder",
    # 形态自己就是独立的词，并过去就把那个词藏了
    "bit", "bore", "born", "could", "might", "media", "pence",
    # 同形异义：bases 可能是 basis 也可能是 base；leaves 可能是 leaf 也可能是 leave
    "bases", "leaves", "dying",
])
def test_生成表没有收进不该收的(word):
    """`scripts/gen_irregular.py` 的筛选判据是「宁可漏收，不可错收」：
    漏收一条只是偶尔多烧一次修复调用，有界；错收一条会把 A 词判成 B 词的形态，
    错的词还会被当成 surface 写进 Encounter——写进库的东西不可再生。

    这条盯着那六道筛子。哪天有人放宽了判据重新生成，这里会先挂。
    """
    from core.lexicon.lemma import IRREGULAR

    assert word not in IRREGULAR


def test_手写表优先于生成表():
    """两张表装的不是一类东西：手写表编码的是「决策」，生成表装的是「事实」。
    决策不能被事实覆盖——LemmInflect 确实认为 better 的原形是 good，
    而本项目刻意不这么并。
    """
    from core.lexicon.irregular_forms import GENERATED
    from core.lexicon.lemma import _CURATED, IRREGULAR

    for form, base in _CURATED.items():
        assert IRREGULAR[form] == base, f"{form} 被生成表覆盖了"
    assert not (set(GENERATED) & set(_CURATED)), "两张表不该有交集，有就说明筛子漏了"


def test_目标词的不规则形态不算超纲(cefr_table):
    """和前面几条同源，验的是它落在超纲检测上的后果：
    词表收的是原形 arise，文中出现的是 arose，查不到等级一律视为超纲。
    """
    cefr_table({"the": "A1", "problem": "A1", "again": "A1", "arise": "C1"})
    text = "The problem arose again."
    assert offenders(text, "B2") == {"arose"}, "前提：arise 是 C1，不给 allow 时确实超纲"
    assert offenders(text, "B2", allow={"arise"}) == set()
