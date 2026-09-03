"""文章生成管线。

这个文件里的测试大多在问同一个问题：**模型返回了不符合预期的东西时会怎样。**

之所以值得这么多篇幅：json_schema 只有 Kimi 会真的执行，DeepSeek 收下也不生效，
所以「合法但形状不对的 json」是常态而不是边角情况。而这类错误如果一路带到下游，
抛出来的是 AttributeError，那时整篇文章已经花掉几分钟和上万 token——连同
已经写好的段落一起作废。
"""
from __future__ import annotations

import itertools

import pytest

from conftest import GOOD_AUDIT, GOOD_PARAGRAPH, GOOD_PLAN, run_pipeline
from tasks.article.schema import (
    coerce_audits, coerce_glossary, coerce_paragraph, coerce_plan,
)
from tasks.article.task import (
    MAX_PARAGRAPHS,
    MAX_WORDS,
    ArticleTask,
    estimated_words,
    sizing,
)


# ------------------------------------------------------- 形状归一（纯函数）

@pytest.mark.parametrize("junk", [
    None, 5, "abc", [], {}, [1, 2], {"x": None}, [[]],
    {"sentences": 5}, {"audits": "x"}, {"paragraphs": [None, 5, "x"]},
    [{"en": None, "targets": 7}], {"glossary": [None]},
])
@pytest.mark.parametrize("fn", [coerce_plan, coerce_paragraph, coerce_audits, coerce_glossary])
def test_归一函数对任意输入都不抛(fn, junk):
    fn(junk)


def test_顶层数组被包回它该在的键下():
    assert coerce_paragraph([{"en": "a", "zh": "b"}])["sentences"][0]["en"] == "a"
    assert coerce_audits([{"lemma": "x", "strength": "strong"}])[0]["lemma"] == "x"


def test_非字符串字段置空():
    """模型偶尔把 title_en 写成对象，直接入库会污染 String 列。"""
    assert coerce_plan({"title_en": {"x": 1}})["title_en"] == ""


def test_未知的线索强度按最坏情况处理():
    """审计判不出结论的词绝不能默认放行——那正好绕开管线里最该拦住东西的一关。"""
    assert coerce_audits({"audits": [{"lemma": "a", "strength": "medium"}]})[0]["strength"] == "none"
    assert coerce_audits({"audits": [{"lemma": "a", "strength": 5}]})[0]["strength"] == "none"
    assert coerce_audits({"audits": [{"lemma": "a", "strength": "STRONG"}]})[0]["strength"] == "strong"


def test_段落只保留三个键():
    """多余的键会被 repair_prompt 原样 dump 回去喂给模型，等于教它照抄垃圾。"""
    out = coerce_paragraph({"sentences": [{"en": "a", "zh": "b", "junk": 1}], "extra": 2})
    assert set(out) == {"sentences"}
    assert set(out["sentences"][0]) == {"en", "zh", "targets"}


# ------------------------------------------------- 形状归一（跑完整条管线）

MALFORMED = {
    "plan 是顶层数组":        ("plan", [{"focus": "f", "words": ["abandon"]}]),
    "plan 是纯字符串":        ("plan", "抱歉我不能完成"),
    "paragraphs 是字符串数组": ("plan", {"title_en": "T", "paragraphs": ["第一段", "第二段"]}),
    "paragraphs 是 dict":     ("plan", {"title_en": "T", "paragraphs": {"1": {"words": ["a"]}}}),
    "words 是 null":          ("plan", {"title_en": "T", "paragraphs": [{"words": None}]}),
    "title_en 是对象":        ("plan", {"title_en": {"x": 1},
                                        "paragraphs": [{"words": ["abandon", "silence"]}]}),
    "write 是顶层数组":       ("write", [{"en": "x", "zh": "y"}]),
    "write 没有 sentences":   ("write", {"paragraph": "..."}),
    "sentences 是字符串数组": ("write", {"sentences": ["hello", "second"]}),
    "targets 是字符串数组":   ("write", {"sentences": [
        {"en": "The shop was abandoned in silence.", "zh": "a",
         "targets": ["abandon", "silence"]}]}),
    "audit 是顶层数组":       ("audit", [{"lemma": "abandon", "strength": "strong"}]),
    "audit 元素是字符串":     ("audit", {"audits": ["abandon: strong"]}),
    "audit strength 是数字":  ("audit", {"audits": [{"lemma": "abandon", "strength": 5}]}),
    "glossary 是顶层数组":    ("glossary", [{"lemma": "abandon", "zh": "抛弃"}]),
    "glossary 元素是字符串":  ("glossary", {"glossary": ["abandon = 抛弃"]}),
}


