"""生成 data/wordbook/dict.csv —— 背单词页用的参考词典。

    .venv\\Scripts\\python.exe scripts\\build_wordbook_dict.py
    .venv\\Scripts\\python.exe scripts\\build_wordbook_dict.py --source 本地的ecdict.csv

数据来源：ECDICT（https://github.com/skywind3000/ECDICT，MIT，Copyright (c) 2025 Linwei）。
许可与出处一并写进 data/wordbook/README.md，那个文件跟着生成物一起提交。

--------------------------------------------------------------------------
**搬的是数据，不是依赖**，和 scripts/gen_irregular.py 同一个做法：ECDICT 原始表
66MB、77 万词，跑一次筛出用得上的那部分提交进仓库，运行期不下载、不联网。
「双击 run.bat 就能用」那条承诺不能因为加了个背单词页就破掉。

为什么不是只留六级 + 考研那 6096 个词（0.78MB）：
纸质书的一页上不只有核心词——派生词、词组、以及编者顺手带的超纲词都会出现，
而**匹配不上的词对用户表现为「这个词查不到」**，得他自己补。所以宁可多带一些：
    考纲 8 个标签  ∪  有柯林斯星级  ∪  收进牛津 3000  ∪  词频前 3 万
实测 33217 词 / 3.7MB。多出来的 2.9MB 换的是「贴一页进去基本不用管」。

派生词是**这里算出来的，不是 ECDICT 给的**。ECDICT 的 exchange 字段只有屈折
形式（abandon -> abandoned / abandoning / abandons），没有 abandonment 这类
派生词——而用户要的恰恰是后者。算法是拿 core.lexicon.lemma.forms_of 的候选
加一张后缀表，去**整本 77 万词**里查存在性：池子越大命中越高，而池子只在
构建时需要，运行期一个字节都不占。实测六级词里 92% 能扫出至少一个派生词，
只拿输出的那 3.3 万当池子的话会掉到 53%——这就是为什么池子必须是全表。
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "wordbook"
OUT = OUT_DIR / "dict.csv"
CACHE = OUT_DIR / "ecdict.csv"          # 66MB 的原始表，.gitignore 里排除了
URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"

# 考纲标签。带上四级和高考不是为了考它们，是因为六级/考研书里本来就混着这些词
EXAM_TAGS = {"zk", "gk", "cet4", "cet6", "ky", "toefl", "ielts", "gre"}
FREQ_TOP = 30000

# 派生后缀。收的判据是「加上去之后那个词真的存在于 ECDICT」，所以这张表宁可写宽——
# 写宽只会多试几次查表，写窄会漏掉真的派生词。
SUFFIXES = (
    "ly", "ment", "ness", "tion", "sion", "ity", "ive", "able", "ible", "al", "ic",
    "ous", "ful", "less", "er", "or", "ist", "ism", "ize", "ise", "ation", "ance",
    "ence", "ant", "ent", "ship", "hood",
)
MAX_DERIVATIVES = 8

COLUMNS = ("word", "phonetic", "translation", "exchange", "derivatives",
           "collins", "frq", "tag")


def _download(dst: Path) -> None:
    import httpx

    print(f"下载 {URL} ...（66MB，只需要这一次）", flush=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_suffix(".partial")
    try:
        with httpx.stream("GET", URL, timeout=300.0, follow_redirects=True) as r:
            r.raise_for_status()
            with partial.open("wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
        partial.replace(dst)
    finally:
        partial.unlink(missing_ok=True)


def _rows(path: Path) -> list[dict]:
    csv.field_size_limit(1 << 24)
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _clean_translation(raw: str) -> str:
    r"""ECDICT 把多个义项写成**字面的两个字符 `\n`**，不是真换行。

    按真换行去切的话一个都切不开，义项会连着 `\n` 一起显示在卡片背面——
    看起来像乱码，而且不会报错。这一条踩过一次，留着。
    """
    parts = [p.strip() for p in (raw or "").replace("\\n", "\n").split("\n")]
    # [网络] 那一档是机器翻译聚合来的，质量最差，背单词不需要
    parts = [p for p in parts if p and not p.startswith("[网络]")]
    return " / ".join(parts)[:220]


def _derivatives(word: str, pool: set[str]) -> list[str]:
    from core.lexicon.lemma import forms_of

    low = word.lower()
    out: set[str] = set()
    for cand in forms_of(low):                 # 规则屈折：-s / -ed / -ing / -er ...
        if cand != low and cand in pool:
            out.add(cand)
    stems = [low]
    if low.endswith("e"):
        stems.append(low[:-1])                 # create -> creative
    if low.endswith("y"):
        stems.append(low[:-1] + "i")           # happy -> happiness
    for suf in SUFFIXES:
        for stem in stems:
            cand = stem + suf
            if cand != low and cand in pool:
                out.add(cand)
    return sorted(out)[:MAX_DERIVATIVES]


def build(source: Path) -> int:
    rows = _rows(source)
    print(f"读入 {len(rows)} 条")

    def tags(r: dict) -> set[str]:
        return set((r.get("tag") or "").split())

    keep: dict[str, dict] = {}
    ranked = sorted(
        (r for r in rows if (r.get("frq") or "0").isdigit() and int(r["frq"]) > 0),
        key=lambda r: int(r["frq"]),
    )
    for r in ranked[:FREQ_TOP]:
        keep[r["word"]] = r
    for r in rows:
        if tags(r) & EXAM_TAGS or (r.get("collins") or "").strip() \
                or (r.get("oxford") or "").strip():
            keep[r["word"]] = r
    # 词组和带符号的条目留着没用：粘进来的是一页里的单词，匹配不上它们
    keep = {w: r for w, r in keep.items()
            if w and w.replace("-", "").replace(" ", "").isalpha()}

    # 池子是**整本**，不是上面筛出来的那部分（见模块注释）
    pool = {r["word"].lower() for r in rows if r["word"].isalpha()}

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    with_deriv = 0
    for word in sorted(keep, key=str.lower):
        r = keep[word]
        deriv = _derivatives(word, pool)
        with_deriv += bool(deriv)
        writer.writerow([
            word,
            r.get("phonetic") or "",
            _clean_translation(r.get("translation") or ""),
            r.get("exchange") or "",
            ",".join(deriv),
            r.get("collins") or "",
            r.get("frq") or "",
            r.get("tag") or "",
        ])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(buf.getvalue(), encoding="utf-8", newline="")
    size = OUT.stat().st_size / 1024 / 1024
    print(f"已写入 {OUT.relative_to(ROOT)}：{len(keep)} 词，{size:.2f} MB")
    print(f"  有派生词的：{100 * with_deriv / len(keep):.0f}%")
    per_tag = {t: sum(1 for r in keep.values() if t in tags(r)) for t in sorted(EXAM_TAGS)}
    print("  考纲分布：" + "  ".join(f"{t}={n}" for t, n in per_tag.items()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=None,
                    help="本地的 ecdict.csv；不给就用缓存，缓存也没有就下载")
    args = ap.parse_args()

    source = args.source or CACHE
    if not source.is_file():
        _download(CACHE)
        source = CACHE
    return build(source)


if __name__ == "__main__":
    sys.exit(main())
