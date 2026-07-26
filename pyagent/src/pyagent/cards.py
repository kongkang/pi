"""飞书卡片构造。

决策卡片是整个授信闭环的门面：pi 想执行危险操作时，你在手机上看到的就是这张卡。
所以它必须自解释 —— 光看卡片就知道"哪个项目、要干什么、为什么问我"，
不需要回头翻上下文。

按钮的 value 字段携带 decision_id，卡片回调靠它找回对应的待决策项。
"""

from __future__ import annotations

import json
from typing import Any

from .decisions import Decision

# 飞书卡片按钮配色
_PRIMARY = "primary"
_DANGER = "danger"
_DEFAULT = "default"

# 看起来像"批准"的选项用主色，像"拒绝"的用危险色，其余默认
_APPROVE_WORDS = ("allow", "approve", "yes", "确认", "允许", "同意", "批准", "继续")
_DENY_WORDS = ("block", "deny", "no", "拒绝", "取消", "禁止", "停止")


def _button_type(label: str) -> str:
    low = label.strip().lower()
    if any(w in low for w in _APPROVE_WORDS):
        return _PRIMARY
    if any(w in low for w in _DENY_WORDS):
        return _DANGER
    return _DEFAULT


def _button(label: str, decision_id: str, choice: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": _button_type(label),
        # 回调时通过 action.value 拿回这些字段
        "value": {"decision_id": decision_id, "choice": choice},
    }


def decision_card(d: Decision) -> dict[str, Any]:
    """构造待决策卡片。"""
    lines = []
    if d.project:
        lines.append(f"**项目**：{d.project}")
    if d.message:
        lines.append(d.message)
    if not lines:
        lines.append("_（该请求未附带更多说明）_")

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
    ]

    if d.method == "confirm":
        actions = [_button("确认", d.id, "true"), _button("拒绝", d.id, "false")]
    elif d.options:
        actions = [_button(opt, d.id, opt) for opt in d.options]
    else:
        # input/editor 类：卡片按钮表达不了自由文本，引导用户直接回消息
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"请直接回复内容，或回复 `/deny {d.id}` 拒绝。",
                },
            }
        )
        actions = [_button("拒绝", d.id, "__cancel__")]

    elements.append({"tag": "hr"})
    elements.append({"tag": "action", "actions": actions})
    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"决策 {d.id} · 超时未回应将按拒绝处理"}
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"⚠ {d.title}"},
        },
        "elements": elements,
    }


def resolved_card(d: Decision, outcome: str) -> dict[str, Any]:
    """决策已处理后替换原卡片，避免用户对着过期按钮点。"""
    lines = []
    if d.project:
        lines.append(f"**项目**：{d.project}")
    lines.append(f"**结果**：{outcome}")
    if d.resolved_by:
        lines.append(f"**处理人**：{d.resolved_by}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "grey",
            "title": {"tag": "plain_text", "content": f"✓ {d.title}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"决策 {d.id} 已关闭"}],
            },
        ],
    }


def text_message(content: str) -> str:
    """飞书文本消息的 content 字段要求是 JSON 字符串。"""
    return json.dumps({"text": content}, ensure_ascii=False)


def card_message(card: dict[str, Any]) -> str:
    """飞书交互卡片的 content 字段同样是 JSON 字符串。"""
    return json.dumps(card, ensure_ascii=False)
