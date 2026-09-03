"""文章生成的提示词。

三步管线（借鉴 llm-storyteller 的 outline → narrative → polish，
但第三步换成更有价值的「校验 → 定点修复」）：

  1. plan   —— 先看词表找语义交集，协商出一个能自然容纳这批词的题材
  2. write  —— 按段生成，每段单独一次调用
  3. repair —— 只重写有问题的段落

为什么按段生成而不是一次写完：
  · DeepSeek 官方警告 max_tokens 不足会让 JSON 从中间截断，短输出更安全；
  · 出问题时只重写一段，不用整篇重来；
  · 前端能有真实进度，而不是转 60 秒圈。
"""
from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# 系统提示：贯穿三步的共同约束
# ---------------------------------------------------------------------------

SYSTEM = """\
你是一位为中文母语者设计英语分级读物的作者，兼职做中英对照翻译。

你的文章要同时满足两件事，缺一不可：
一是好读——有情节、有转折、读者愿意读完；
二是能记住词——目标词出现时，上下文必须足以让人猜出词义。

铁律：
1. 目标词首次出现时，同句或紧邻的句子里必须埋进「语境线索」：
   同义改写、正反对比、或一个具体到能看见画面的场景。
   反例（无线索，等于白读）：He was very meticulous about it.
   正例（有线索）：He lined up every pencil so its tip pointed the same way —
   he was meticulous, the kind of man who could not leave one thing crooked.
2. 除目标词外，用词不得超过指定的 CEFR 等级。宁可换个说法，不要炫词。
3. 中文是意译，要像中文，不要英式中文；但必须一句英文对一句中文，不合并不拆分。
4. 只输出 json，不要任何解释、不要 markdown 围栏。
"""

# ---------------------------------------------------------------------------
# 第一步：选题
# ---------------------------------------------------------------------------

PLAN_EXAMPLE = {
    "topic": "一位深夜电台主持人接到一通打错的电话",
    "genre": "短篇小说",
    "title_en": "The Wrong Number",
    "title_zh": "打错的电话",
    "reason": "这批词里 abandon / silence / confess 有共同的情绪场，适合用叙事承载",
    "paragraphs": [
        {"focus": "深夜电台，主持人独自值班", "words": ["abandon", "silence"]},
        {"focus": "电话响起，对方以为打给了别人", "words": ["confess", "hesitate"]},
    ],
    "unplaced": [],
    "names": ["Nora", "Riverton"],
}

PLAN_TEMPLATE = """\
下面是这次要学的目标词：
{words}

请先做选题，再写文章。要求：

1. 通读词表，找出它们的语义交集或情绪共性，据此挑一个能自然容纳大多数词的
   题材与文体（叙事 / 新闻报道 / 书信 / 影评 / 科普 / 日记，任选）。
   不要硬凑：题材是为了让词自然出现，不是让词变成填空题。
2. 规划 {n_paragraphs} 个段落，把目标词分配下去，每段 {per_para} 个左右。
   同一个词只分配给一段。分配要服务情节推进，不要平均撒。
3. 确实塞不进的词放进 unplaced，别硬塞——硬塞会毁掉整篇的可读性。
4. 把你打算用的人名、地名、机构名列进 names，并且**让每个名字至少在某句话的
   中间出现一次**，不要只出现在句首。程序靠「这个大写词在句中出现过」这一条
   来把专有名词和生词分开——只在句首露过面的名字，它分不出是名字还是生词，
   会当成超纲词退回来重写。

用 json 回答，格式与下例完全一致：
{example}
"""

# ---------------------------------------------------------------------------
# 第二步：写正文（按段）
# ---------------------------------------------------------------------------

WRITE_EXAMPLE = {
    "sentences": [
        {
            "en": "The station had been abandoned for years, its windows blind with dust.",
            "zh": "这座车站已经废弃多年，窗玻璃蒙着灰，什么也照不出来。",
            "targets": [{"lemma": "abandon", "surface": "abandoned"}],
        },
        {
            "en": "No one came, no one left, and the silence sat there like a passenger.",
            "zh": "没人来，也没人走，寂静像个乘客一样坐在那里。",
            "targets": [{"lemma": "silence", "surface": "silence"}],
        },
    ]
}

WRITE_TEMPLATE = """\
文章题材：{topic}
文体：{genre}
英文标题：{title_en}

{context}

现在写第 {index} 段（共 {total} 段）。
本段情节：{focus}
本段必须用到的目标词：{words}
本段长度：{n_sentences} 句左右，每句 12-22 个词。
除目标词外，用词不得超过 CEFR {level} 级。

每句都要给出对应的中文翻译，一句对一句。
targets 只填本段的目标词：lemma 写原形，surface 写它在这句英文里的实际形态
（比如原形 abandon、句中是 abandoned，就写 surface: "abandoned"）。
不要给字符位置，位置由程序自己算。

用 json 回答，格式与下例完全一致：
{example}
"""

CONTEXT_TEMPLATE = """\
前文已经写到（衔接用，不要重复）：
{recap}
"""

# ---------------------------------------------------------------------------
# 第三步：语境线索审计
#
# 这是整个管线里最重要的一关。前面的机械校验只能验「词出现了没有」，
# 验不了「读者能不能从上下文猜出词义」——而后者才是读文章记单词唯一起作用的机制。
# 词出现了但没有线索，等于白读，跟单词书没区别。所以专门用一次调用来审这件事。
# ---------------------------------------------------------------------------