@pytest.mark.parametrize("case", list(MALFORMED), ids=list(MALFORMED))
def test_畸形输出不中断管线(case, fake_llm, happy_responses):
    """掰不回来的部分置空，交给已有的「校验 → 修复」循环，不抛异常。"""
    step, payload = MALFORMED[case]
    happy_responses[step] = payload
    doc, stats, _ = run_pipeline(fake_llm(happy_responses))

    assert isinstance(doc["title_en"], str)
    cs = stats["clue_strength"]
    #  归一之后 strength 只会是三个值之一，分母不会再冒出第四个键
    assert sum(cs.values()) == cs["strong"] + cs["weak"] + cs["none"]


def test_全线返回_null_时报可读错误(fake_llm, happy_responses):
    """归一的副作用是把「崩溃」变成「静默存一篇 0 词的文章」，那比报错更糟。"""
    llm = fake_llm({k: None for k in happy_responses})
    with pytest.raises(ValueError, match="没有产出任何可用的正文"):
        run_pipeline(llm)


# --------------------------------------------------------- 选题的词分配

assign = ArticleTask._assign_words


@pytest.mark.parametrize("name,planned,words,unplaced,expect", [
    ("模型自己加词",
     [{"words": ["abandon", "quixotic", "silence"]}], ["abandon", "silence"], [],
     ([["abandon", "silence"]], [])),
    ("同一个词分给两段",
     [{"words": ["abandon", "silence"]}, {"words": ["silence"]}], ["abandon", "silence"], [],
     ([["abandon", "silence"], []], [])),
    ("模型漏词，补回最后一段",
     [{"words": ["abandon"]}, {"words": []}], ["abandon", "silence"], [],
     ([["abandon"], ["silence"]], [])),
    ("模型改了大小写，以用户拼写为准",
     [{"words": ["Abandon", "SILENCE"]}], ["abandon", "silence"], [],
     ([["abandon", "silence"]], [])),
    ("模型说塞不进就不硬塞",
     [{"words": ["abandon", "silence"]}], ["abandon", "silence", "fragile"], ["fragile"],
     ([["abandon", "silence"]], ["fragile"])),
    ("说塞不进但其实是漏了，仍然补回去",
     [{"words": ["abandon", "silence"]}], ["abandon", "silence", "fragile"], [],
     ([["abandon", "silence", "fragile"]], [])),
    ("模型说全都塞不进 —— 它没理解任务，忽略",
     [{"words": []}], ["abandon", "silence"], ["abandon", "silence"],
     ([["abandon", "silence"]], [])),
    ("用户自己输入了重复词",
     [{"words": ["abandon"]}], ["abandon", "Abandon"], [],
     ([["abandon"]], [])),
    ("顺序保持模型的分配顺序",
     [{"words": ["silence", "abandon"]}], ["abandon", "silence"], [],
     ([["silence", "abandon"]], [])),
])
def test_词分配(name, planned, words, unplaced, expect):
    got_plan, got_dropped = assign(planned, words, unplaced)
    assert ([p["words"] for p in got_plan], got_dropped) == expect, name


def test_保留段落的其它键():
    assert assign([{"focus": "深夜电台", "words": ["a"]}], ["a"], [])[0][0]["focus"] == "深夜电台"


def test_幻觉词不烧修复预算(fake_llm, happy_responses):
    """模型往 plan 里加词，会一边要求它出现、一边把它判成超纲词——
    两条指令互相打架，谁也满足不了，MAX_REPAIRS 全花在一个用户没要求学的词上。"""
    happy_responses["plan"] = {
        **GOOD_PLAN,
        "paragraphs": [{"focus": "f", "words": ["abandon", "silence", "quixotic"]}],
    }
    llm = fake_llm(happy_responses)
    _, stats, _ = run_pipeline(llm)

    assert stats["repairs"] == 0
    assert llm.calls.count("repair") == 0
    assert sum(stats["clue_strength"].values()) == 2       # 不是 3
    assert [a["lemma"] for a in stats["audits"]] == ["abandon", "silence"]


