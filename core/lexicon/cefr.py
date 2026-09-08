"""词汇难度标尺：CEFR 分级查询 + 超纲词检测。

数据源：CEFR-J Vocabulary Profile（openlanguageprofiles/olp-en-cefrj）
先跑 scripts/fetch_cefr.py 下载到 data/cefr.csv。
没有该文件时自动降级为内置的高频词兜底表，功能不中断，只是判定更粗。
"""
from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

from .lemma import lemma_candidates, same_word, tokenize_spans

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CEFR_CSV = DATA_DIR / "cefr.csv"

# 判断一个大写词是句首还是句中，只需要看它和前一个词之间隔着什么
_SENT_BREAK = re.compile(r"[.!?…\n。！？]")

LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
LEVEL_INDEX = {lv: i for i, lv in enumerate(LEVELS)}

# 没有 CEFR-J 数据时的兜底表：最高频的功能词与核心词。
# 它只保证「超纲检测不会把 the / of / because 之类误判成生词」，精度远不如真词表。
_FALLBACK = """
a about above across after again against all almost alone along already also although always am among an and
another answer any anyone anything appear apple are area arm around arrive art as ask at away baby back bad bag
ball bank be beautiful because become bed been before begin behind believe below beside best better between big
bird birthday bit black blue boat body book born both box boy bread break bring brother brown build burn bus
business busy but buy by call can car care carry case cat catch cause centre certain chair chance change cheap
check child choose church city class clean clear climb clock close clothes cloud coffee cold college colour come
common company complete computer condition consider continue control cook cool corner cost could country course
cover create cross cry cup cut dance danger dark date daughter day dead deal dear death decide deep degree
describe design desk develop die difference different difficult dinner direct discover discuss do doctor dog
door doubt down draw dream dress drink drive drop dry during each ear early earth east easy eat education effect
egg eight either else empty end enough enter equal especially even evening ever every example except exercise
expect experience explain eye face fact fail fall family famous far farm fast father fear feel few field fight
fill film final find fine finger finish fire first fish fit five fix floor flower fly follow food foot for force
forget form four free fresh friend from front full fun future game garden general get girl give glass go gold
good govern great green ground group grow guess hair half hand happen happy hard hat hate have he head health
hear heart heat heavy help her here high hill him his history hit hold holiday home hope horse hospital hot hotel
hour house how however human hundred hungry hurry hurt husband i ice idea if important in include increase indeed
industry information inside instead interest into introduce it its job join journey joy just keep key kill kind
king kitchen knife know lady lake land language large last late laugh law lay lead learn leave left leg lesson
let letter level library lie life light like line list listen little live local long look lose lot love low luck
lunch machine main make man many map mark market marry match matter may me mean meat meet member memory middle
might mile milk mind minute miss mistake modern moment money month moon more morning most mother mountain mouth
move much music must my name nation nature near necessary need never new news next nice night nine no none nor
north nose not note nothing notice now number obtain of off offer office often oil old on once one only open
opinion or order other our out outside over own page pain paint paper parent park part party pass past pay peace
pen people perhaps period person picture piece place plan plant play please point police poor position possible
power practise prepare present press pretty price problem produce program provide public pull push put question
quick quiet quite radio rain raise reach read ready real reason receive record red remember remove rent repeat
reply report rest result return rich ride right ring rise river road rock room round rule run safe salt same
save say school science sea season seat second see seem sell send sense sentence separate serious serve service
set seven several shall shape share sharp she ship shoe shop short should shoulder show shut sick side sight sign
silver simple since sing single sister sit situation six size skin sky sleep slow small smell smile smoke snow so
social soft some son song soon sorry sound south space speak special speed spend sport spring stand star start
state station stay step still stone stop store story straight strange street strong student study subject such
sudden suffer sugar summer sun supply support suppose sure surprise sweet swim system table take talk tall taste
teach team tell ten test than thank that the their them then there these they thick thin thing think third this
those though thought three through throw thus ticket time tired to today together tomorrow tonight too top total
touch toward town trade train travel tree trip trouble true try turn twelve twenty two type under understand
until up use usual value various very village visit voice wait walk wall want war warm wash watch water way we
wear weather week weight welcome well west wet what wheel when where whether which while white who whole why wide
wife wild will win wind window wine winter wish with within without woman wonder wood word work world worry worth
would write wrong year yes yesterday yet you young your
""".split()

