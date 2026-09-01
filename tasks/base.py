"""任务（功能模块）接口。

一个「任务」= 一件能对着词表做的事：生成文章、出题、造例句、拆词根、
画关联图谱……它们结构相同，都是「拼 prompt → 要 JSON → 校验 → 修复 → 落库」。

新增功能的完整流程：
    1. 在 tasks/ 下建一个目录，写一个 Task 子类，实现 run()；
    2. 在 tasks/__init__.py 里 import 一次（模块内调 register 自注册）；
    3. 结束。core/ 与 web/ 一行都不用改。

所有任务共用 core/store 的同一份词汇学习状态——这是「集成」而不是
「几个互不相干的工具装在一个壳里」的关键。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from core.llm.client import LLM

# 生成过程中往前端推的进度事件。run() 直接 yield 出来，
# 调用方按 type 分发——不再另设回调，两套推送机制只会互相打架。
Event = dict[str, Any]


@dataclass
class Problem:
    """校验发现的一个问题，要能直接翻译成给模型的修复指令。"""

    kind: str
    detail: str
    hint: str = ""

    def as_instruction(self) -> str:
        return f"- [{self.kind}] {self.detail}{('　修法：' + self.hint) if self.hint else ''}"


class Task:
    """所有功能模块的基类。"""

    id: str = ""
    name: str = ""
    description: str = ""

    def run(self, llm: LLM, params: dict) -> Iterator[Event]:
        """执行任务，边跑边 yield 进度事件，最后 yield 一个 done 事件。

        校验与修复由各任务在 run() 内部自己组织：什么算「一段合格」
        因任务而异，基类硬定一个 validate/repair 形状只会碍事。
        """
        raise NotImplementedError


_REGISTRY: dict[str, Task] = {}


def register(task: Task) -> Task:
    _REGISTRY[task.id] = task
    return task


def get(task_id: str) -> Task:
    if task_id not in _REGISTRY:
        raise KeyError(f"未注册的任务：{task_id}")
    return _REGISTRY[task_id]


def all_tasks() -> list[Task]:
    return list(_REGISTRY.values())
