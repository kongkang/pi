"""决策桥与卡片构造的单测：不连飞书，只验证同步↔异步桥接与协议正确性。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyagent import cards  # noqa: E402
from pyagent.adapters.feishu import FeishuAdapter  # noqa: E402
from pyagent.decisions import DecisionBroker  # noqa: E402


def test_blocking_wait_resolved_by_async_callback() -> None:
    """pi 侧阻塞等待，IM 侧异步回调唤醒 —— 整个闭环的核心。"""
    broker = DecisionBroker()
    d = broker.open(
        {"method": "select", "title": "允许写入项目外文件？", "options": ["Allow", "Block"]},
        project="english-game",
    )

    result: dict = {}

    def pi_side() -> None:
        # 模拟 pi_rpc 的 UI 处理线程：阻塞等人回应
        result["payload"] = broker.wait(d.id, timeout=5.0)

    t = threading.Thread(target=pi_side)
    t.start()
    time.sleep(0.1)
    assert d.pending, "此刻应仍在等待"

    # 模拟飞书卡片回调
    assert broker.resolve(d.id, "Allow", by="ou_kongkang") is True
    t.join(timeout=3)

    assert result["payload"] == {"value": "Allow"}, "select 的响应字段应为 value"
    assert not d.pending
    assert d.resolved_by == "ou_kongkang"


def test_timeout_defaults_to_refusal() -> None:
    """超时必须按拒绝处理 —— 宁可卡住也不能默认放行。"""
    broker = DecisionBroker()
    d = broker.open({"method": "select", "title": "危险操作", "options": ["Allow", "Block"]})
    started = time.time()
    payload = broker.wait(d.id, timeout=0.3)
    assert payload is None, "超时必须返回 None（上层转成 cancelled）"
    assert time.time() - started >= 0.3


def test_confirm_maps_to_confirmed_field() -> None:
    """confirm 类的响应字段是 confirmed(bool)，不是 value。"""
    broker = DecisionBroker()
    d = broker.open({"method": "confirm", "title": "清空会话？"})
    broker.resolve(d.id, "true")
    assert broker.wait(d.id, timeout=1) == {"confirmed": True}

    d2 = broker.open({"method": "confirm", "title": "清空会话？"})
    broker.resolve(d2.id, "false")
    assert broker.wait(d2.id, timeout=1) == {"confirmed": False}


def test_unknown_method_falls_back_to_cancel() -> None:
    """未知 dialog 方法不猜语义，一律按取消 —— 与 pi_rpc 的安全默认一致。"""
    broker = DecisionBroker()
    d = broker.open({"method": "some_future_dialog", "title": "?"})
    broker.resolve(d.id, "whatever")
    assert broker.wait(d.id, timeout=1) == {"cancelled": True}


def test_double_click_is_idempotent() -> None:
    """卡片按钮被连点两次时，第二次必须被拒绝，避免覆盖已有结论。"""
    broker = DecisionBroker()
    d = broker.open({"method": "select", "title": "x", "options": ["Allow", "Block"]})
    assert broker.resolve(d.id, "Allow") is True
    assert broker.resolve(d.id, "Block") is False, "重复处理应返回 False"
    assert broker.wait(d.id, timeout=1) == {"value": "Allow"}


def test_cancel_all_releases_waiters() -> None:
    """会话终止时必须清场，否则 pi 侧线程永久挂着。"""
    broker = DecisionBroker()
    ds = [broker.open({"method": "confirm", "title": f"q{i}"}) for i in range(3)]
    assert len(broker.pending()) == 3

    released: list = []

    def waiter(did: str) -> None:
        released.append(broker.wait(did, timeout=5.0))

    threads = [threading.Thread(target=waiter, args=(d.id,)) for d in ds]
    for t in threads:
        t.start()
    time.sleep(0.1)

    assert broker.cancel_all() == 3
    for t in threads:
        t.join(timeout=3)
    assert released == [{"cancelled": True}] * 3
    assert broker.pending() == []


def test_decision_card_carries_id_in_button_value() -> None:
    """卡片回调靠 action.value 找回决策，decision_id 必须带上。"""
    broker = DecisionBroker()
    d = broker.open(
        {"method": "select", "title": "允许 git push？", "options": ["Allow", "Block"]},
        project="dyj-app-v2",
    )
    card = cards.decision_card(d)

    actions = [e for e in card["elements"] if e.get("tag") == "action"]
    assert actions, "卡片必须含 action 区"
    buttons = actions[0]["actions"]
    assert len(buttons) == 2
    for b in buttons:
        assert b["value"]["decision_id"] == d.id, "按钮必须携带 decision_id"
    # 项目名要出现在卡片里 —— 这是"忘了上下文也能看懂"的前提
    assert "dyj-app-v2" in cards.card_message(card)
    # Allow 用主色、Block 用危险色，降低误点
    kinds = {b["text"]["content"]: b["type"] for b in buttons}
    assert kinds["Allow"] == "primary"
    assert kinds["Block"] == "danger"


def test_feishu_text_extraction() -> None:
    """群里 @机器人 会插入 @_user_1 占位符，必须剔除。"""
    ext = FeishuAdapter._extract_text
    assert ext("text", '{"text":"@_user_1 帮我看下进度"}') == "帮我看下进度"
    assert ext("text", '{"text":"直接说话"}') == "直接说话"
    assert ext("post", '{"content":[[{"tag":"text","text":"富文本"}]]}') == "富文本"
    assert ext("image", '{"image_key":"x"}') == ""
    assert ext("text", "not-json") == ""
    assert ext("text", None) == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"✓ {fn.__name__}")
    print(f"\n{len(tests)} 个测试全部通过")
