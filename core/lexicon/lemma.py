"""轻量词形还原（零依赖）。

不追求语言学上的完美——它只服务两件事：
  1. 超纲词检测时把 studies/studied/studying 归到 study，避免误报；
  2. 把文章中出现的词形挂到父词条上（借鉴 Lute 的 parent term 设计）。
真正的目标词词形由模型在生成时直接给出（surface 字段），不靠这里猜。
"""
from __future__ import annotations

import re

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

# 高频不规则形式，规则法搞不定的部分。
#
# 这张表决定「哪些词会被并到同一个词条下」，所以收什么不是语言学问题，
# 是学习目标问题：was → be 该并（没人会说「我要学 was」），
# better → good 不该并（用户说要学 better，词条面板却标着 good，
# 语境也和 good 的混在一起）。
#
# 按这条线剔掉了两类：异干比较级（better/best/worse/worst）、
# 以及 people→person、lay→lie 和一批代词物主形式。
#
# 剩下一处已知的并不干净：left/rose/saw/found 这类同形异义。
# left 既是 leave 的过去式，也是「左」；rose 既是 rise 的过去式，也是花。
# 不看词性分不开，而这里没有词性。保留归并是因为它们绝大多数时候
# 确实是过去式；真被并错了，用户看到的是标题不对，数据本身没坏。
IRREGULAR: dict[str, str] = {
    "am": "be", "is": "be", "are": "be", "was": "be", "were": "be", "been": "be", "being": "be",
    "has": "have", "had": "have", "having": "have",
    "does": "do", "did": "do", "done": "do", "doing": "do",
    "went": "go", "gone": "go", "goes": "go",
    "said": "say", "says": "say", "made": "make", "took": "take", "taken": "take",
    "came": "come", "saw": "see", "seen": "see", "knew": "know", "known": "know",
    "got": "get", "gotten": "get", "gave": "give", "given": "give",
    "found": "find", "thought": "think", "told": "tell", "became": "become",
    "left": "leave", "felt": "feel", "put": "put", "brought": "bring",
    "began": "begin", "begun": "begin", "kept": "keep", "held": "hold",
    "wrote": "write", "written": "write", "stood": "stand", "heard": "hear",
    "let": "let", "meant": "mean", "met": "meet", "ran": "run", "paid": "pay",
    "sat": "sit", "spoke": "speak", "spoken": "speak", "led": "lead",
    "grew": "grow", "grown": "grow", "lost": "lose", "fell": "fall", "fallen": "fall",
    "sent": "send", "built": "build", "understood": "understand", "drew": "draw",
    "broke": "break", "broken": "break", "spent": "spend", "cut": "cut", "rose": "rise",
    "driven": "drive", "drove": "drive", "bought": "buy", "wore": "wear", "chose": "choose",
    "chosen": "choose", "ate": "eat", "eaten": "eat", "sold": "sell", "won": "win",
    "taught": "teach", "caught": "catch", "threw": "throw", "flew": "fly",
    "children": "child", "men": "man", "women": "woman",
    "feet": "foot", "teeth": "tooth", "mice": "mouse", "geese": "goose", "lives": "life",
}

_VOWELS = set("aeiou")


def _suffix_candidates(w: str) -> list[str]:
    """规则法产出所有可能的原形。

    英语的 -ed/-ing 还原本质上有歧义（hoped→hope 但 hopped→hop，
    walked→walk 但 waked→wake），单条规则不可能都对。所以这里不做选择，
    而是把候选全给出来，由调用方拿词表去筛——只要有一个命中就算认识。
    """
    out: list[str] = []

    def push(x: str) -> None:
        if x and x not in out:
            out.append(x)

    # 复数 / 第三人称单数
    if len(w) > 4 and w.endswith("ies"):
        push(w[:-3] + "y")
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        push(w[:-2])
    if len(w) > 3 and w.endswith("es"):
        push(w[:-2]); push(w[:-1])
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        push(w[:-1])

    # 过去式 / 现在分词
    if len(w) > 4 and w.endswith("ied"):
        push(w[:-3] + "y")
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            stem = w[: -len(suf)]
            # 末尾叠辅音说明触发了「双写辅音」规则，去重后的形式才是原形
            if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
                push(stem[:-1])       # stopped -> stop, running -> run
            push(stem)                # walked -> walk, studying -> study
            push(stem + "e")          # making -> make, hoped -> hope

    # 副词 / 比较级 / 最高级
    if len(w) > 4 and w.endswith("ily"):
        push(w[:-3] + "y")            # happily -> happy
    if len(w) > 4 and w.endswith("ly"):
        push(w[:-2])                  # quickly -> quick
    for suf, back in (("est", 3), ("er", 2)):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            stem = w[:-back]
            if stem.endswith("i"):
                push(stem[:-1] + "y")  # happier -> happy
            if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
                push(stem[:-1])        # biggest -> big
            push(stem); push(stem + "e")

    return out