_HEADWORD_KEYS = ("headword", "word", "lemma")


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], bool]:
    """返回 (原形 -> CEFR 等级, 是否用的是真实 CEFR 数据)。"""
    if CEFR_CSV.exists():
        table: dict[str, str] = {}
        with CEFR_CSV.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                key = next((k for k in row if k and k.strip().lower() in _HEADWORD_KEYS), None)
                lvk = next((k for k in row if k and "cefr" in k.strip().lower()), None)
                if lvk is None and "level" in row:
                    lvk = "level"
                if not key or not lvk:
                    continue
                word = (row[key] or "").strip().lower()
                level = (row[lvk] or "").strip().upper()[:2]
                if not word or level not in LEVEL_INDEX:
                    continue
                # 同一个词有多条词性记录时取最容易的那一级
                if word not in table or LEVEL_INDEX[level] < LEVEL_INDEX[table[word]]:
                    table[word] = level
        if table:
            return table, True
    return {w: "A1" for w in _FALLBACK}, False


def is_real_data() -> bool:
    return _load()[1]


def size() -> int:
    return len(_load()[0])


def vocabulary() -> set[str]:
    return set(_load()[0])


# ---------------------------------------------------------------------------
# 第二判据：CEFR-J 查不到时，回落到词频
#
# 为什么需要它。CEFR-J 只有 8653 条，而这个仓库里**同时还躺着一份 33217 条的
# ECDICT 表**（data/wordbook/dict.csv，为背单词页提交的），两份数据从来没说过话。
# 于是 25131 个词只因为「不在那 8653 条里」就被判超纲——其中 2903 个词频排进
# 前一万，875 个还带着中学 / 四级考纲标签。`toward`、`color`、`program`、
# `realize`、`center`、`behavior`、`recognize` 全在这批里，而 db.add_word 的
# 注释早就点过名：「多数是标尺自己不认识的词」。
#
# 代价不是「多报一个词」：check_paragraph 会据此判 too_hard，拿修复预算去要求
# 模型「把 toward 换成 B2 以内的说法」——花钱把一段本来合格的文章改坏，
# 而结果面板上那个「超纲词占比」还跟着虚高。
#
# 为什么是词频，不是考纲标签。标签（zk / gk / cet4）看着更贴这个用户群，
# 但它**验不了**：CEFR-J 里没有这些词，就没有真值可以对。而词频有 7915 个
# 两表都有的词可以做留出验证。一个能量的判据胜过两个不能量的（第 20 条：
# 同一件事两套判据必然分叉）。
#
# 阈值怎么定的——这一段是重点，因为第一次算出来的数是**错的**。
#
#   · 先按「词频 <= r 的词里有 95% 真在 L 档以内」反解，得到 B2 = 7386。
#     看着很稳。
#   · 但那个 95% 是在**留出集**上算的，而留出集就是 CEFR-J 自己的词表——
#     A1-B2 占 81%。真正会走到回落的是它**不认识**的词，那批先验上更难。
#     基率一变，估出来的精度必然偏乐观。
#   · 于是换个问法验：把 C1/C2 那 1528 个词藏起来，假装 CEFR-J 不认识它们，
#     看回落会把多少个放进 B2。答案是 **250 个，16.4%**——说好的 5% 泄漏，
#     实际是 16%。
#
# 所以阈值必须落在每一档的**严端**，不是中位：**CEFR-J 不认识的词，先验上比
# 它认识的更难**，那就按它认识的那批里最难的四分之一来卡。取各档观测到的
# 25 分位，藏难词再验一遍：B2 档漏放降到 3.2%（49/1528），而 330 个未收录的
# 常用词被放行，上面点名的那几个一个不落。3.2% 落在 CEFR-J 自己相邻档的
# 噪声以内（人工评级在相邻档上本来就常有分歧），可以接受。
#
# 两条守住的：
#   · **只回落，不覆盖。** CEFR-J 有的词一律以它为准——它是人工分级，
#     词频只是代用品。aesthetic（真值 C1）、cognitive（真值 C2）按词频都会被
#     判成 B2，正好说明这条不能反过来。
#   · **没有证据就不放行。** 词典里查不到、或者查到了但没有词频的词，
#     照旧判超纲（half-finished 就是）。方向仍然是「往严不往松」。
#
# 数字要重新标定时跑 scripts/ 下那几个标定脚本的做法：把 dict.csv 和
# cefr.csv 的交集按档分组、取各档 frq 的 25 分位。别拍脑袋改。
# ---------------------------------------------------------------------------

