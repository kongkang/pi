"""feishu.json 的并发写与权限测试。

该文件同时存 app_secret 与 chat_id，且由两条路径写入：
feishu-setup（独立进程）与 serve 收到首条消息时的 save_default_chat。
不加锁的读-改-写会互相覆盖字段，所以这里专门守住这个行为。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyagent import config as cfgmod  # noqa: E402
from pyagent.orchestrator import (  # noqa: E402
    _feishu_path,
    _update_feishu_file,
    load_feishu_config,
    save_credentials,
    save_default_chat,
)


def _clean(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".lock").unlink(missing_ok=True)
    path.with_name(path.name + ".tmp").unlink(missing_ok=True)


def test_mixed_field_writes_do_not_clobber() -> None:
    """凭证与 chat_id 由不同路径写入，必须共存。"""
    cfg = cfgmod.load()
    path = _feishu_path(cfg)
    _clean(path)
    try:
        save_credentials(cfg, "app_x", "secret_x")
        save_default_chat(cfg, "oc_chat_x")
        data = json.loads(path.read_text())
        assert data == {"app_id": "app_x", "app_secret": "secret_x", "chat_id": "oc_chat_x"}

        # 反序再来一遍，确认与写入顺序无关
        _clean(path)
        save_default_chat(cfg, "oc_chat_y")
        save_credentials(cfg, "app_y", "secret_y")
        data = json.loads(path.read_text())
        assert data["chat_id"] == "oc_chat_y"
        assert data["app_secret"] == "secret_y"
    finally:
        _clean(path)


def test_concurrent_writes_keep_all_fields() -> None:
    """多线程并发写不同字段，最终所有字段都必须在。"""
    cfg = cfgmod.load()
    path = _feishu_path(cfg)
    _clean(path)
    try:
        keys = [f"field_{i}" for i in range(12)]

        def writer(k: str) -> None:
            _update_feishu_file(cfg, {k: k})

        threads = [threading.Thread(target=writer, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        data = json.loads(path.read_text())
        missing = [k for k in keys if data.get(k) != k]
        assert not missing, f"并发写丢失字段: {missing}"
    finally:
        _clean(path)


def test_permission_tightened_even_when_unchanged() -> None:
    """内容无变化时也要收紧权限 —— 早返回路径曾漏掉这步。"""
    cfg = cfgmod.load()
    path = _feishu_path(cfg)
    _clean(path)
    try:
        path.write_text(json.dumps({"app_id": "same", "app_secret": "same"}))
        os.chmod(path, 0o644)
        save_credentials(cfg, "same", "same")  # 内容相同 → 走早返回
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        _clean(path)


def test_corrupt_file_reads_as_unconfigured() -> None:
    """损坏的凭证文件按未配置处理，不能抛堆栈。"""
    cfg = cfgmod.load()
    path = _feishu_path(cfg)
    _clean(path)
    try:
        path.write_text("{ this is not json")
        # 环境变量会覆盖文件，测试时要确保干净
        for var in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_CHAT_ID"):
            os.environ.pop(var, None)
        fs = load_feishu_config(cfg)
        assert not fs.configured
    finally:
        _clean(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"✓ {fn.__name__}")
    print(f"\n{len(tests)} 个测试全部通过")
