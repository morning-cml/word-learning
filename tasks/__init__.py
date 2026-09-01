"""任务注册表。新增功能模块时在这里 import 一次即可。"""
from tasks.base import Task, all_tasks, get, register  # noqa: F401
from tasks.article import task as _article  # noqa: F401  注册 article 任务
