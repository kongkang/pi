"""输入/通知适配器抽象：让本地 CLI 与飞书共用同一套编排语义。

编排器不该知道消息是从终端还是飞书来的，也不该知道决策是靠敲键盘还是点卡片
按钮做出的。两侧都实现这里的协议，编排逻辑就只写一遍。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..decisions import Decision


@dataclass
class IncomingMessage:
    """一条来自用户的消息，已抹平各 IM 差异。"""

    text: str
    # 会话线索：飞书用 thread_id/chat_id，CLI 用固定值。编排器据此区分"哪个任务"
    thread: str = ""
    chat: str = ""
    sender: str = ""
    # 原始事件，排障用
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        return self.text.startswith("/")


class Notifier(Protocol):
    """主动向用户推送。编排器用它汇报进度、请求决策。"""

    def notify(self, text: str, *, thread: str = "", chat: str = "") -> None:
        """推送一条普通消息。"""
        ...

    def request_decision(self, decision: Decision, *, thread: str = "", chat: str = "") -> None:
        """把待决策项呈现给用户。

        必须是非阻塞的 —— 呈现完就返回，实际等待由 DecisionBroker.wait() 负责。
        这样 pi 侧线程等在 Event 上，IM 侧不被占用。
        """
        ...

    def close_decision(self, decision: Decision) -> None:
        """决策已有结果（或已失效），更新呈现，避免用户对着过期卡片点。"""
        ...


class InputAdapter(Protocol):
    """接收用户消息。"""

    def start(self, on_message: Callable[[IncomingMessage], None]) -> None:
        """开始接收。可能阻塞（如 WebSocket 长连接），由调用方决定是否另起线程。"""
        ...

    def stop(self) -> None: ...
