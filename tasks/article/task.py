"""文章生成任务：选题 → 按段写 → 校验修复。"""
from __future__ import annotations

import math
import re
from typing import Iterator

from core.lexicon import cefr
from core.lexicon.lemma import forms_of, same_word
from core.llm.client import LLM
from tasks.base import Event, Problem, Task, register

from . import prompts
from .schema import (
    AUDIT_SCHEMA, PARAGRAPH_SCHEMA, PLAN_SCHEMA,
    coerce_audits, coerce_glossary, coerce_paragraph, coerce_plan,
)

# 每段承载的目标词数。研究（SRS-Stories）给的经验区间是每篇 10-20 个，
# 但那是在「其余全部是已知词」的前提下；我们的生词密度更高，所以压到每段 3 个。
WORDS_PER_PARAGRAPH = 3
MAX_PARAGRAPHS = 6

# 每段句数 = 本段目标词数 + 这个富余量。富余的那几句不承载目标词，
# 用来推情节、以及给相邻句留出铺线索的地方。
#
# **篇幅必须跟着词数走。** 之前每段句数写死 5 句、段数又有两段下限，
# 结果是 1 到 6 个词全都得到「2 段约 170 词」——给一个词和给六个词读一样长的
# 东西，多出来的全是稀释。18 到 40 个词那头同样平：全是 510 词。
#
# 富余量取 0，也就是「每个目标词一句」，总长落在词数的 17-20 倍。
# 这个值没有实测依据——本机 7 篇里只有 3 篇有线索审计数据，而且全在
# 43-59 倍那一端，17-28 倍区间一个观测都没有。
#
# 之所以敢先按低值发：**这个风险有仪表。** clue_strength 每次生成都测，
# 而且是结果面板的第一个数字、文库列表里的分段条。密度真伤到线索，
# 下一两篇就会看见 3/5 而不是 5/5——错了会响，不是沉默失败。
# 真降下来了，把这个常数调回 1 或 2 即可（1 ≈ 23 倍，2 ≈ 28 倍）。
SENTENCE_SCAFFOLD = 0
# 再少就不成篇了。一句话的「文章」没有情节可言，也没有相邻句可以铺线索。
MIN_SENTENCES_PER_PARAGRAPH = 2

# 每句多少词。用来估篇幅，不是硬约束（prompt 里写的是 12-22 词）。
# 17 是从本机 7 篇实测的每句词数中位数来的，别拍脑袋改。
WORDS_PER_SENTENCE = 17

MAX_REPAIRS = 2
# 一次最多接受多少个词。再多，sizing() 会把每段压到十几个目标词，
# 一段里塞十几个生词读起来就是填空作业了。该做的是分批，所以在入口就拦住。
MAX_WORDS = 40
# 语境线索不达标时的重写轮数。这是整个管线里最值得花预算的地方——
# 词出现了但读者猜不出词义，这一遍就白读了，跟单词书没区别。
MAX_CLUE_FIXES = 2
# 超纲词占比阈值：超过就要求重写。2% 大约是 400 词文章里 8 个陌生词。
OFFENDER_LIMIT = 0.02
# 但单段只有几十个词，光看比例的话一个稍难的词就能顶破 2%，白烧一次修复调用。
# 所以再加一条绝对下限：每段至多容忍这么多个超纲词，不管比例算出来多少。
OFFENDER_FLOOR = 1


def sizing(n_words: int) -> tuple[int, int, int]:
    """按目标词数量决定篇幅。返回 (段数, 每段词数, 每段句数)。

    三个数都跟着 n_words 走——尤其是句数。它以前是个常数，那正是
    「给 1 个词和给 6 个词读到一样长的文章」的根因。
    """
    n_para = max(1, min(MAX_PARAGRAPHS, math.ceil(n_words / WORDS_PER_PARAGRAPH)))
    per_para = max(1, math.ceil(n_words / n_para))
    n_sent = max(MIN_SENTENCES_PER_PARAGRAPH, per_para + SENTENCE_SCAFFOLD)
    return n_para, per_para, n_sent


def estimated_words(n_para: int, n_sent: int) -> int:
    """这批规划大概会写出多少词。首页在调模型之前就要告诉用户篇幅。"""
    return n_para * n_sent * WORDS_PER_SENTENCE