def test_塞不进的词如实报出来(fake_llm, happy_responses):
    """交一篇稀释过的文章，用户看到的是「生成完成」，没有任何线索说明哪里不对；
    如实报出来，用户第一眼就知道该换个题材再生成一篇。"""
    happy_responses["plan"] = {**GOOD_PLAN, "unplaced": ["fragile"]}
    _, stats, events = run_pipeline(
        fake_llm(happy_responses), words=["abandon", "silence", "fragile"])

    assert stats["unplaced"] == ["fragile"]
    assert "fragile" in stats["targets_missed"]
    assert any("容不下" in e.get("message", "") for e in events if e["type"] == "phase")


# ----------------------------------------------------------------- 线索审计

def test_审计漏审的词按最坏情况处理(fake_llm, happy_responses):
    happy_responses["audit"] = {"audits": [{"lemma": "abandon", "strength": "strong"}]}
    _, stats, _ = run_pipeline(fake_llm(happy_responses))
    assert stats["clue_strength"]["none"] == 1        # silence 没被审到


def test_线索不足会触发补写(fake_llm, happy_responses):
    happy_responses["audit"] = {"audits": [
        {"lemma": "abandon", "strength": "weak"},
        {"lemma": "silence", "strength": "weak"}]}
    llm = fake_llm(happy_responses)
    run_pipeline(llm)
    assert llm.calls.count("clue_fix") > 0


def test_补线索把机械校验搞坏就丢弃(fake_llm, happy_responses):
    """补线索不能把难度或对齐搞坏，坏了这次改写就不算数。"""
    happy_responses["audit"] = {"audits": [{"lemma": "abandon", "strength": "weak"}]}
    happy_responses["clue_fix"] = {"sentences": [{"en": "", "zh": ""}]}   # 明显不合格
    doc, stats, _ = run_pipeline(fake_llm(happy_responses))

    assert doc["paragraphs"][0]["sentences"][0]["en"] == GOOD_PARAGRAPH["sentences"][0]["en"]
    assert stats["clue_fixes"] == 0        # 被丢弃的改写不计数


def test_模型回了屈折形也认得出是同一个词(fake_llm, happy_responses):
    """问它 abandon，它回 abandoned——不能因此当成「漏审」。

    按字符串相等去认的话，这个词被兜底成 none，接着发生的事一件比一件糟：
    一段本来线索充分的段落挨两轮补线索改写（4 次多余调用、两分多钟，
    而改写只可能让它变差），结果面板还把它报成「语境线索充分 0/1」——正好反了。
    而线索强度是这个项目唯一用来自我监测的仪表，仪表读反了，
    「密度伤到线索会响」这条前提就不成立了。
    """
    happy_responses["audit"] = {"audits": [
        {"lemma": "abandoned", "strength": "strong"},      # 过去分词
        {"lemma": "Silences", "strength": "strong"},       # 大写 + 复数
    ]}
    llm = fake_llm(happy_responses)
    _, stats, _ = run_pipeline(llm)

    assert llm.calls.count("clue_fix") == 0, "本来就合格，不该去补线索"
    assert stats["clue_strength"] == {"strong": 2, "weak": 0, "none": 0}
    assert [a["lemma"] for a in stats["audits"]] == ["abandon", "silence"],         "lemma 要归回用户给的那个词，否则 save_article 挂不上线索"


def test_一条结论只能算到一个词头上(fake_llm, happy_responses):
    """认领必须是一对一的，否则 clue_strength 的分母会虚高。"""
    happy_responses["audit"] = {"audits": [{"lemma": "abandoned", "strength": "strong"}]}
    _, stats, _ = run_pipeline(fake_llm(happy_responses))
    assert stats["clue_strength"] == {"strong": 1, "weak": 0, "none": 1}


def test_完全对不上的结论仍按最坏情况处理(fake_llm, happy_responses):
    """放宽认领判据不能顺手把「审计确实漏了」也放过去。"""
    happy_responses["audit"] = {"audits": [{"lemma": "unrelated", "strength": "strong"}]}
    _, stats, _ = run_pipeline(fake_llm(happy_responses))
    assert stats["clue_strength"]["none"] == 2


# ------------------------------------------------------- targets 的收敛

#  这一组守的是 data/app.db。文章能重新生成，词条和累计语境不能——
#  一个不该进去的词写进去了，事后没有任何办法把它和真正学过的词分开。

