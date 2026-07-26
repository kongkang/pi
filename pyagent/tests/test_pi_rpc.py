"""pi_rpc 的纯逻辑单测：不启动 pi 子进程，只验证协议与资源管理逻辑。

用 `uv run python -m pytest tests/` 运行（pytest 为可选开发依赖），
或 `uv run python tests/test_pi_rpc.py` 直接跑。
"""

from __future__ import annotations

import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyagent.pi_rpc import (  # noqa: E402
    _CRITICAL_EVENTS,
    _FIRE_AND_FORGET_METHODS,
    PiRpcError,
    PiSession,
    _reject_option_like,
)


def _bare_session(maxsize: int) -> PiSession:
    """构造只带事件队列的 PiSession，不启子进程。"""
    s = PiSession.__new__(PiSession)
    s._events = queue.Queue(maxsize=maxsize)
    s._dropped_events = 0
    return s


def test_critical_events_survive_full_queue() -> None:
    """队列满时关键事件必须保住 —— 丢了 agent_settled 会让 ask() 空等到超时。"""
    s = _bare_session(5)
    # 队首刻意放关键事件，验证不会被"丢队首腾位置"的做法误伤
    s._put_event({"type": "agent_settled"})
    for i in range(4):
        s._put_event({"type": "message_update", "i": i})
    assert s._events.qsize() == 5

    s._put_event({"type": "__closed__"})

    drained = []
    while True:
        try:
            drained.append(s._events.get_nowait())
        except queue.Empty:
            break

    types = [e["type"] for e in drained]
    assert "agent_settled" in types, "队首的关键事件被丢弃"
    assert "__closed__" in types, "新到的关键事件未入队"
    assert all(t in _CRITICAL_EVENTS for t in types), "非关键事件未被淘汰"


def test_noncritical_dropped_without_disturbing_queue() -> None:
    """队列满时非关键事件直接丢弃，不得动已入队内容。"""
    s = _bare_session(2)
    s._put_event({"type": "agent_settled"})
    s._put_event({"type": "turn_end"})
    s._put_event({"type": "message_update"})

    assert s._dropped_events == 1
    assert s._events.qsize() == 2


def test_drain_events_clears_backlog() -> None:
    """ask() 依赖 drain_events 清掉上一轮残留，否则会读到旧 agent_settled 提前返回。"""
    s = _bare_session(10)
    for _ in range(3):
        s._put_event({"type": "agent_settled"})
    assert s.drain_events() == 3
    assert s._events.empty()


def test_termination_survives_lost_closed_event() -> None:
    """队列被关键事件塞满、__closed__ 挤不进去时，events() 仍须能终止。

    终止语义靠 _finished Event，不依赖 __closed__ 事件本身送达。
    """
    import threading

    s = _bare_session(2)
    s._finished = threading.Event()
    # 塞满且全是关键事件 —— 此时 __closed__ 无处可放
    s._put_event({"type": "agent_settled"})
    s._put_event({"type": "extension_ui_request"})
    assert s._events.full()

    s._wake_all_waiters = PiSession._wake_all_waiters.__get__(s)
    s._responses = {}
    s._resp_lock = threading.Lock()
    s._wake_all_waiters()

    # __closed__ 确实没进队列，但 Event 已置位
    assert s._finished.is_set()
    collected = list(s.events())
    types = [e["type"] for e in collected]
    assert "__closed__" not in types, "本用例前提是 __closed__ 未能入队"
    assert len(collected) == 2, "已入队的关键事件应先被消费完"
    # 关键：events() 正常返回而非永久阻塞（能走到这行就说明没挂死）


def test_reject_option_like_blocks_injection() -> None:
    """以 - 开头的值会被 pi 当命令行选项解析，必须拒绝。"""
    for bad in ("--no-session", "-m", "a\nb", "a\x00b"):
        try:
            _reject_option_like("model", bad)
        except PiRpcError:
            continue
        raise AssertionError(f"未拦截危险参数: {bad!r}")

    assert _reject_option_like("model", "openai-codex/gpt-5.5") == "openai-codex/gpt-5.5"


def test_fire_and_forget_whitelist_is_closed() -> None:
    """白名单方向必须是"只有这些不回复"，未知方法一律按 dialog 回 cancelled。"""
    assert "notify" in _FIRE_AND_FORGET_METHODS
    # dialog 类不得出现在 fire-and-forget 白名单里
    for dialog in ("select", "confirm", "input", "editor"):
        assert dialog not in _FIRE_AND_FORGET_METHODS


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"✓ {fn.__name__}")
    print(f"\n{len(tests)} 个测试全部通过")
