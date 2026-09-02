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


def level_of(word: str) -> str | None:
    """查一个词的 CEFR 等级，自动尝试词形还原。查不到返回 None。"""
    table = _load()[0]
    for cand in lemma_candidates(word):
        if cand in table:
            return table[cand]
    return None


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
    """
    table = _load()[0]
    per: dict[str, int] = dict.fromkeys(LEVELS, 0)
    for level in table.values():
        per[level] += 1
    out, running = {}, 0
    for level in LEVELS:
        running += per[level]
        out[level] = running
    return out


def within(word: str, max_level: str) -> bool:
    """该词是否在 max_level 及以下。查不到等级一律视为超纲。"""
    lv = level_of(word)
    if lv is None:
        return False
    return LEVEL_INDEX[lv] <= LEVEL_INDEX.get(max_level, len(LEVELS) - 1)


def scan(text: str, max_level: str, *, allow: set[str] | None = None,
         names: set[str] | None = None) -> dict:
    """扫描文本找出超纲词。

    allow  传本次的目标词——它们本来就是要学的生词，不算超纲。
    names  传选题阶段声明的人名 / 地名。有了它就不用再猜句首那个大写词
           到底是专有名词还是生词——生成的时候本来就知道，别把信息扔了再猜。
    """
    targets = [w.strip() for w in (allow or set()) if w and w.strip()]
    allow_lemmas = {resolve(w) for w in targets}
    declared = {n.lower() for n in (names or set())}
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
            # 实据时才跳：选题阶段声明过，或者它在别处以大写出现在句中。
            if not at_sentence_start or low in declared or low in mid_sentence_caps:
                continue
        total += 1
        base = resolve(tok)
        if base in allow_lemmas:
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
        "using_real_data": is_real_data(),
    }