AUDIT_EXAMPLE = {
    "audits": [
        {
            "lemma": "meticulous",
            "strength": "strong",
            "clue": "lined up every pencil so its tip pointed the same way",
            "why": "具体动作画面直接演出了「极度细致」，不认识这个词也能猜到",
        },
        {
            "lemma": "abandon",
            "strength": "none",
            "clue": "",
            "why": "只有一句 He decided to abandon it，前后没有任何能推断词义的信息",
        },
    ]
}

AUDIT_TEMPLATE = """\
下面是一段面向英语学习者的短文。这些词是读者**不认识**的生词：
{words}

请你扮演一个完全不认识这些词的读者，逐个判断：**只看这段文字，能不能推断出这个词的意思？**

判断标准，严格执行：
- strong：同句或紧邻句里有同义改写、正反对比、或具体到能看见画面的场景，
          不认识这个词也能八九不离十地猜对。
- weak：有一点方向性提示，但猜出来的意思可能偏差很大。
- none：完全没有线索，把这个词换成任何一个生词，句子照样通顺。

判断时要苛刻。「上下文读起来通顺」不等于「能推断出词义」——
一个词能被无痛替换成另一个词而不影响理解，那它就是 none。

段落原文：
{text}

用 json 回答，格式与下例完全一致。clue 字段要从原文里**原样摘抄**那段起线索作用的文字：
{example}
"""

CLUE_FIX_TEMPLATE = """\
以下目标词在这一段里缺少足够的语境线索，读者读完猜不出词义——
这等于白读，必须修：
{issues}

修法：为每个词补上线索。可以用同义改写、正反对比、或者加一个具体到能看见画面的
细节场景。**不要直接写出中文释义，也不要用括号解释**——要让读者自己能猜出来。
可以改写句子、也可以增加一句，但整段句数变化不要超过 1 句，情节和风格保持不变。

当前段落：
{current}

除目标词外用词仍不得超过 CEFR {level} 级；中英仍要一句对一句。
用 json 回答，格式与之前完全一致（顶层是 sentences 数组）：
{example}
"""

# ---------------------------------------------------------------------------
# 第四步：定点修复
# ---------------------------------------------------------------------------

REPAIR_TEMPLATE = """\
这是刚才生成的第 {index} 段：
{current}

校验发现以下问题：
{problems}

请重写这一段，修掉全部问题，同时保持情节、长度和风格不变。
除目标词外的用词仍不得超过 CEFR {level} 级；中英仍要一句对一句。

用 json 回答，格式与之前完全一致（顶层是 sentences 数组）：
{example}
"""


def _fmt(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def plan_prompt(words: list[str], n_paragraphs: int, per_para: int) -> list[dict]:
    listing = "\n".join(f"- {w}" for w in words)
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": PLAN_TEMPLATE.format(
                words=listing,
                n_paragraphs=n_paragraphs,
                per_para=per_para,
                example=_fmt(PLAN_EXAMPLE),
            ),
        },
    ]


def write_prompt(plan: dict, index: int, total: int, para: dict,
                 level: str, n_sentences: int, recap: str = "") -> list[dict]:
    context = CONTEXT_TEMPLATE.format(recap=recap) if recap else ""
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": WRITE_TEMPLATE.format(
                topic=plan.get("topic", ""),
                genre=plan.get("genre", ""),
                title_en=plan.get("title_en", ""),
                context=context,
                index=index,
                total=total,
                focus=para.get("focus", ""),
                words=", ".join(para.get("words", [])) or "（本段无指定目标词）",
                n_sentences=n_sentences,
                level=level,
                example=_fmt(WRITE_EXAMPLE),
            ),
        },
    ]


GLOSSARY_EXAMPLE = {
    "glossary": [
        {"lemma": "reluctant", "pos": "adj.", "zh": "不情愿的；勉强的",
         "note": "常搭配 be reluctant to do sth"},
        {"lemma": "threshold", "pos": "n.", "zh": "门槛；界限，临界点",
         "note": "既指实体门槛，也指抽象的临界值"},
    ]
}

GLOSSARY_TEMPLATE = """\
给下面这些英文单词写中文释义，供中国学习者背诵使用：
{words}

要求：
- zh 只给最核心的 1-2 个义项，不要罗列词典上的全部义项——背单词时义项越多越记不住。
- note 写一句真正有用的话：高频搭配、易混词、或构词法。没有就留空字符串。
- 如果这个词在文章语境里用的是某个特定义项，把那个义项放在最前面。

文章语境（供你判断该突出哪个义项）：
{context}

用 json 回答，格式与下例完全一致：
{example}
"""


def glossary_prompt(words: list[str], context: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": GLOSSARY_TEMPLATE.format(
                words="、".join(words),
                context=context[:1200] or "（暂无）",
                example=_fmt(GLOSSARY_EXAMPLE),
            ),
        },
    ]


def audit_prompt(text: str, words: list[str]) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": AUDIT_TEMPLATE.format(
                words="、".join(words),
                text=text,
                example=_fmt(AUDIT_EXAMPLE),
            ),
        },
    ]


def clue_fix_prompt(current: dict, issues: list[str], level: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": CLUE_FIX_TEMPLATE.format(
                issues="\n".join(issues),
                current=_fmt(current),
                level=level,
                example=_fmt(WRITE_EXAMPLE),
            ),
        },
    ]


def repair_prompt(index: int, current: dict, problems: list[str], level: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": REPAIR_TEMPLATE.format(
                index=index,
                current=_fmt(current),
                problems="\n".join(problems),
                level=level,
                example=_fmt(WRITE_EXAMPLE),
            ),
        },
    ]