#: 词频排名 <= 这个数，就认这一档。取自 7915 个双表词各档 frq 的 25 分位。
FREQ_CUTOFFS: dict[str, int] = {
    "A1": 316, "A2": 1125, "B1": 2331, "B2": 4302, "C1": 7582,
}


@lru_cache(maxsize=1)
def _freq_table() -> dict[str, int]:
    """词形（小写）-> 词频排名。词典缺失就返回空表，回落自动关掉。

    直接借 core.wordbook.dictionary 那份已经缓存好的表，不自己再读一遍 CSV：
    「这个 CSV 长什么样」只该有一处知道（第 20 条）。函数内 import 是为了
    不在 core.lexicon 的导入期把词典也拉起来——它 3.7MB，而绝大多数用到
    lexicon 的地方（测试尤其）根本不需要它。
    """
    try:
        from core.wordbook import dictionary
    except ImportError:                      # pragma: no cover
        return {}
    out: dict[str, int] = {}
    for word, row in dictionary._load().items():
        raw = (row.get("frq") or "").strip()
        if raw.isdigit() and int(raw) > 0:
            out[word] = int(raw)
    return out


def freq_available() -> bool:
    return bool(_freq_table())


def _level_from_freq(word: str) -> str | None:
    """按词频给一个等级。没有词频数据就返回 None（= 没有证据，照旧判超纲）。

    和 level_of 一样在词形候选上取**最容易**的那一个（词频里就是排名最小的），
    理由同 level_of：只要这个词形有一种读者认得的读法，他就读得下去。
    """
    table = _freq_table()
    if not table:
        return None
    ranks = [table[cand] for cand in lemma_candidates(word) if cand in table]
    if not ranks:
        return None
    best = min(ranks)
    for level in LEVELS[:-1]:
        if best <= FREQ_CUTOFFS[level]:
            return level
    return "C2"


def level_of(word: str) -> str | None:
    """查一个词的 CEFR 等级，自动尝试词形还原。查不到返回 None。

    候选里命中多个时取**最容易的那一档**，不是第一个命中的。

    原来是「第一个命中就返回」，而 lemma_candidates 把词本身排在最前面，
    于是**屈折形自己也是词条时，它就把原形挡住了**：

        Standing → 命中 standing（C2，名词「地位」），A1 的 stand 轮不到
        cones    → 命中 con（C1），cone 轮不到

    这不是小数点问题：check_paragraph 会据此判 too_hard，拿一次修复调用
    去要求模型「把 Standing 换成 CEFR B2 以内的说法」——花钱把一段本来
    合格的文章改坏，而界面上只显示「第 N 段校验未过」。

    取最容易的那一档，和 _load() 里「同一个词有多条词性记录时取最容易的
    那一级」是同一条规矩：这把标尺要回答的是「读者读不读得下去」，
    只要这个词形有一种读者认得的读法，他就读得下去。

    代价量过（8653 条词表）：518 个词条判定变松，71 个越过 B2 线。
    绝大多数是对的——reluctantly ← reluctant、standing ← stand、trying ← try、
    revealing ← reveal 这类叙事文里的高频词。只有三四个是过度还原：
    batter ← bat、charter ← chart、wares ← war、flatter ← flat。
    被修正的是高频词，被放松的是低频词，这个交换划算；残留的那几个
    记在 需要注意.md 第 4c 条里。

    min 遇到并列取先出现的，而 lemma_candidates 把词本身排在最前——
    等级一样时仍然以词本身为准，行为不变。

    CEFR-J 一个候选都没命中时，才回落到词频（见上面 FREQ_CUTOFFS 那一段）。
    **顺序不能反**：CEFR-J 是人工分级，词频只是代用品。
    """
    table = _load()[0]
    hits = [table[cand] for cand in lemma_candidates(word) if cand in table]
    if hits:
        return min(hits, key=lambda lv: LEVEL_INDEX[lv])
    return _level_from_freq(word)


def resolve(word: str) -> str:
    """用词表仲裁出最可信的原形；词表里没有就退回启发式结果。"""
    table = _load()[0]
    cands = lemma_candidates(word)
    for cand in cands:
        if cand in table:
            return cand
    if not cands:
        return word.lower()
    return cands[1] if len(cands) > 1 else cands[0]


