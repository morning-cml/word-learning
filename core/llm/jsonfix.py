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

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


class JsonParseError(ValueError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def _balanced_slice(text: str) -> str | None:
    """从第一个 { 或 [ 起，按括号配对截出完整的一段（跳过字符串内的括号）。"""
    start = min(
        (i for i in (text.find("{"), text.find("[")) if i != -1),
        default=-1,
    )
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


def _repair_truncated(fragment: str) -> str | None:
    """JSON 被 max_tokens 截断时，尽量补齐括号救回前面已完整的部分。"""
    in_str, escaped, stack = False, False, []
    for ch in fragment:
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
        elif ch in "}]" and stack:
            stack.pop()
    if not stack and not in_str:
        return None
    patched = fragment
    if in_str:
        patched += '"'
    # 丢掉最后一个可能残缺的键值对
    cut = max(patched.rfind(","), patched.rfind("{"), patched.rfind("["))
    if cut > 0 and patched[cut] == ",":
        patched = patched[:cut]
    return patched + "".join(reversed(stack))


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
        patched = _repair_truncated(cand)
        if patched:
            try:
                return json.loads(_TRAILING_COMMA.sub(r"\1", patched))
            except json.JSONDecodeError:
                continue

    raise JsonParseError("无法从模型输出中解析出 JSON", text[:1000])