# 英语单词极少以这些叠辅音结尾，出现即说明是还原过头的残形（stopp / runn / bigg）
_IMPLAUSIBLE_ENDINGS = ("pp", "tt", "nn", "gg", "dd", "bb", "mm", "rr", "vv", "kk", "cc")


def _implausible(w: str) -> bool:
    if len(w) < 3:
        return True
    if w.endswith(_IMPLAUSIBLE_ENDINGS):
        return True
    if w.endswith("i"):           # happi / citi / universiti
        return True
    if not any(c in _VOWELS or c == "y" for c in w):
        return True
    return False


def lemma_candidates(word: str) -> list[str]:
    """返回原形候选，按可能性排序。

    -ed / -ing 的还原在英语里本质歧义（hoped→hope vs hopped→hop），
    所以不做单点判断：候选全给出，由词表做最终仲裁（见 cefr.resolve）。
    """
    w = word.lower().strip("'-")
    if not w:
        return []
    if w in IRREGULAR:
        return [IRREGULAR[w], w]
    cands = [w, *_suffix_candidates(w)]
    # 稳定排序：明显不像单词的残形排到最后，但保留在候选里
    return sorted(cands, key=_implausible)


def lemma(word: str, vocab: set[str] | None = None) -> str:
    """单一原形。传入词表时由词表仲裁，否则用形态合理性启发式。"""
    cands = lemma_candidates(word)
    if not cands:
        return ""
    w = word.lower().strip("'-")
    if vocab:
        for c in cands:
            if c in vocab:
                return c
    if w in IRREGULAR:
        return IRREGULAR[w]
    for c in cands[1:]:           # 优先给去掉屈折后缀的形式
        if not _implausible(c):
            return c
    return cands[0]


def same_word(target: str, token: str) -> bool:
    """token 是不是 target 这个词的另一种形态。

    方向是有意义的，不能拿「两边指向同一个词根」来判：

      · run / ran     —— 算。ran 就是 run 的不规则过去式。
      · better / good —— 不算。better 的词典父词条确实是 good，但用户要学的
        是 better；文中出现 good 却判成「目标词已出现」，等于校验被骗过去，
        接着 good 会被当成 better 的 surface 高亮出来，还写进 Encounter。

    所以只认两条路：token 能还原到 target（ran → run），
    或者 target 规则地屈折成 token（studies → study，用户直接输入变形时）。
    反过来把 target 往上还原一层再比，就会把整张 IRREGULAR 表变成误报源。
    """
    x = target.lower().strip("'-")
    y = token.lower().strip("'-")
    if not x or not y:
        return False
    return x == y or x in lemma_candidates(y) or y in _suffix_candidates(x)


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(text)


def tokenize_spans(text: str) -> list[tuple[str, int, int]]:
    """带位置的分词。超纲检测要靠位置分辨一个大写词是句首还是句中。"""
    return [(m.group(0), m.start(), m.end()) for m in WORD_RE.finditer(text)]


def lemmas_in(text: str) -> list[str]:
    return [lemma(t) for t in tokenize(text)]


def forms_of(word: str) -> set[str]:
    """给一个原形，粗略生成常见变形，用于在文中宽松定位。"""
    w = word.lower()
    out = {w}
    if w.endswith("y") and len(w) > 2 and w[-2] not in _VOWELS:
        out |= {w[:-1] + "ies", w[:-1] + "ied", w[:-1] + "ily", w[:-1] + "ier"}
    elif w.endswith(("s", "x", "z", "ch", "sh")):
        out.add(w + "es")
    else:
        out.add(w + "s")
    if w.endswith("e"):
        out |= {w + "d", w[:-1] + "ing", w + "r", w + "st"}
    else:
        out |= {w + "ed", w + "ing", w + "er", w + "est"}
        if len(w) > 2 and w[-1] not in _VOWELS and w[-2] in _VOWELS and w[-3] not in _VOWELS:
            out |= {w + w[-1] + "ed", w + w[-1] + "ing"}   # stop -> stopped/stopping
    out |= {w + "ly", w + "ment", w + "ness", w + "tion"}
    return out