def level_counts() -> dict[str, int]:
    """每一级「及以下」的累计词数。

    界面上要回答的是「选 B2 意味着文章能用多大的词汇量」，那是累计值而不是
    本级词数——用词上限是个天花板，B2 以下的词当然也能用。

    数字必须现算，不能写死在模板里：没下载 CEFR-J 时词表会退回内置兜底表，
    那张表只有两千来个词且全标 A1，此时写死的数字会和程序实际执行的标尺
    对不上——而「界面说的」和「实际拦的」不一致，正是用户没法自己发现的那类错。
    调用方拿 is_real_data() 决定要不要显示这些数字。

    **词频回落进来的词也要算。** 同样是那条「界面说的必须等于实际拦的」：
    标尺现在放行 CEFR-J 之外的一批常用词（见 FREQ_CUTOFFS），这里不跟着算，
    首页那个「B2 = 6863 词」就比程序实际允许的少了几百个——而这种不一致
    恰好是用户没法自己发现的。口径只有一个：**level_of 认它是这一档，它就该被数进来。**
    """
    table = _load()[0]
    per: dict[str, int] = dict.fromkeys(LEVELS, 0)
    for level in table.values():
        per[level] += 1
    # 回落只对 CEFR-J 没收的词生效，所以这里数的是「词典有、CEFR-J 没有」的那部分
    for word in _freq_table():
        if word not in table:
            level = _level_from_freq(word)
            if level:
                per[level] += 1
    out, running = {}, 0
    for level in LEVELS:
        running += per[level]
        out[level] = running
    return out


def normalize_level(value: object, fallback: str = "B2") -> str:
    """把外面传进来的用词上限收敛成 LEVELS 里的一个。

    认不出来就退回 fallback，而不是让它一路走到 within()。原来那里查不到时
    取的是**最宽松**的一档，于是一个 `b2`、一个尾随空格、或者
    settings.local.json 里留下的一个旧值，就能让整把难度标尺静默失效——
    界面上写着 B2，实际按 C2 放行。实测同一段 C2 堆砌文本：
    传 'B2' 判出 13 个超纲词，传 'b2' 只判出 11 个。

    「界面说的」和「实际拦的」不一致，正是这个项目里用户没法自己发现的那类错：
    能判断「这篇文章的用词是不是超了 B2」的人，本来就不需要这个功能。
    """
    text = value.strip().upper() if isinstance(value, str) else ""
    return text if text in LEVEL_INDEX else fallback


def within(word: str, max_level: str) -> bool:
    """该词是否在 max_level 及以下。查不到等级一律视为超纲。"""
    lv = level_of(word)
    if lv is None:
        return False
    # 认不出来的上限按**最严**算（A1），不是最宽松。调用方本该先过
    # normalize_level，所以这条分支正常走不到；真走到了，宁可整段都判超纲、
    # 让修复循环当场炸出来，也不要安安静静地把标尺放到最宽——
    # 后者没有任何人会发现，而这正是「把沉默的失败换成可见的失败」那条。
    return LEVEL_INDEX[lv] <= LEVEL_INDEX.get(max_level, 0)


