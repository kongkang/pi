"""决策桥：把 pi 的同步 UI 请求桥接到异步 IM 回调。

问题所在：pi 的 extension_ui_request 是阻塞语义 —— 扩展调 ctx.ui.select() 后
pi 会一直等客户端回一条 extension_ui_response。而飞书是异步的：发卡片 → 用户
某个时刻点按钮 → 回调进来。两种模型必须对接。

做法：每个待决策项一个 threading.Event。pi 侧的处理线程 open() 后 wait() 阻塞；
IM 回调侧 resolve() 置位并唤醒。超时或进程退出时按安全默认（拒绝）返回。

安全默认在这里是硬约束：拿不到明确批准就一律不批准。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# pi 的 dialog 类方法与其响应字段的对应关系（见 pi 的 docs/rpc.md）
_RESPONSE_FIELD = {
    "select": "value",
    "input": "value",
    "editor": "value",
    "confirm": "confirmed",
}


@dataclass
class Decision:
    """一个待用户裁决的请求。"""

    id: str
    method: str
    title: str
    message: str = ""
    options: list[str] = field(default_factory=list)
    # 来源上下文，便于用户忘了这是哪个任务时能看懂
    project: str = ""
    session: str = ""
    created_at: float = field(default_factory=time.time)

    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _payload: dict[str, Any] | None = field(default=None, repr=False)
    resolved_by: str = ""

    @property
    def pending(self) -> bool:
        return not self._event.is_set()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def describe(self) -> str:
        """给用户看的一句话摘要。"""
        where = f"{self.project}" if self.project else "未知项目"
        opts = f"（选项：{' / '.join(self.options)}）" if self.options else ""
        return f"[{where}] {self.title}{opts}"


class DecisionBroker:
    """待决策项的登记与唤醒。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Decision] = {}

    # ── pi 侧 ──

    def open(
        self,
        req: dict[str, Any],
        *,
        project: str = "",
        session: str = "",
    ) -> Decision:
        """登记一个来自 pi 的 UI 请求。"""
        d = Decision(
            id=uuid.uuid4().hex[:10],
            method=str(req.get("method") or "unknown"),
            title=str(req.get("title") or "需要你确认"),
            message=str(req.get("message") or ""),
            options=[str(o) for o in (req.get("options") or [])],
            project=project,
            session=session,
        )
        with self._lock:
            self._items[d.id] = d
        return d

    def wait(self, decision_id: str, timeout: float) -> dict[str, Any] | None:
        """阻塞等待裁决。返回可直接回给 pi 的 payload；未获批准返回 None。

        None 会被 pi_rpc 转成 {"cancelled": True} —— 即安全默认拒绝。
        """
        with self._lock:
            d = self._items.get(decision_id)
        if d is None:
            return None

        if not d._event.wait(timeout):
            return None  # 超时 → 不批准
        return d._payload

    def discard(self, decision_id: str) -> None:
        """请求已无意义（例如会话结束），清理登记。"""
        with self._lock:
            self._items.pop(decision_id, None)

    # ── IM 侧 ──

    def resolve(self, decision_id: str, choice: str, *, by: str = "") -> bool:
        """用户作出选择。choice 是选项文本，或 confirm 的 'true'/'false'。

        返回 False 表示该决策不存在或已被处理（重复点击卡片按钮时会遇到）。
        """
        with self._lock:
            d = self._items.get(decision_id)
            if d is None or d._event.is_set():
                return False
            d._payload = self._build_payload(d, choice)
            d.resolved_by = by
            d._event.set()
            return True

    def cancel(self, decision_id: str, *, by: str = "") -> bool:
        """用户明确驳回。"""
        with self._lock:
            d = self._items.get(decision_id)
            if d is None or d._event.is_set():
                return False
            d._payload = {"cancelled": True}
            d.resolved_by = by
            d._event.set()
            return True

    def cancel_all(self) -> int:
        """会话终止时清场，避免 pi 侧线程一直挂着。"""
        n = 0
        with self._lock:
            for d in self._items.values():
                if not d._event.is_set():
                    d._payload = {"cancelled": True}
                    d._event.set()
                    n += 1
        return n

    # ── 查询 ──

    def pending(self) -> list[Decision]:
        """当前待裁决项，最早的在前。"""
        with self._lock:
            items = [d for d in self._items.values() if d.pending]
        return sorted(items, key=lambda d: d.created_at)

    def get(self, decision_id: str) -> Decision | None:
        with self._lock:
            return self._items.get(decision_id)

    @staticmethod
    def _build_payload(d: Decision, choice: str) -> dict[str, Any]:
        """按 pi 协议构造 extension_ui_response 的字段。"""
        field_name = _RESPONSE_FIELD.get(d.method)
        if field_name is None:
            # 未知 dialog 方法：不猜语义，按取消处理（与 pi_rpc 的安全默认一致）
            return {"cancelled": True}
        if field_name == "confirmed":
            return {"confirmed": choice.strip().lower() in ("true", "1", "yes", "确认", "允许")}
        return {"value": choice}
