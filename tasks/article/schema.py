"""结构化输出的 JSON Schema。

Kimi 支持 Structured Output（json_schema），传进去能让模型在解码层就守住格式；
DeepSeek 只有 json_object，传了也不生效——所以两边都必须靠 validate() 兜底，
schema 只是「有就更省事」的加分项，绝不是保证。
"""
from __future__ import annotations

from typing import Any

PLAN_SCHEMA = {
    "name": "article_plan",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "topic": {"type": "string"},
            "genre": {"type": "string"},
            "title_en": {"type": "string"},
            "title_zh": {"type": "string"},
            "reason": {"type": "string"},
            "paragraphs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "focus": {"type": "string"},
                        "words": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["focus", "words"],
                },
            },
            "unplaced": {"type": "array", "items": {"type": "string"}},
            "names": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["topic", "genre", "title_en", "title_zh", "paragraphs"],
    },
}

AUDIT_SCHEMA = {
    "name": "clue_audit",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "audits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "lemma": {"type": "string"},
                        "strength": {"type": "string", "enum": ["strong", "weak", "none"]},
                        "clue": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["lemma", "strength"],
                },
            }
        },
        "required": ["audits"],
    },
}

PARAGRAPH_SCHEMA = {
    "name": "article_paragraph",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "sentences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "en": {"type": "string"},
                        "zh": {"type": "string"},
                        "targets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "lemma": {"type": "string"},
                                    "surface": {"type": "string"},
                                },
                                "required": ["lemma", "surface"],
                            },
                        },
                    },
                    "required": ["en", "zh"],
                },
            }
        },
        "required": ["sentences"],
    },
}


# ---------------------------------------------------------------------------
# 形状归一
#
# 上面这些 schema 只有 Kimi 会真的执行，DeepSeek 收下也不生效。所以模型完全
# 可能回一个「合法但形状不对」的 json：顶层是数组、paragraphs 里躺着字符串、
# words 是 null、strength 是数字……jsonfix 只保证「能解析成 Python 对象」，
# 保证不了「解析出来是这个形状」。
#
# 归一必须紧贴每次调用做掉。形状错误一路带到下游，抛出来的是 AttributeError，
# 而那时整篇文章已经花掉几分钟和上万 token —— 连同已经写好的段落一起作废。
# 掰不回来的部分置空，交给后面的「校验 → 修复」循环去要一次重写：
# 空段落本来就会被 check_paragraph 判成 empty，走的是已有的路。
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    """非字符串一律当空。模型偶尔把 title_en 写成对象，直接入库会污染 String 列。"""
    return value.strip() if isinstance(value, str) else ""


def _texts(value: Any) -> list[str]:
    return [t for t in (_text(x) for x in value) if t] if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _object(value: Any, key: str) -> dict:
    """顶层给成数组时，包回它本该在的那个键下面；其余非 dict 当空。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {key: value}
    return {}


def coerce_plan(value: Any) -> dict:
    """选题结果 → {topic, genre, title_en, title_zh, reason, paragraphs, unplaced}。"""
    plan = _object(value, "paragraphs")
    return {
        "topic": _text(plan.get("topic")),
        "genre": _text(plan.get("genre")),
        "title_en": _text(plan.get("title_en")),
        "title_zh": _text(plan.get("title_zh")),
        "reason": _text(plan.get("reason")),
        "paragraphs": [
            {"focus": _text(p.get("focus")), "words": _texts(p.get("words"))}
            for p in _dicts(plan.get("paragraphs"))
        ],
        "unplaced": _texts(plan.get("unplaced")),
        "names": _texts(plan.get("names")),
    }


def coerce_paragraph(value: Any) -> dict:
    """段落 → {"sentences": [{en, zh, targets: [{lemma, surface}]}]}。

    只保留这三个键：多余的键会被 repair_prompt 原样 dump 回去喂给模型，
    等于教它下次照抄一遍垃圾。
    """
    sentences = []
    for sent in _dicts(_object(value, "sentences").get("sentences")):
        targets = [
            {"lemma": _text(t.get("lemma")), "surface": _text(t.get("surface"))}
            for t in _dicts(sent.get("targets"))
            if _text(t.get("lemma"))
        ]
        sentences.append({
            "en": _text(sent.get("en")),
            "zh": _text(sent.get("zh")),
            "targets": targets,
        })
    return {"sentences": sentences}


def coerce_audits(value: Any) -> list[dict]:
    """线索审计 → [{lemma, strength, clue, why}]。

    strength 只认三个值。认不出来一律按 none 处理 —— 审计判不出结论的词
    绝不能默认放行，那正好绕开这个管线里最该拦住东西的一关。
    """
    out = []
    for audit in _dicts(_object(value, "audits").get("audits")):
        lemma = _text(audit.get("lemma"))
        if not lemma:
            continue
        strength = _text(audit.get("strength")).lower()
        out.append({
            "lemma": lemma,
            "strength": strength if strength in ("strong", "weak", "none") else "none",
            "clue": _text(audit.get("clue")),
            "why": _text(audit.get("why")),
        })
    return out


def coerce_glossary(value: Any) -> list[dict]:
    """释义 → [{lemma, pos, zh, note}]。"""
    return [
        {
            "lemma": _text(item.get("lemma")).lower(),
            "pos": _text(item.get("pos")),
            "zh": _text(item.get("zh")),
            "note": _text(item.get("note")),
        }
        for item in _dicts(_object(value, "glossary").get("glossary"))
    ]