def scan(text: str, max_level: str, *, allow: set[str] | None = None,
         studied: set[str] | None = None) -> dict:
    """扫描文本找出超纲词。

    allow    本次的目标词——它们本来就是要学的生词，不算超纲。
    studied  这个用户词库里已有的词。难度上限本来就是「读者认不认得」的
             代用品，而对这些词有直接证据，不必再拿 CEFR 等级去猜。

    两个分开而不是合成一份，理由是**匹配方式不一样**，不是语义洁癖：
    allow 很小（一篇文章几个词），所以除了还原后相等，还额外跑一遍
    same_word 去认派生形式；studied 会随使用一直长（几百上千个词），
    在每个待判词上再套一层 O(N) 的 same_word 会把扫描拖垮，
    所以它只做「还原后相等」。词库里存的本来就是 resolve 过的原形，
    对得上。

    **这里不接受模型申报的人名。** 曾经有过一个 names 参数，选题阶段模型
    报什么就无条件放行什么。问题不在「模型会不会撒谎」，在于**被检查的一方
    控制了检查器**——而 prompt 里那句「漏报了，你的角色名会被当成超纲词退回来
    重写」是单边施压：多报没有代价，少报要挨一次重写，模型自然会多报。
    构造一下能把超纲率从 26% 压到 7%，而界面上只会显示一个更好看的数字。

    剩下的判据只有一条，而且是**独立于模型的**：这个大写词在别处以大写出现在
    句子中间。真专有名词几乎一定会（叙事里名字大量出现在句中），
    而句首大写只是句子开头，不构成任何证据。
    只在句首露过面的名字会被当成生词退回来重写一次，然后用户标一次「忽略」，
    从此不再犯——这正是 Lute 那个循环。实测本机 7 篇：完全不信 names，
    多余修复 0 次。
    """
    targets = [w.strip() for w in (allow or set()) if w and w.strip()]
    allow_lemmas = {resolve(w) for w in targets}
    studied_lemmas = {resolve(w) for w in (studied or set()) if w and w.strip()}
    revisited: set[str] = set()          # 学过的词这次又出现了——这是好消息，报出去
    spans = tokenize_spans(text)
    # 句中出现的大写词是专有名词的独立证据，不依赖模型报得全不全
    mid_sentence_caps: set[str] = set()

    # 先扫一遍位置，把句中大写的词收集出来
    starts: list[bool] = []
    prev_end: int | None = None
    for tok, start, end in spans:
        at_start = prev_end is None or bool(_SENT_BREAK.search(text, prev_end, start))
        starts.append(at_start)
        prev_end = end
        if not at_start and tok[0].isupper():
            mid_sentence_caps.add(tok.lower())

    total = 0
    offenders: dict[str, dict] = {}
    for (tok, _s, _e), at_sentence_start in zip(spans, starts):
        if len(tok) < 2:
            continue
        if tok[0].isupper():
            low = tok.lower()
            # 句中大写 = 专有名词，跳过。
            # 句首大写只是句子开头，不能一起跳——那等于每句第一个词都逃过
            # 检测，模型只要把生词放句首就绕开了整把标尺。只在有专有名词
            # 实据时才跳，而实据只认一条：它在别处以大写出现在句中。
            # （模型自己申报的 names 不算实据，理由见函数开头。）
            if not at_sentence_start or low in mid_sentence_caps:
                continue
        total += 1
        base = resolve(tok)
        if base in allow_lemmas:
            continue
        if base in studied_lemmas:
            # 学过的词不算超纲。记下来：一篇新文章里出现旧词，正是这个产品
            # 声称最有效的那个机制（多语境重复），不该悄悄发生。
            revisited.add(base)
            continue
        if within(tok, max_level):
            continue
        # 到这里这个词就要被判成超纲了。判之前必须再问一次：它是不是某个
        # 目标词的另一种形态？
        #
        # 上面那条「还原后相等」的快路只在两边能碰头时成立，而**目标词的派生
        # 形式自己就是词表词条**时它碰不了头：resolve("abandoned") 得到的是
        # abandoned（B2 词条）而不是 abandon，allow 里那个 abandon 永远等不到。
        # 词表里这样的组合有 983 对，其中 496 对派生形式的等级比原形更高
        # ——也就是恰好会顶破用词上限的那些。
        #
        # 后果不是「少判一个词」这么轻：check_paragraph 会据此判 too_hard，
        # 拿修复预算去要求模型「把 abandoned 换成 B2 以内的说法」，
        # 即花钱让它删掉这篇文章的目标词本身；stats 里还会把目标词列进
        # 「文中仍有超纲词」。而这一切用户都看不出来。
        #
        # same_word 是这个项目对「这是不是同一个词的另一种形态」的既定判据，
        # _appears 判「目标词出现了没有」用的就是它。两边共用一个判据，
        # 才不会一边说「出现了」一边说「超纲了」。它只在本来就要报错的
        # 分支上跑，顺风路径零开销。
        if any(same_word(w, tok) for w in targets):
            continue
        item = offenders.setdefault(
            base, {"lemma": base, "surface": tok, "level": level_of(tok), "count": 0}
        )
        item["count"] += 1
    off_total = sum(o["count"] for o in offenders.values())
    return {
        "total_words": total,
        "offenders": sorted(offenders.values(), key=lambda o: -o["count"]),
        "offender_count": off_total,
        "offender_rate": (off_total / total) if total else 0.0,
        "revisited": sorted(revisited),
        "using_real_data": is_real_data(),
    }