def _marked(doc) -> list[tuple[str, str]]:
    return [(t["lemma"], t["surface"])
            for p in doc["paragraphs"] for s in p["sentences"] for t in s["targets"]]


def test_模型自己加的词不许写进词库(fake_llm, happy_responses):
    """让它写 abandon / silence，它顺手把 shop、spring 也标成了 target。

    这两个词会拿到自己的 Word 行和 Encounter，进词库、算进「累计词条」和
    「在多个语境中见过」——**这个应用用来说明自己有用的那两个数字**。
    而它完全不报错：文章是好的，用户看到的是「生成完成」。
    """
    happy_responses["write"] = {"sentences": [{
        **GOOD_PARAGRAPH["sentences"][0],
        "targets": [
            {"lemma": "abandon", "surface": "abandoned"},
            {"lemma": "silence", "surface": "silence"},
            {"lemma": "shop", "surface": "shop"},        # 模型自己加的
            {"lemma": "spring", "surface": "spring"},    # 模型自己加的
        ],
    }]}
    doc, _, _ = run_pipeline(fake_llm(happy_responses))
    assert sorted({lemma for lemma, _ in _marked(doc)}) == ["abandon", "silence"]


def test_模型把_lemma_回成派生形式也要归回原词(fake_llm, happy_responses):
    """问它 abandon，它标 abandoned。

    abandoned 自己就是 B2 词条，cefr.resolve 归不回 abandon，于是库里同时
    立着两条，同一个词的语境从此分摊在两个词条下，越攒越散
    （需要注意.md 第 6b 条那个裂缝）。
    """
    happy_responses["write"] = {"sentences": [{
        **GOOD_PARAGRAPH["sentences"][0],
        "targets": [{"lemma": "abandoned", "surface": "abandoned"},
                    {"lemma": "Silences", "surface": "silence"}],
    }]}
    doc, _, _ = run_pipeline(fake_llm(happy_responses))
    assert sorted({lemma for lemma, _ in _marked(doc)}) == ["abandon", "silence"]


def test_别的段落分到的词出现在本段也算数():
    """收敛的判据是「全篇的目标词」，不是「本段分到的词」。

    一个分给第 3 段的词真的出现在第 1 段里，那是一处货真价实的语境——
    按本段的词去滤会把它丢掉，而累计语境正是这个应用最不该丢的东西。
    """
    para = {"sentences": [{
        "en": "The shop was abandoned, and the silence stayed on.", "zh": "废弃了。",
        "targets": [{"lemma": "abandon", "surface": "abandoned"},
                    {"lemma": "silence", "surface": "silence"}]}]}
    out = ArticleTask._normalize(para, expected=["abandon"], wanted=["abandon", "silence"])
    assert [t["lemma"] for t in out["sentences"][0]["targets"]] == ["abandon", "silence"]


def test_同一句里同一个词的两种形态都要留着():
    """去重按 (词, 形态)，不能只按词——两处都该高亮。"""
    para = {"sentences": [{
        "en": "He abandoned the shop, and the abandoning was quick.", "zh": "x",
        "targets": [{"lemma": "abandon", "surface": "abandoned"},
                    {"lemma": "abandon", "surface": "abandoning"}]}]}
    out = ArticleTask._normalize(para, expected=[], wanted=["abandon"])
    assert [t["surface"] for t in out["sentences"][0]["targets"]] == ["abandoned", "abandoning"]


def test_认领之后撞车的两条要去重():
    """abandon 和 abandoned 会归到同一个词上，不去重就会标两遍。"""
    para = {"sentences": [{
        "en": "The shop was abandoned.", "zh": "x",
        "targets": [{"lemma": "abandon", "surface": "abandoned"},
                    {"lemma": "Abandoned", "surface": "abandoned"}]}]}
    out = ArticleTask._normalize(para, expected=["abandon"], wanted=["abandon"])
    assert len(out["sentences"][0]["targets"]) == 1


def test_收敛不影响模型漏标时的兜底(fake_llm, happy_responses):
    """模型一个 target 都不标时，_appears 补出来的那条不能被一起滤掉。"""
    happy_responses["write"] = {"sentences": [
        {**GOOD_PARAGRAPH["sentences"][0], "targets": []}]}
    doc, stats, _ = run_pipeline(fake_llm(happy_responses))
    assert sorted({lemma for lemma, _ in _marked(doc)}) == ["abandon", "silence"]
    assert stats["targets_hit"] == 2