def _appears(word: str, text: str) -> str | None:
    """在文本里找这个词的任意变形，返回实际出现的形态。

    两条路都要走：规则法生成的变形只能覆盖规则变化，
    ran / went / brought 这类不规则形式必须靠词形还原才能认出来——
    漏认会让校验误报「目标词没出现」，白烧一次修复调用。
    """
    low = text.lower()
    for form in sorted(forms_of(word), key=len, reverse=True):
        m = re.search(rf"\b{re.escape(form)}\b", low)
        if m:
            return text[m.start():m.end()]

    for m in re.finditer(r"[A-Za-z][A-Za-z'\-]*", text):
        token = m.group(0)
        if same_word(word, token):
            return token
    return None


class ArticleTask(Task):
    id = "article"
    name = "生成文章"
    description = "把一批单词写成一篇有情节的短文，句子级中英对照"

    # ------------------------------------------------------------------ 选题

    @staticmethod
    def _assign_words(planned: list[dict], words: list[str],
                      unplaced: list[str]) -> tuple[list[dict], list[str]]:
        """把选题结果的词分配收敛回本次真正要学的这批词。

        模型有两个稳定的跑偏方向，代价都直接落在修复预算上：

        · 自己往里加词。加进来的词不在 allow 里，于是校验一边要求它出现、
          一边把它判成超纲词——两条指令互相打架，谁也满足不了，
          MAX_REPAIRS 就全烧在一个用户根本没要求学的词上。
        · 把同一个词分给两段。prompt 里写了「同一个词只分配给一段」，
          但那只是请求。两段都得硬塞一次，线索审计也会把它数两遍——
          界面上「语境线索充分 3/2」这种分母比总数还小的数字就是这么来的。

        以用户给的拼写为准：下游的 allow、_appears、以及入库的词条 key
        都按它对齐，用模型回声里的大小写会对不上。

        unplaced 是模型自己说「这批词里这几个塞不进这个题材」。prompt 里写着
        「别硬塞——硬塞会毁掉整篇的可读性」，但代码一直在硬塞：漏掉的词
        统统补回最后一段。结果是一篇被稀释过的文章，而用户看到的是「生成完成」。
        现在区分两种情况——模型说塞不进的，放掉并如实报出来；模型只是忘了的，
        照旧补回去。返回 (分配结果, 真正没覆盖到的词)。
        """
        canonical: dict[str, str] = {}
        for word in words:
            canonical.setdefault(word.lower(), word)      # 同一个词以首次出现的拼写为准
        taken: set[str] = set()
        cleaned: list[dict] = []
        for para in planned:
            keep = []
            for raw in para["words"]:
                key = raw.lower()
                if key in canonical and key not in taken:
                    taken.add(key)
                    keep.append(canonical[key])
            cleaned.append({**para, "words": keep})
        leftover = [w for key, w in canonical.items() if key not in taken]
        declined = {u.lower() for u in unplaced}
        dropped = [w for w in leftover if w.lower() in declined]
        forgotten = [w for w in leftover if w.lower() not in declined]

        # 模型说全都塞不进，那是它没理解任务，不是真的塞不进。
        # 照它说的做会产出一篇一个目标词都没有的文章——白花一次钱。
        if not taken and not forgotten:
            return [{**p, "words": list(canonical.values())} if i == len(cleaned) - 1
                    else p for i, p in enumerate(cleaned)], []

        if forgotten:
            cleaned[-1]["words"].extend(forgotten)
        return cleaned, dropped

    # ------------------------------------------------------------------ 校验

    def check_paragraph(self, para: dict, expected: list[str], level: str,
                        allow: set[str], names: set[str]) -> list[Problem]:
        problems: list[Problem] = []
        sentences = para.get("sentences") or []

        if not sentences:
            return [Problem("empty", "这一段没有任何句子")]

        for i, sent in enumerate(sentences, 1):
            if not (sent.get("en") or "").strip():
                problems.append(Problem("missing_en", f"第 {i} 句英文为空"))
            if not (sent.get("zh") or "").strip():
                problems.append(
                    Problem("missing_zh", f"第 {i} 句缺中文翻译",
                            "每句英文都必须配一句中文，不能空着")
                )

        text = " ".join((s.get("en") or "") for s in sentences)

        for word in expected:
            if _appears(word, text) is None:
                problems.append(
                    Problem("missing_target", f"目标词 {word} 没有在本段出现",
                            f"必须自然用上 {word}，并让上下文能推出它的意思")
                )

        report = cefr.scan(text, level, allow=allow, names=names)
        budget = max(OFFENDER_FLOOR, round(OFFENDER_LIMIT * report["total_words"]))
        if report["offender_count"] > budget:
            worst = [o["surface"] for o in report["offenders"][:8]]
            problems.append(
                Problem(
                    "too_hard",
                    f"本段有 {report['offender_count']} 个超纲词（占 "
                    f"{report['offender_rate']:.1%}，本段上限 {budget} 个）："
                    + "、".join(worst),
                    f"把这些词换成 CEFR {level} 以内的说法",
                )
            )
        return problems

    # -------------------------------------------------------------- 语境线索审计

    @staticmethod
    def audit_clues(llm: LLM, para: dict, expected: list[str]) -> list[dict]:
        """让模型扮演不认识这些词的读者，逐个判断能否从上下文推断词义。

        机械校验只能验「词出现了没有」，验不了「读者猜不猜得出来」。
        后者才是读文章记单词唯一起作用的机制，所以单开一次调用来审。
        """
        if not expected:
            return []
        text = " ".join((s.get("en") or "") for s in para.get("sentences", []))
        if not text.strip():
            return []
        audits = coerce_audits(llm.json(
            prompts.audit_prompt(text, expected),
            purpose="structured", max_tokens=2500, json_schema=AUDIT_SCHEMA,
        ))
        by_lemma = {a["lemma"].lower(): a for a in audits}
        # 模型漏审的词按最坏情况处理，不能默认通过
        for word in expected:
            by_lemma.setdefault(
                word.lower(),
                {"lemma": word, "strength": "none", "clue": "", "why": "审计未覆盖该词"},
            )
        return [by_lemma[w.lower()] for w in expected]

    @staticmethod
    def weak_clues(audits: list[dict]) -> list[dict]:
        return [a for a in audits if a.get("strength") != "strong"]

    # ------------------------------------------------------------------ 主流程

    def run(self, llm: LLM, params: dict) -> Iterator[Event]:
        words: list[str] = [w for w in params.get("words", []) if w.strip()]
        level: str = params.get("level", "B2")
        allow = set(words)
        names: set[str] = set()      # 选题阶段声明的人名地名，见 cefr.scan

        n_para, per_para, n_sent = sizing(len(words))
        yield {
            "type": "phase", "phase": "plan",
            "message": f"正在为 {len(words)} 个词选题，规划 {n_para} 段",
        }

        # --- 第一步：选题 ---
        plan = coerce_plan(llm.json(
            prompts.plan_prompt(words, n_para, per_para),
            purpose="structured", max_tokens=2000, json_schema=PLAN_SCHEMA,
        ))
        planned = plan["paragraphs"]
        if not planned:
            planned = [{"focus": "", "words": words[i::n_para]} for i in range(n_para)]

        names = set(plan["names"])
        planned, dropped = self._assign_words(planned, words, plan["unplaced"])
        if dropped:
            yield {
                "type": "phase", "phase": "plan",
                "message": "模型判断这个题材容不下 " + "、".join(dropped)
                           + "，没有硬塞——换个题材再生成一篇能覆盖到它们",
            }

        yield {
            "type": "plan",
            "topic": plan.get("topic", ""),
            "genre": plan.get("genre", ""),
            "title_en": plan.get("title_en", ""),
            "title_zh": plan.get("title_zh", ""),
            "reason": plan.get("reason", ""),
            "paragraphs": len(planned),
        }

        # --- 第二步 + 第三步：逐段生成并当场校验修复 ---
        paragraphs: list[dict] = []
        all_audits: list[dict] = []
        recap = ""
        total_repairs = 0
        total_clue_fixes = 0

        for idx, para_plan in enumerate(planned, 1):
            expected = [w for w in para_plan["words"] if w.strip()]
            yield {
                "type": "phase", "phase": "write",
                "index": idx, "total": len(planned),
                "message": f"第 {idx}/{len(planned)} 段：{'、'.join(expected) or '过渡段'}",
            }

            para = coerce_paragraph(llm.json(
                prompts.write_prompt(
                    plan, idx, len(planned), para_plan, level, n_sent, recap
                ),
                purpose="creative", max_tokens=3000, json_schema=PARAGRAPH_SCHEMA,
            ))

            problems = self.check_paragraph(para, expected, level, allow, names)
            for attempt in range(MAX_REPAIRS):
                if not problems:
                    break
                total_repairs += 1
                yield {
                    "type": "phase", "phase": "repair", "index": idx,
                    "message": f"第 {idx} 段校验未过（{problems[0].kind}），第 {attempt + 1} 次修复",
                }
                para = coerce_paragraph(llm.json(
                    prompts.repair_prompt(
                        idx, para, [p.as_instruction() for p in problems], level
                    ),
                    purpose="structured", max_tokens=3000, json_schema=PARAGRAPH_SCHEMA,
                ))
                problems = self.check_paragraph(para, expected, level, allow, names)

            # --- 语境线索审计：机械校验过了，还要问「读者猜得出来吗」 ---
            audits = []
            if expected:
                yield {
                    "type": "phase", "phase": "audit", "index": idx,
                    "message": f"第 {idx} 段：审查 {len(expected)} 个词的语境线索",
                }
                audits = self.audit_clues(llm, para, expected)
                for attempt in range(MAX_CLUE_FIXES):
                    weak = self.weak_clues(audits)
                    if not weak:
                        break
                    yield {
                        "type": "phase", "phase": "clue_fix", "index": idx,
                        "message": "第 {} 段线索不足：{}，第 {} 次补线索".format(
                            idx,
                            "、".join(f"{a['lemma']}({a.get('strength')})" for a in weak),
                            attempt + 1,
                        ),
                    }
                    issues = [
                        f"- {a['lemma']}：线索强度 {a.get('strength')}。{a.get('why', '')}"
                        for a in weak
                    ]
                    candidate = coerce_paragraph(llm.json(
                        prompts.clue_fix_prompt(para, issues, level),
                        purpose="creative", max_tokens=3000, json_schema=PARAGRAPH_SCHEMA,
                    ))
                    # 补线索不能把机械校验搞坏；坏了就丢弃这次改写
                    if self.check_paragraph(candidate, expected, level, allow, names):
                        yield {
                            "type": "phase", "phase": "clue_fix", "index": idx,
                            "message": f"第 {idx} 段补线索后其他校验回退，已放弃该次改写",
                        }
                        break
                    # 采纳了才计数：被丢弃的那次改写没有落到文章上，
                    # 计进去会让「补了几次线索」这个数字对不上文章的实际内容。
                    total_clue_fixes += 1
                    # 这份 candidate 刚通过了机械校验，problems 随之清空——
                    # 否则文章里会留着补线索之前那一轮的问题标记。
                    para, problems = candidate, []
                    audits = self.audit_clues(llm, para, expected)

            para = self._normalize(para, expected)
            para["problems"] = [p.kind for p in problems]
            para["audits"] = audits
            all_audits.extend(audits)
            paragraphs.append(para)

            tail = " ".join((s.get("en") or "") for s in para.get("sentences", [])[-2:])
            recap = (recap + " " + tail).strip()[-600:]
            yield {"type": "paragraph", "index": idx, "paragraph": para}

        # 形状归一会把掰不回来的内容置空，修复循环也可能一次都没救回来。
        # 到这里还是一片空白，就不能当成功交出去——那会存下一篇 0 词的文章，
        # 用户看到的是「生成完成」，比直接报错更糟。释义调用也不必再花了。
        if not any(s.get("en") for p in paragraphs for s in p.get("sentences", [])):
            raise ValueError(
                "模型没有产出任何可用的正文，多半是持续返回了不符合格式的内容。"
                "去设置页跑一次四层检验可以确认这个模型能不能干这个活。"
            )

        # --- 释义：一次调用拿全部目标词的中文释义，供词条面板使用 ---
        yield {"type": "phase", "phase": "glossary", "message": "生成中文释义"}
        full_text = " ".join(
            (s.get("en") or "") for p in paragraphs for s in p.get("sentences", [])
        )
        glossary = self._glossary(llm, words, full_text)

        doc = {
            "title_en": plan.get("title_en", ""),
            "title_zh": plan.get("title_zh", ""),
            "topic": plan.get("topic", ""),
            "genre": plan.get("genre", ""),
            "paragraphs": paragraphs,
            "glossary": glossary,
        }
        yield {"type": "done", "document": doc, "stats": self._stats(
            doc, words, level, llm, total_repairs, dropped,
            all_audits, total_clue_fixes, names,
        )}

    @staticmethod
    def _glossary(llm: LLM, words: list[str], context: str) -> dict[str, str]:
        """一次调用拿全部目标词的中文释义。失败不影响文章本身。"""
        if not words:
            return {}
        try:
            data = llm.json(
                prompts.glossary_prompt(words, context),
                purpose="structured", max_tokens=2500,
            )
        except Exception:  # noqa: BLE001  释义只是加分项，挂了也不该毁掉整篇文章
            return {}
        out: dict[str, str] = {}
        for item in coerce_glossary(data):
            if not item["lemma"] or not item["zh"]:
                continue
            out[item["lemma"]] = " ".join(filter(None, [
                f"{item['pos']} {item['zh']}".strip(),
                f"（{item['note']}）" if item["note"] else "",
            ]))
        return out

    # --------------------------------------------------------------- 后处理

    @staticmethod
    def _normalize(para: dict, expected: list[str]) -> dict:
        """校正模型给的 targets：surface 要真的出现在句子里。

        不要求模型给字符位置——模型数字符位置经常错位；位置由前端按
        surface 做词边界匹配自己算，稳得多。
        """
        for sent in para.get("sentences", []):
            en = sent.get("en") or ""
            fixed = []
            for tgt in sent.get("targets") or []:
                lemma = (tgt.get("lemma") or "").strip()
                if not lemma:
                    continue
                surface = (tgt.get("surface") or "").strip()
                if not surface or not re.search(
                    rf"\b{re.escape(surface)}\b", en, re.IGNORECASE
                ):
                    surface = _appears(lemma, en) or ""
                if surface:
                    fixed.append({"lemma": lemma.lower(), "surface": surface})
            # 模型漏标的，补上
            marked = {t["lemma"] for t in fixed}
            for word in expected:
                if word.lower() in marked:
                    continue
                found = _appears(word, en)
                if found:
                    fixed.append({"lemma": word.lower(), "surface": found})
            sent["targets"] = fixed
        return para

    @staticmethod
    def _stats(doc: dict, words: list[str], level: str, llm: LLM,
               repairs: int, dropped: list[str],
               audits: list[dict], clue_fixes: int, names: set[str]) -> dict:
        text = " ".join(
            s.get("en", "")
            for p in doc["paragraphs"] for s in p.get("sentences", [])
        )
        n_sentences = sum(len(p.get("sentences", [])) for p in doc["paragraphs"])
        hit = [w for w in words if _appears(w, text)]
        report = cefr.scan(text, level, allow=set(words), names=names)
        strength = {"strong": 0, "weak": 0, "none": 0}
        for a in audits:
            strength[a.get("strength") or "none"] += 1
        return {
            "word_count": len(text.split()),
            "sentence_count": n_sentences,
            "targets_total": len(words),
            "targets_hit": len(hit),
            "targets_missed": [w for w in words if w not in hit],
            # 模型判断塞不进、因而没有硬塞的词。和 targets_missed 不是一回事：
            # 那个是「本该写进去却没写」，这个是「有意没写」。
            "unplaced": dropped,
            "offender_rate": round(report["offender_rate"], 4),
            "offenders": report["offenders"][:15],
            "using_real_cefr": report["using_real_data"],
            "repairs": repairs,
            # 线索审计结果：这是衡量「这篇文章到底能不能帮你记住词」的唯一指标
            "clue_strength": strength,
            "clue_fixes": clue_fixes,
            "audits": audits,
            "llm_calls": llm.usage.calls,
            "tokens": llm.usage.total_tokens,
            "ms": llm.usage.ms,
        }


register(ArticleTask())
