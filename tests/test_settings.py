"""本地设置：Key 的读写、掩码回传、以及落盘的原子性。

这个文件此前是空白。settings.local.json 和 data/app.db 是本机仅有的两份
不可再生状态——一份是 API Key，一份是学习状态——而只有后者有测试盯着。
"""
from __future__ import annotations

import json

import pytest

from core import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    """把设置文件重定向到临时目录。模块级常量，只能 monkeypatch。"""
    path = tmp_path / "settings.local.json"
    monkeypatch.setattr(settings, "SETTINGS_PATH", path)
    # 环境变量兜底会让「删掉 key」这类断言测的是别的东西
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    return path


# --------------------------------------------------------------- Key 的进出

def test_掩码原样回传时不覆盖真_key(store):
    """前端拿到的永远是掩码串，用户不动那一栏直接保存时会把它回传。

    判据是「含 *」而不是「以 * 开头」——mask() 产出的是 sk-ab********wxyz，
    开头恰恰不是 *，按开头判等于这道防线在它唯一该起作用的时候不起作用。
    """
    settings.save({"keys": {"deepseek": "sk-real-key-0123456789"}})
    masked = settings.mask(settings.api_key("deepseek"))
    assert "*" in masked

    settings.save({"keys": {"deepseek": masked}})
    assert settings.api_key("deepseek") == "sk-real-key-0123456789"


def test_传空才是删除(store):
    settings.save({"keys": {"deepseek": "sk-x"}})
    settings.save({"keys": {"deepseek": "   "}})          # 全空格 = 清除
    assert settings.api_key("deepseek") == ""


def test_key_绝不下发到前端(store):
    settings.save({"keys": {"deepseek": "sk-real-key-0123456789"}})
    blob = json.dumps(settings.public_view(), ensure_ascii=False)
    assert "sk-real-key-0123456789" not in blob
    assert "********" in blob


# ------------------------------------------------------------------ 落盘

def test_写设置是原子的(store, monkeypatch):
    """写到一半失败时，原来那份必须原封不动。

    原来是直接 write_text——「先把文件截断，再往里写」。断电 / 进程被杀 /
    磁盘满卡在这两步之间，留下的就是半个 JSON，而 _read() 会把
    JSONDecodeError 吞掉、安静地退回 DEFAULTS：**API Key 就此消失**。
    更糟的是下一步：用户随手再存一次设置，那份不带 key 的 DEFAULTS
    就会被完整地写上去，连半个文件这条线索也没了。
    """
    settings.save({"active_model": "m1", "keys": {"deepseek": "sk-the-only-copy"}})
    before = store.read_bytes()

    # 模拟「内容都写好了，改名这一步挂了」。
    # 用 context() 限定作用域：直接 undo() 会把 fixture 里那个
    # SETTINGS_PATH 的重定向一起撤掉，接下来读的就是用户真正的设置文件了。
    with monkeypatch.context() as m:
        m.setattr("pathlib.Path.replace", lambda self, target: (_ for _ in ()).throw(OSError("磁盘满了")))
        with pytest.raises(OSError):
            settings.save({"keys": {"deepseek": "sk-new"}})

    assert store.read_bytes() == before, "写失败不该动到原来那份"
    assert settings.api_key("deepseek") == "sk-the-only-copy"
    assert settings.load()["active_model"] == "m1"


def test_不留下临时文件(store):
    settings.save({"keys": {"deepseek": "sk-x"}})
    leftovers = list(store.parent.glob("*.partial"))
    assert leftovers == [], f"中间文件没清干净：{leftovers}"


def test_半个文件不会被当成合法设置读进来(store):
    """守的是「读」这一侧：真出现了残缺文件，也得能看出来不对劲。

    这条不断言能救回 key——救不回来。它断言的是 _read() 不会抛，
    以免一个坏文件把整个应用卡在启动上。
    """
    settings.save({"keys": {"deepseek": "sk-x"}})
    raw = store.read_text(encoding="utf-8")
    store.write_text(raw[: len(raw) // 2], encoding="utf-8")

    data = settings.load()                    # 不能抛
    assert data["keys"] == {}


# ------------------------------------------------------------------ 解析

def test_指定了别家就不要拿本家的模型名当默认值(store):
    """已保存的 active_model 只对已保存的那一家有效。

    调用方指定了别家时还拿它当默认值，就会把 A 家的模型名发给 B 家的端点，
    报回来的是一个「模型不存在」，很难看出根因在这。
    """
    settings.save({"active_provider": "deepseek", "active_model": "deepseek-v4-flash"})
    assert settings.active() == ("deepseek", "deepseek-v4-flash")
    assert settings.active("deepseek") == ("deepseek", "deepseek-v4-flash")

    pid, model = settings.active("kimi")
    assert pid == "kimi"
    assert model != "deepseek-v4-flash", "不能把 deepseek 的模型名发给 kimi"
