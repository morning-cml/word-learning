"""启动时给 data/app.db 留一份快照。

为什么这件事值得单独做：这个应用里**文章是可以重新生成的，学习状态不能**。
掌握程度、累计语境、释义都是一次次阅读攒出来的，丢了没有任何办法补回来——
而 store 又是按「将来的测验 / SRS / 关联图谱模块直接读」设计的，
它的价值随时间只增不减。这类资产的期望损失是阶跃函数：一直为零，直到某次全损。

三条约束，都是为了让备份本身不要变成新的故障源：

  · 绝不阻塞启动。备份失败就是没备份，不能连带着让应用起不来。
  · 绝不用坏数据覆盖好备份。走 SQLite 自己的 backup API 而不是复制文件——
    它会正确处理锁和 WAL，源库损坏时直接抛错，不会产出一个「看着像备份」的文件。
    写入也先落到 .partial 再原子改名，中途断电不会留下半个快照。
  · 绝不无限占盘。只留最近 KEEP 份。

时机是「动 schema 之前」。init_db() 里紧跟着就是 _migrate() 的 ALTER TABLE，
那是这条路径上最可能把老库改坏的一步——要备份的正是它动手之前的状态。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

KEEP = 5


def _dir(db_path: Path) -> Path:
    return db_path.parent / "backups"


def snapshots(db_path: Path) -> list[Path]:
    """已有快照，最新的在前。文件名里带时间戳，所以逆字典序就是逆时间序。"""
    folder = _dir(db_path)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{db_path.stem}-*.db"), reverse=True)


def _snapshot(src: Path, dst: Path) -> None:
    partial = dst.with_name(dst.name + ".partial")
    try:
        source = sqlite3.connect(str(src))
        try:
            # 源库损坏时 backup() 会抛 DatabaseError，此处直接中断，
            # 不会走到下面的 replace——已有的好备份保持不动。
            target = sqlite3.connect(str(partial))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        partial.replace(dst)
    finally:
        partial.unlink(missing_ok=True)


def run(db_path: Path, *, keep: int = KEEP, force: bool = False) -> dict:
    """留一份快照并清掉过期的。任何异常都吞掉，只在返回值里说明。

    force=True 跳过「库没被写过就不重复留档」那条短路。调用方**明知自己
    马上要动库**时必须传它，理由见下面那段注释。
    """
    state: dict = {"ok": True, "made": False, "count": 0, "latest": "", "error": ""}
    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return state                      # 还没有库，没什么可备份的

        existing = snapshots(db_path)
        if not force and existing and existing[0].stat().st_mtime >= db_path.stat().st_mtime:
            # 上次备份之后库没被写过。一天开十次应用不该攒出十份一样的快照。
            #
            # 但这条短路有个反直觉的后果：**它恰好在最该留档的那一次生效**。
            # 「库自上次备份后没被写过」正是「这次启动才要动它」的典型场景——
            # 于是要改数据的那一次反而没有当次快照兜底。
            # 所以准备写库的调用方要传 force=True，见 db._reconcile_counts。
            state["count"] = len(existing)
            state["latest"] = existing[0].name
            return state

        folder = _dir(db_path)
        folder.mkdir(parents=True, exist_ok=True)
        # 精确到毫秒。只到秒的话，同一秒内的两次备份会撞上同一个文件名，
        # replace() 直接把前一份盖掉——轮换看着在跑，实际一份都没多。
        # 毫秒放在末尾且位宽固定，逆字典序仍然等于逆时间序（snapshots 依赖这点）。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        target = folder / f"{db_path.stem}-{stamp}.db"
        _snapshot(db_path, target)

        for stale in snapshots(db_path)[keep:]:
            stale.unlink(missing_ok=True)

        kept = snapshots(db_path)
        state.update(made=True, count=len(kept), latest=target.name)
        return state
    except Exception as exc:            # noqa: BLE001  备份失败绝不能拖垮启动
        state.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return state
