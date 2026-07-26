"""飞书适配器：WebSocket 长连接，免公网 IP / 域名 / 内网穿透。

为什么用长连接：本机主动连飞书服务器接收事件，不需要飞书回调到公网地址。
编排器跑在你自己的 Mac 上，这是唯一可行的方式。

两条入向通道：
- 消息事件（register_p2_im_message_receive_v1）：你说的话
- 卡片回调（register_p2_card_action_trigger）：你点的决策按钮
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from .. import cards
from ..decisions import Decision, DecisionBroker
from .base import IncomingMessage


class FeishuConfig:
    """飞书应用凭证。从环境变量或配置文件读取，不落在代码里。"""

    def __init__(self, app_id: str, app_secret: str, *, default_chat: str = "") -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        # 主控对话的 chat_id：编排器主动汇报进度时发到这里
        self.default_chat = default_chat

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)


class FeishuAdapter:
    """同时充当 InputAdapter 与 Notifier。"""

    def __init__(self, cfg: FeishuConfig, broker: DecisionBroker) -> None:
        self.cfg = cfg
        self.broker = broker
        self._api = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()
        self._ws: lark.ws.Client | None = None
        self._on_message: Callable[[IncomingMessage], None] | None = None
        # decision_id → 卡片所在 message_id，用于决策落定后更新卡片
        self._decision_msgs: dict[str, str] = {}
        self._lock = threading.Lock()
        # 去重：飞书事件可能重投，同一 message_id 只处理一次
        self._seen_messages: set[str] = set()

    # ── 入向 ──

    def start(self, on_message: Callable[[IncomingMessage], None]) -> None:
        """建立长连接并阻塞接收。调用方通常放到独立线程。"""
        self._on_message = on_message
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .register_p2_card_action_trigger(self._handle_card_action)
            .build()
        )
        self._ws = lark.ws.Client(
            self.cfg.app_id,
            self.cfg.app_secret,
            event_handler=handler,
            auto_reconnect=True,
        )
        self._ws.start()

    def stop(self) -> None:
        # lark 的 ws Client 没有公开的 stop；进程退出即断开。
        # 这里只做本地清场，避免 pi 侧线程挂着等一个永远不会来的决策。
        self.broker.cancel_all()

    def _handle_message(self, data: lark.im.v1.P2ImMessageReceiveV1) -> None:  # type: ignore[name-defined]
        msg = data.event.message
        if msg is None:
            return

        mid = msg.message_id or ""
        with self._lock:
            if mid and mid in self._seen_messages:
                return  # 重投，忽略
            if mid:
                self._seen_messages.add(mid)
                # 防止无界增长
                if len(self._seen_messages) > 5000:
                    self._seen_messages.clear()
                    self._seen_messages.add(mid)

        text = self._extract_text(msg.message_type, msg.content)
        if not text:
            return  # 图片/文件等暂不处理

        sender = ""
        if data.event.sender and data.event.sender.sender_id:
            sender = data.event.sender.sender_id.open_id or ""

        incoming = IncomingMessage(
            text=text,
            thread=msg.thread_id or msg.root_id or "",
            chat=msg.chat_id or "",
            sender=sender,
            raw={"message_id": mid, "message_type": msg.message_type},
        )
        if self._on_message:
            self._on_message(incoming)

    @staticmethod
    def _extract_text(message_type: str | None, content: str | None) -> str:
        """从飞书消息体里取纯文本。content 是 JSON 字符串。"""
        if not content:
            return ""
        try:
            body = json.loads(content)
        except json.JSONDecodeError:
            return ""
        if message_type == "text":
            raw = str(body.get("text") or "")
            # 群里 @机器人 会带 @_user_1 占位符，去掉
            return " ".join(p for p in raw.split() if not p.startswith("@_user_")).strip()
        if message_type == "post":
            # 富文本：拼接所有 text 段
            parts: list[str] = []
            for para in body.get("content") or []:
                for node in para or []:
                    if isinstance(node, dict) and node.get("tag") == "text":
                        parts.append(str(node.get("text") or ""))
            return "".join(parts).strip()
        return ""

    def _handle_card_action(
        self, data: lark.CardActionTrigger  # type: ignore[name-defined]
    ) -> object:
        """处理决策卡片按钮点击。返回值会被飞书用来更新卡片/弹 toast。"""
        action = data.event.action
        value = (action.value if action else None) or {}
        decision_id = str(value.get("decision_id") or "")
        choice = str(value.get("choice") or "")

        operator = ""
        if data.event.operator:
            operator = data.event.operator.open_id or ""

        if not decision_id:
            return self._toast("无法识别的按钮", "error")

        d = self.broker.get(decision_id)
        if d is None:
            return self._toast("该决策已失效", "warning")

        if choice == "__cancel__":
            ok = self.broker.cancel(decision_id, by=operator)
            outcome = "已拒绝"
        else:
            ok = self.broker.resolve(decision_id, choice, by=operator)
            outcome = f"已选择：{choice}"

        if not ok:
            return self._toast("该决策已处理过了", "info")

        # 用返回值直接替换卡片，避免用户对着过期按钮再点
        return {
            "toast": {"type": "success", "content": outcome},
            "card": {
                "type": "raw",
                "data": cards.resolved_card(d, outcome),
            },
        }

    @staticmethod
    def _toast(content: str, kind: str) -> dict[str, object]:
        return {"toast": {"type": kind, "content": content}}

    # ── 出向（Notifier 协议） ──

    def notify(self, text: str, *, thread: str = "", chat: str = "") -> None:
        target = chat or self.cfg.default_chat
        if not target:
            return
        self._send(target, "text", cards.text_message(text))

    def request_decision(self, decision: Decision, *, thread: str = "", chat: str = "") -> None:
        """发出决策卡片。非阻塞 —— 发完即返回，等待由 broker.wait() 承担。"""
        target = chat or self.cfg.default_chat
        if not target:
            return
        msg_id = self._send(target, "interactive", cards.card_message(cards.decision_card(decision)))
        if msg_id:
            with self._lock:
                self._decision_msgs[decision.id] = msg_id

    def close_decision(self, decision: Decision) -> None:
        """决策超时或会话结束时，把卡片改成已关闭态。"""
        with self._lock:
            msg_id = self._decision_msgs.pop(decision.id, None)
        if not msg_id:
            return
        outcome = "已超时（按拒绝处理）" if decision.pending else "已处理"
        req = (
            PatchMessageRequest.builder()
            .message_id(msg_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(cards.card_message(cards.resolved_card(decision, outcome)))
                .build()
            )
            .build()
        )
        self._api.im.v1.message.patch(req)

    def _send(self, receive_id: str, msg_type: str, content: str) -> str:
        """发消息，返回 message_id（失败返回空串）。"""
        # receive_id_type 按 id 前缀推断：oc_ 是群，ou_ 是用户
        id_type = "chat_id" if receive_id.startswith("oc_") else "open_id"
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        resp = self._api.im.v1.message.create(req)
        if not resp.success():
            lark.logger.error(f"飞书发消息失败 code={resp.code} msg={resp.msg}")
            return ""
        return (resp.data.message_id if resp.data else "") or ""
