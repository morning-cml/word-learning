"""run.bat 的两条结构性约定。

两条都属于「删掉之后一切照常，直到某个用户的路径长得不一样」——
没有别的地方会报出来，所以只能在这里钉住。
"""
from __future__ import annotations

from pathlib import Path

import pytest

RUN_BAT = Path(__file__).resolve().parents[1] / "run.bat"


@pytest.fixture(scope="module")
def raw() -> bytes:
    return RUN_BAT.read_bytes()


def test_行尾是_CRLF(raw):
    """cmd 读 LF 结尾的批处理会把命令截错，表现是「双击闪一下就没了」。"""
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "混进了裸 LF"


def test_切目录之后立刻验一下切成功了没有(raw):
    """`cd` 失败**不会**让批处理停下来，它会接着在原来的目录里跑。

    双击时那通常是 System32，于是后面会往那里建 .venv、找不到
    requirements.txt，报出来的错和真正的原因（路径）毫无关系。
    两种都不报错的失败法：

    · 路径里有感叹号。第 2 行开了 EnableDelayedExpansion，感叹号会被吃掉。
      实测 "danci!xuexi" 下 cd 直接失败，%CD% 还停在 C:\\Windows。
    · 从网络共享运行（\\\\服务器\\共享）。cmd 不支持把 UNC 当工作目录。

    判据取一个必然存在的项目文件，比逐个去猜原因可靠。
    """
    text = raw.decode("utf-8")
    lines = [ln.strip() for ln in text.split("\r\n")]
    i = lines.index('cd /d "%~dp0"')
    # 只看紧跟其后的一小段：守卫必须在 cd 之后、在任何真正干活的命令之前
    tail = lines[i + 1: i + 25]

    guard = next((n for n, ln in enumerate(tail) if ln.startswith("if not exist")), None)
    assert guard is not None, "cd /d 之后没有验证是否真的切过去了"
    assert "requirements.txt" in tail[guard]
    body = " ".join(tail[guard: guard + 12])
    assert "exit /b 1" in body, "验出来不对必须停下，不能接着往下跑"
    assert "pause" in body, "双击的场景里不 pause 就什么都看不到"
