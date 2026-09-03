"""LLM 返回 JSON 的多层兜底解析。

绝不裸调 json.loads()。已知的真实失败模式，以及各自归谁管：

  1. 模型把 JSON 包在 ```json ... ``` 里（最常见）  —— 这里，抠 code fence
  2. JSON 前后带一句「好的，这是你要的结果：」      —— 这里，括号配对截取
  3. max_tokens 不够导致 JSON 从中间被截断          —— 这里，补齐括号救回前半
  4. 尾随逗号                                       —— 这里，正则去掉
  5. DeepSeek 官方承认的偶发空 content              —— 调用方 client.py 重试
  6. 中文引号当成结构引号（{“a”: 1}）              —— 不管

第 6 种是故意不管的：中文译文里本来就大量出现 “ ” 「 」，全局替换会把
本来解析得好好的句子改坏。真遇上了走第 5 种同样的路——重试一次比猜着改安全。

解析出来是不是预期的形状，这里同样不负责，见 tasks/article/schema.py。
"""
from __future__ import annotations

import json
import re
from typing import Any

try:                        # 可选依赖：装了就多一层修复，没装照常跑
    import json_repair as _json_repair
except ImportError:         # pragma: no cover - 取决于装没装
    _json_repair = None

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class JsonParseError(ValueError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def _first_bracket(text: str) -> int:
    """第一个 { 或 [ 的下标，没有则 -1。"""
    return min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)


def _balanced_slice(text: str) -> str | None:
    """从第一个 { 或 [ 起，按括号配对截出完整的一段（跳过字符串内的括号）。"""
    start = _first_bracket(text)
    if start == -1:
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _repair_truncated(fragment: str) -> list[str]:
    """JSON 被 max_tokens 截断时，尽量补齐括号救回前面已完整的部分。

    返回一组候选，「保留得最多」的排在前面，调用方逐个试。给一组而不是一个：
    末尾那半截值该不该丢，光看字符串判不出来，所以两种都给出去让 json 自己裁决。
    本项目的四种 schema 全是字符串和数组，没有一个数字字段，
    所以「优先保留、解析不过再回退」是安全的。

    回退点这一步踩过两个坑，都不报错，只会让这一层安静地失效：

    · **回退点必须落在字符串之外。** 原来是 rfind(",")，找到的往往是正文里的
      逗号——中英对照的正文里逗号遍地都是——回退点于是落在句子中间，
      截出来的 JSON 必然不合法。
    · **回退点和括号栈必须取自同一个位置。** 栈是一路扫到末尾算出来的，
      回退却把字符串截回了更早的地方：被截掉的那几个 } ] 已经在栈里弹过一次，
      再按末尾的栈去补就少补几个，补出来照样不合法。

    两条叠加起来，这一层对本项目真正会产出的形状
    （`{"sentences": [{...}, {"en": "下一句被截断`）**一次都没救回来过**——
    而表现出来只是「解析失败、重试一次」：多烧一次三十秒的调用，没人会发现。
    """
    start = _first_bracket(fragment)
    if start == -1:
        return []
    fragment = fragment[start:]

    in_str = escaped = False
    stack: list[str] = []
    cut, cut_stack = -1, []          # 最后一个落在字符串之外的逗号，及它那一刻的栈
    for i, ch in enumerate(fragment):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
        elif ch == "," and stack:
            cut, cut_stack = i, list(stack)

    if not stack and not in_str:
        return []                    # 根本没被截断，轮不到这一层

    # 回退：连同末尾那个残缺的键值对一起丢掉，并且用**那个逗号处**的栈来收尾
    back = [fragment[:cut] + "".join(reversed(cut_stack))] if cut >= 0 else []
    if in_str:
        # 截在字符串中间，那段文字本身就是残的，优先整个丢掉；
        # 没有可回退的逗号时（`{"en": "半句`）才退而求其次把引号补上。
        return [*back, fragment + '"' + "".join(reversed(stack))]
    return [fragment + "".join(reversed(stack)), *back]


