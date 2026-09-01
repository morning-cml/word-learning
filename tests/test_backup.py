"""启动前的数据库快照。

备份本身很容易变成新的故障源，所以这里盯的主要是三件「不该发生的事」：
不能拖垮启动、不能用坏数据覆盖好备份、不能无限占盘。
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from core.store import backup


class _Db:
    """一个能往里写东西的临时 SQLite 文件。

    Path 有 __slots__，挂不上 write 方法，所以包一层；
    其余属性直接代理给 Path，测试里当路径用就行。
    """

    def __init__(self, path):
        self.path = path

    def write(self, n):
        c = sqlite3.connect(self.path)
        c.execute("create table if not exists t(x)")
        c.execute("insert into t values (?)", (n,))
        c.commit()
        c.close()

    def __fspath__(self):
        return str(self.path)

    def __str__(self):
        return str(self.path)

    def __getattr__(self, name):
        return getattr(self.path, name)


@pytest.fixture
def db_file(tmp_path):
    return _Db(tmp_path / "app.db")


def test_库还不存在时什么都不做(tmp_path):
    assert backup.run(tmp_path / "nope.db")["made"] is False


def test_空文件不留档(tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert backup.run(empty)["made"] is False


def test_首次留档(db_file):
    db_file.write(1)
    r = backup.run(db_file.path)
    assert r["made"] and r["count"] == 1 and r["ok"]


def test_库没动过就不重复留档(db_file):
    """一天开十次应用不该攒出十份一模一样的快照。"""
    db_file.write(1)
    backup.run(db_file.path)
    r = backup.run(db_file.path)
    assert not r["made"] and r["count"] == 1


def test_库动过就再留一份(db_file):
    db_file.write(1)
    backup.run(db_file.path)
    db_file.write(2)
    assert backup.run(db_file.path)["made"]


def test_轮换只留最近若干份(db_file):
    """同一秒内连做十几次，专门压文件名撞车：只精确到秒的话
    replace() 会把前一份直接盖掉——轮换看着在跑，实际一份都没多。"""
    for i in range(14):
        db_file.write(i)
        backup.run(db_file.path, keep=5)

    snaps = backup.snapshots(db_file.path)
    assert len(snaps) == 5
    assert len({p.name for p in snaps}) == 5
    #  snapshots() 依赖「逆字典序 == 逆时间序」，文件名格式变了这条会先挂
    assert [p.name for p in snaps] == [
        p.name for p in sorted(snaps, key=lambda x: x.stat().st_mtime, reverse=True)]


def test_快照是完好的库(db_file):
    for i in range(3):
        db_file.write(i)
    backup.run(db_file.path)
    snap = backup.snapshots(db_file.path)[0]

    assert sqlite3.connect(str(snap)).execute("pragma integrity_check").fetchone()[0] == "ok"
    rows = sqlite3.connect(str(snap)).execute("select count(*) from t").fetchone()[0]
    src = sqlite3.connect(str(db_file)).execute("select count(*) from t").fetchone()[0]
    assert rows == src


def test_源库损坏时绝不覆盖好备份(db_file):
    """走 SQLite 自己的 backup API 而不是复制文件，就是为了这一条：
    源库坏了它直接抛，不会产出一个「看着像备份」其实打不开的文件。"""
    db_file.write(1)
    backup.run(db_file.path)
    before = [p.name for p in backup.snapshots(db_file.path)]

    db_file.write_bytes(b"not a sqlite database" * 200)
    t = time.time() + 60
    os.utime(db_file.path, (t, t))                 # 装成「刚被写过」，逼它尝试留档

    r = backup.run(db_file.path)
    assert not r["made"] and not r["ok"] and r["error"]
    assert [p.name for p in backup.snapshots(db_file.path)] == before
    assert not list((db_file.parent / "backups").glob("*.partial"))   # 没有半截残件

    kept = db_file.parent / "backups" / before[0]
    assert sqlite3.connect(str(kept)).execute("select count(*) from t").fetchone()[0] > 0


def test_路径不可用时安静返回而不是抛(tmp_path):
    """备份失败就是没备份，不能连带着让应用起不来。"""
    r = backup.run(tmp_path / "no" / "such" / "dir" / "app.db")
    assert r["made"] is False


def test_init_db_会顺手留档(temp_db):
    """时机是「动 schema 之前」：紧跟着的 _migrate() 会 ALTER TABLE，
    是这条路径上最可能把老库改坏的一步。"""
    from core.store.models import Article

    with temp_db.session() as s:
        s.add(Article(title_en="X"))
    temp_db.init_db()

    state = temp_db.backup_state()
    assert state["ok"] and state["count"] >= 1
