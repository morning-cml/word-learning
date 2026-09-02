"""生成 core/lexicon/irregular_forms.py。

用法（需要临时装一个只在这里用得上的库）：

    pip install lemminflect
    .venv\\Scripts\\python.exe scripts\\gen_irregular.py

**搬的是数据，不是依赖。** LemmInflect 的词形数据来自 NIH SPECIALIST Lexicon，
准确率比规则法高得多；但它本体 0.9MB 却要拖 37MB 的 numpy，而 run.bat
「双击就能用」那条承诺建在只有几个运行期依赖上面。所以在这里跑一次，
把结果提交进仓库，运行期一个依赖都不加。

为什么需要这张表：规则法还原不了真·不规则形态（arose / awoke / analyses）。
认不出来的后果不是「少认一个变形」——`_appears` 会误报「目标词没有在本段出现」，
管线据此烧掉一次修复调用，而且修复很可能把一段本来合格的文章改坏；
更要紧的是 `_normalize` 补不上这个 target，**这一处语境就不会写进 Encounter**，
而累计语境是这个应用唯一不可再生的资产。

--------------------------------------------------------------------------
筛选判据。**宁可漏收，不可错收**——两种错误的代价差着量级：

  漏收一条 → 偶尔多烧一次修复调用。有界，且下次生成就没事了。
  错收一条 → 把 A 词判成 B 词的形态，于是「目标词已出现」被骗过去，
             错的词还会被当成 surface 写进 Encounter（见 需要注意.md 第 5 条）。
             写进库的东西不可再生。

所以下面每一条都是「拿不准就丢掉」：

  1. 指向多个原形          better→good/well、worse→bad/ill/wrong
  2. 带空格或连字符        book shelves、over-took——分词器切不出来，收了也没用
  3. 异干比较级            more→much、elder→old。按 需要注意.md 第 5 条的既定判据，
                           用户说要学 more，词条面板却标着 much 是错的
  4. 形态自己就是词表词条  bit→bite、bore→bear、lay→lie、could→can。
                           它自己是个独立的词，并过去就把那个词藏了
  5. 形态自己是高频常用词  born→bear。第 4 条只看 CEFR 词表，而 CEFR-J 恰好没收 born；
                           内置兜底表（最高频那两千词）能补上这个缺口
  6. 同形异义              bases→basis 但也可能是 base 的复数；leaves→leaf 或 leave

第 4、5 两条放行的是「这个形态在别处没有独立身份」的那些——arose、awoken、
analyses 都只可能是某个词的形态，并进去不会藏起任何东西。

剩下一类没有过滤：aquae / areae / beeves 这种拉丁语或古体复数。它们收着无害
（真实文本里不会出现），要滤掉得有词频数据，而这里没有——留着比为它引一个
新数据源划算。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "core" / "lexicon" / "irregular_forms.py"

COMPARATIVE = {"JJR", "JJS", "RBR", "RBS"}
VERB = {"VBD", "VBN", "VBZ", "VBG"}
NOUN = {"NNS"}

HEADER = '''"""不规则词形表（生成物，不要手改）。

由 scripts/gen_irregular.py 从 LemmInflect 的词形数据筛选而来，
筛选判据写在那个脚本的模块注释里——**改判据要去改脚本，然后重新生成**。
手改这个文件的话，下一次重新生成就把你的改动冲掉了。

数据来源：LemmInflect（MIT），其词形数据来自 NIH SPECIALIST Lexicon。
搬数据不搬依赖：运行期不需要 LemmInflect，也不需要 numpy。

和 lemma.py 里那张手写的 IRREGULAR 的分工：
  · 手写表编码的是**决策**（哪些该并、哪些刻意不并），它优先级更高；
  · 这张表补的是**规则法推不出、而且并过去不会藏起别的词**的那些形态。
两张表在 lemma.py 里合并，冲突时以手写表为准。
"""
from __future__ import annotations

# 形态 -> 原形。{count} 条：不规则动词 {verbs} 条，不规则复数 {nouns} 条。
GENERATED: dict[str, str] = {{
'''


def build() -> tuple[dict[str, str], dict[str, set[str]], dict[str, list]]:
    try:
        from lemminflect import getAllInflections
    except ImportError:
        raise SystemExit(
            "需要先装 LemmInflect：pip install lemminflect\n"
            "（它只在这个脚本里用得上，不进 requirements.txt）"
        ) from None

    from core.lexicon import cefr
    from core.lexicon.lemma import IRREGULAR, _suffix_candidates

    table = cefr._load()[0]
    if not cefr.is_real_data():
        raise SystemExit(
            "当前用的是内置兜底词表（只有两千来个词且全标 A1），"
            "按它生成出来的表会缺一大片。先跑 scripts/fetch_cefr.py。"
        )
    common = set(cefr._FALLBACK)

    candidates: dict[str, set[str]] = defaultdict(set)
    tags: dict[str, set[str]] = defaultdict(set)
    for word in table:
        for tag, forms in getAllInflections(word).items():
            for form in forms:
                if form == word or form in IRREGULAR:
                    continue
                if word in _suffix_candidates(form):
                    continue          # 规则法已经能还原，不用进表
                candidates[form].add(word)
                tags[form].add(tag)

    keep: dict[str, str] = {}
    dropped: dict[str, list] = defaultdict(list)
    for form, bases in sorted(candidates.items()):
        tag = tags[form]
        if len(bases) > 1:
            dropped["指向多个原形"].append(form)
            continue
        base = next(iter(bases))
        if not form.isalpha():
            dropped["带空格或连字符"].append(form)
            continue
        if tag & COMPARATIVE and not (tag & (VERB | NOUN)):
            dropped["异干比较级"].append(form)
            continue
        if form in table:
            dropped["形态自己就是词表词条"].append(form)
            continue
        if form in common:
            dropped["形态自己是高频常用词"].append(form)
            continue
        others = sorted({c for c in _suffix_candidates(form)
                         if c in table and c != base})
        if others:
            dropped["同形异义"].append(form)
            continue
        keep[form] = base

    return keep, tags, dropped


def main() -> int:
    keep, tags, dropped = build()
    # 按**这个形态自己**的词性分类，不是按原形的——同一个原形往往两类都有
    nouns = sum(1 for f in keep if tags[f] & NOUN and not tags[f] & VERB)
    verbs = len(keep) - nouns

    body = "".join(f'    {f!r}: {b!r},\n' for f, b in sorted(keep.items()))
    OUT.write_text(
        HEADER.format(count=len(keep), verbs=verbs, nouns=nouns) + body + "}\n",
        encoding="utf-8",
    )

    print(f"已写入 {OUT.relative_to(ROOT)}：{len(keep)} 条")
    for reason, forms in sorted(dropped.items(), key=lambda kv: -len(kv[1])):
        print(f"  丢弃 · {reason}（{len(forms)}）：{'、'.join(forms[:8])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