# ------------------------------------------------------------- 标成忽略的词

def test_标成忽略的词不再算超纲(fake_llm, happy_responses):
    """人判一次，永久生效——这是 Lute 那套 status 的用法。

    专有名词没有便宜又可靠的自动判据（人名和普通词大面积同形：Rose、Will、
    Grace、Hope…），所以这个领域的两种成熟做法都是「做成数据」：
    Paul Nation 的词表附一张专有名词表，Lute 让用户标一次然后永久生效。
    这个项目本来就有 status 99「忽略（专有名词等）」和阅读页的 I 键，
    只是难度标尺从来没读过它。
    """
    happy_responses["write"] = {"sentences": [{
        "en": "Nora abandoned the shop, and the silence stayed on for months.",
        "zh": "Nora 废弃了小店，寂静留了好几个月。",
        "targets": GOOD_PARAGRAPH["sentences"][0]["targets"],
    }]}

    _, before, _ = run_pipeline(fake_llm(happy_responses))
    assert "Nora" in {o["surface"] for o in before["offenders"]}, "前提：不标就会被判超纲"

    task = ArticleTask()
    stats = None
    for ev in task.run(fake_llm(happy_responses),
                       {"words": ["abandon", "silence"], "level": "B2",
                        "ignored": {"Nora"}}):
        if ev["type"] == "done":
            stats = ev["stats"]
    assert stats["offenders"] == []
    assert stats["offender_rate"] == 0.0


def test_忽略集合脏了也不出错(fake_llm, happy_responses):
    """params 是外面传进来的，别假设它一定是一堆干净的字符串。"""
    task = ArticleTask()
    for junk in (None, [], ["", "  "], [None, 5, {"a": 1}, "Nora"], "Nora"):
        events = list(task.run(fake_llm(happy_responses),
                               {"words": ["abandon", "silence"], "level": "B2",
                                "ignored": junk}))
        assert events[-1]["type"] == "done", junk


# ----------------------------------------------------------------- 段数上限

def test_模型回多少段就写多少段是不行的(fake_llm, happy_responses):
    """MAX_PARAGRAPHS 一直只是 sizing() 的入参，从没拦过模型真的回了几段。

    管线里其它每一处模型输出都做了归一和收敛，唯独段数是照单全收的：
    实测模型回 40 段时，顺风路径的 4 次调用变成 44 次、二十多分钟。
    而且它不报错——前端进度条会自己把分母加大，看着只是「这篇比较久」。
    """
    happy_responses["plan"] = {**GOOD_PLAN, "paragraphs": [
        {"focus": f"f{i}", "words": ["abandon"] if i == 30 else (["silence"] if i == 31 else [])}
        for i in range(40)
    ]}
    llm = fake_llm(happy_responses)
    doc, stats, events = run_pipeline(llm)

    assert len(doc["paragraphs"]) == MAX_PARAGRAPHS
    assert llm.calls.count("write") == MAX_PARAGRAPHS
    # 被砍掉的段落里的目标词不能跟着丢
    assert stats["targets_hit"] == stats["targets_total"] == 2
    assert stats["targets_missed"] == []
    # 静默截断读起来和「模型本来就只规划了这么多段」一模一样，必须说一声
    assert any("裁掉" in e.get("message", "") for e in events if e.get("type") == "phase")


def test_没超上限时不多嘴(fake_llm, happy_responses):
    llm = fake_llm(happy_responses)
    _, _, events = run_pipeline(llm)
    assert not any("裁掉" in e.get("message", "") for e in events if e.get("type") == "phase")


# --------------------------------------------------------------------- 篇幅

def _est(n: int) -> int:
    n_para, _per, n_sent = sizing(n)
    return estimated_words(n_para, n_sent)