def _hollow(value: Any) -> bool:
    """这份结果里有没有模型真写出来的东西。

    只给第 4 层用。第 4 层只在输入被截断时才跑，所以「补齐之后一个字都没有」
    只有一种可能：截断点落在第一个值出现之前，补出来的那几个括号是这一层
    自己造的。照单收下就等于把「模型什么都没吐出来」变成「成功解析出一个
    空文档」——那正是本模块第 5 层那句「空结果不算修好」要拦的东西
    （见 需要注意.md 第 2 条），两层理应守同一条规矩。

    实际会踩到的三种（都验证过）：
        `{`                -> `{}`
        `{"sentences": [`  -> `{"sentences": []}`
        `{"sentences":[{`  -> `{"sentences": [{}]}`   ← 还凭空多造了一句
    最后一种直接违反本层自己那条「宁可少一句，也不要凭空多一句模型没写的」。

    代价不是「解析失败」这么轻。审计那次调用被截在开头时，拿回来的
    `{"audits": []}` 会被 audit_clues 读成「所有词都没有线索」，于是一段
    本来写得好好的文章要挨两轮补线索改写——白烧钱，还可能把它改坏，
    而界面上只显示一句「第 N 段线索不足」。抛出去让 client.py 重试才是对的。
    """
    if isinstance(value, dict):
        return all(_hollow(v) for v in value.values())
    if isinstance(value, list):
        return all(_hollow(v) for v in value)
    if isinstance(value, str):
        return not value.strip()
    return value is None


def loads(text: str) -> Any:
    """尽最大努力把模型输出解析成 Python 对象。"""
    if not text or not text.strip():
        raise JsonParseError("模型返回了空内容", text or "")

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    for m in _FENCE.finditer(text):          # 第 2 层：抠 code fence
        candidates.append(m.group(1).strip())

    sliced = _balanced_slice(text)            # 第 3 层：括号配对截取
    if sliced:
        candidates.append(sliced)

    for cand in candidates:
        for attempt in (cand, _TRAILING_COMMA.sub(r"\1", cand)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue

    for cand in candidates:                   # 第 4 层：截断补齐
        for patched in _repair_truncated(cand):
            for attempt in (patched, _TRAILING_COMMA.sub(r"\1", patched)):
                try:
                    got = json.loads(attempt)
                except json.JSONDecodeError:
                    continue
                # 补出来是个空壳就不算救回来，换下一个候选（见 _hollow）
                if not _hollow(got):
                    return got

    # 第 5 层：整段语法修复。json_repair 是个按 JSON 文法走的解析器，
    # 能修上面四层修不了的一类东西——它们都在真实输出里出现过：
    #   {“a”: 1}                     结构位置上的中文引号
    #   {"en": "He said "go", ok"}   正文里没转义的引号
    #   {"zh": "第一行\n第二行"}       正文里没转义的换行
    #   {'a': 1} / None / True       单引号与 Python 字面量
    #
    # 本模块顶上那条「中文引号一律不管」的注释，理由是「全局替换会把正文里的
    # “ ” 「 」改坏」——那个理由只对**正则替换**成立。真解析器分得清哪个引号
    # 在结构位置、哪个在字符串里面（实测：正文里的 “ ” 《 》 —— …… 全部原样保留）。
    #
    # 排在第 4 层之后，不是之前：json_repair 修截断的办法是**补默认值**
    # （给缺的字段填空字符串 / null），而本模块第 4 层只丢不补。
    # 「宁可少一句，也不要凭空多一句模型没写的」——先让只丢不补的那层试。
    #
    # 没装也能跑：这一层是加分项，缺了只是少修几种畸形，和 CEFR 词表缺失时
    # 退回内置兜底表是同一个处理方式。
    if _json_repair is not None:
        for cand in candidates:
            # 截断的候选不交给它。json_repair 修截断的办法是**补默认值**，
            # 实测 `{"sentences": [{"e` 会被补成 `{"sentences": [["e"]]}`——
            # 把半个键名编成了一个值。本模块第 4 层只丢不补，宁可少一句，
            # 也不要凭空多一句模型没写的。所以残缺的输入到第 4 层为止。
            if _repair_truncated(cand):
                continue
            try:
                # 它自己出错不该盖住真正的报错，换下一个候选继续
                got = _json_repair.loads(cand)
            except Exception:
                continue
            # 空结果不算修好。json_repair 对「抱歉，我不能完成」这类纯文字
            # 返回的是 ""，对空输入也是 ""——照单收下就等于把「模型拒绝回答」
            # 悄悄变成「成功解析出一个空文档」，那正是这个项目最不能接受的
            # 那种失败（见 需要注意.md 第 2 条）。走到这一层还是空，就报错。
            if got not in ("", None, [], {}):
                return got

    raise JsonParseError("无法从模型输出中解析出 JSON", text[:1000])
