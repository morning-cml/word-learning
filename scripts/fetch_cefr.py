"""下载 CEFR-J 词表到 data/cefr.csv。

来源：openlanguageprofiles/olp-en-cefrj
  - CEFR-J Vocabulary Profile 1.5（A1-B2），Tono Laboratory 版权，研究与商用免费，需注明出处
  - Octanove Vocabulary Profile C1/C2 1.0，CC BY-SA 4.0

用法：  .venv\\Scripts\\python.exe scripts\\fetch_cefr.py
不跑这个脚本程序也能用，只是难度判定会退回内置的高频词兜底表。
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "cefr.csv"
BASE = "https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/master/"
SOURCES = (
    "cefrj-vocabulary-profile-1.5.csv",
    "octanove-vocabulary-profile-c1c2-1.0.csv",
)
ORDER = {lv: i for i, lv in enumerate(("A1", "A2", "B1", "B2", "C1", "C2"))}
HEADWORD_KEYS = ("headword", "word", "lemma")


def parse(text: str, into: dict[str, str]) -> int:
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        key = next((k for k in row if k and k.strip().lower() in HEADWORD_KEYS), None)
        lvk = next((k for k in row if k and "cefr" in k.strip().lower()), None)
        if lvk is None and "level" in row:
            lvk = "level"
        if not key or not lvk:
            continue
        word = (row[key] or "").strip().lower()
        level = (row[lvk] or "").strip().upper()[:2]
        if not word or level not in ORDER:
            continue
        if word not in into or ORDER[level] < ORDER[into[word]]:
            into[word] = level
        n += 1
    return n


def main() -> int:
    rows: dict[str, str] = {}
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for name in SOURCES:
            print(f"下载 {name} ...", end=" ", flush=True)
            try:
                r = client.get(BASE + name)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                print(f"失败：{exc}")
                continue
            print(f"{parse(r.text, rows)} 条")

    if not rows:
        print("\n没拿到任何数据。检查网络后重试；程序会继续用内置兜底词表。")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["headword", "CEFR"])
        for word in sorted(rows):
            writer.writerow([word, rows[word]])

    dist: dict[str, int] = {}
    for lv in rows.values():
        dist[lv] = dist.get(lv, 0) + 1
    print(f"\n已写入 {OUT}（共 {len(rows)} 词）")
    print("分布： " + "  ".join(f"{lv}={dist[lv]}" for lv in sorted(dist, key=lambda x: ORDER[x])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