def test_篇幅跟着词数走():
    """以前每段句数写死 5 句、段数又有两段下限，于是 **1 到 6 个词全都得到
    「2 段约 170 词」**——给一个词和给六个词读一样长的东西，多出来的全是稀释；
    18 到 40 个词那头同样平，全是 510 词。

    这条断言的是「输入变了输出就得变」，不写死具体数字：数字是可以调的
    （SENTENCE_SCAFFOLD），而「不能有一大段平台」是不能退的。
    """
    from tasks.article.task import MAX_PARAGRAPHS, WORDS_PER_PARAGRAPH

    est = [_est(n) for n in range(1, MAX_WORDS + 1)]
    assert est == sorted(est), "词数增加，篇幅不能反而变短"
    assert est[-1] > est[0] * 4, "整个区间的篇幅跨度不能这么小"

    # 平台只在「舒适容量」以内卡死。超出之后段数封顶，每段句数是整数，
    # 只能每 MAX_PARAGRAPHS 个词跳一档——而那个区间 plan_preview 本来就在
    # 提示「建议分批」，不是该优化的地方。把范围写出来，不是把阈值放宽。
    comfortable = MAX_PARAGRAPHS * WORDS_PER_PARAGRAPH
    longest, run = 1, 1
    for a, b in itertools.pairwise(est[:comfortable]):
        run = run + 1 if a == b else 1
        longest = max(longest, run)
    assert longest <= 3, f"{comfortable} 词以内有 {longest} 个连续词数篇幅相同，太平了"


@pytest.mark.parametrize("n", range(2, 41))
def test_每个目标词分到的篇幅是稳定的(n):
    """篇幅应当和词数成比例，而不是阶梯式地跳。

    n=1 不在这条里：一句话的「文章」没有情节可言，也没有相邻句可以铺线索，
    所以有个下限，比例在那一点上必然偏高。
    """
    ratio = _est(n) / n
    assert 15 <= ratio <= 24, f"{n} 个词 -> {_est(n)} 词，每词 {ratio:.0f} 倍"


def test_段数有上限而句数没有():
    """段数封顶之后，词再多只能靠加句数来消化——不封的话 40 个词会排出
    十几段，而那已经不是一篇文章了。"""
    from tasks.article.task import MAX_PARAGRAPHS

    assert sizing(MAX_WORDS)[0] == MAX_PARAGRAPHS
    assert sizing(MAX_WORDS)[2] > sizing(6)[2]


def test_一个词也给得出一篇():
    """下限是两句：一句承载目标词，一句给它铺线索。"""
    n_para, per_para, n_sent = sizing(1)
    assert (n_para, per_para, n_sent) == (1, 1, 2)

# ------------------------------------------------- 修复预算不能花在目标词身上

def test_不会拿修复预算去删掉目标词本身(cefr_table):
    """目标词写进文章时长的是屈折形态（abandon → abandoned），
    管线自己的 prompt 样例就是这么写的。而 `abandoned` 在 CEFR-J 里是一个
    独立词条、等级还更高，于是超纲检测认不出它就是目标词。

    后果是一条完整的因果链，每一环都不报错：
      判 too_hard → 烧一次修复调用 → 修复指令写着「把这些词换成 CEFR B1
      以内的说法：abandoned、reluctantly」→ 模型照做，把目标词换掉 →
      下一轮校验又报「目标词 abandon 没有在本段出现」→ 两条指令互相打架，
      MAX_REPAIRS 全烧光 → 交出一篇目标词被改没了的文章，
      而用户看到的是「生成完成」。

    OFFENDER_FLOOR 是 1，所以要两个词才顶得破——真实段落里有三个目标词，
    这条链子在实际运行时比这个最小复现更容易触发。
    """
    cefr_table({
        "the": "A1", "mill": "A1", "was": "A1", "after": "A1", "flood": "A1",
        "he": "A1", "spoke": "A1", "about": "A1", "it": "A1",
        "abandon": "B1", "abandoned": "B2",        # -ed 分词自己也是词条
        "reluctant": "B2", "reluctantly": "C1",    # -ly 副词自己也是词条
    })
    para = {"sentences": [
        {"en": "The mill was abandoned after the flood.",
         "zh": "洪水之后工厂就废弃了。",
         "targets": [{"lemma": "abandon", "surface": "abandoned"}]},
        {"en": "He spoke reluctantly about it.",
         "zh": "他很不情愿地提起这件事。",
         "targets": [{"lemma": "reluctant", "surface": "reluctantly"}]},
    ]}

    problems = ArticleTask().check_paragraph(
        para, ["abandon", "reluctant"], "B1",
        allow={"abandon", "reluctant"}, names=set())

    assert problems == [], [p.as_instruction() for p in problems]
