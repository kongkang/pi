"""编排器主循环：飞书 ↔ Pi 主对话 ↔ 决策卡片。

第二步范围：把飞书接成主控对话，并打通授信升级闭环。
子 Agent 派活到多项目（第三步）尚未接入，此处先让主对话可用。

并发要点：飞书消息是异步来的，而 pi 会话一次只能处理一轮。所以消息进队列由
单个 worker 串行喂给 pi —— 并发喂同一个会话会让 pi 拒绝（除非显式 steer）。
"""

from __future__ import annotations

import fcntl
import json
import os
import queue
import threading
from pathlib import Path
from typing import Any

from .adapters.base import IncomingMessage
from .adapters.feishu import FeishuAdapter, FeishuConfig
from .config import Config
from .decisions import DecisionBroker
from .pi_rpc import PiRpcError, PiSession


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


# 决策等待上限：超过则按拒绝处理（安全默认）。可用环境变量调整。
DECISION_TIMEOUT = _env_float("PYAGENT_DECISION_TIMEOUT", 600.0)
# 单轮对话上限
TURN_TIMEOUT = _env_float("PYAGENT_TURN_TIMEOUT", 1800.0)
# 待处理消息上界：worker 阻塞在 ask() 时飞书仍可能持续投递，无界会一直堆积
INBOX_MAX = 200


def _feishu_path(cfg: Config) -> Path:
    return cfg.state_dir / "feishu.json"


def _update_feishu_file(cfg: Config, changes: dict[str, Any]) -> None:
    """对 feishu.json 做加锁的读-改-写，并原子替换。

    该文件同时存 app_secret 与 chat_id，且 feishu-setup（另一个进程）与 serve
    都会写它。不加锁的 RMW 会互相覆盖字段。
    """
    path = _feishu_path(cfg)
    lock_path = path.with_name(path.name + ".lock")

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data: dict[str, Any] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text() or "{}")
            except (json.JSONDecodeError, OSError):
                data = {}
            # 无论后面是否改内容，都先把已有文件的权限收紧 —— 否则"内容无变化"
            # 这条早返回路径会让一个 0644 的凭证文件一直宽着
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

        if all(data.get(k) == v for k, v in changes.items()):
            return  # 无变化，不必写盘（权限已在上面收紧）
        data.update(changes)

        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        # os.open 的 mode 只作用于新建；文件已存在且权限偏宽时要显式收紧
        os.chmod(path, 0o600)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def load_feishu_config(cfg: Config) -> FeishuConfig:
    """凭证优先级：环境变量 > 状态目录下的 feishu.json。

    刻意不放进仓库 —— 凭证不进 git。
    """
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    default_chat = os.environ.get("FEISHU_CHAT_ID", "")

    path = _feishu_path(cfg)
    if path.is_file():
        try:
            data = json.loads(path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            # 损坏或读不动：当作未配置，由调用方给出"先跑 feishu-setup"的引导，
            # 而不是抛出堆栈
            data = {}
        app_id = app_id or str(data.get("app_id") or "")
        app_secret = app_secret or str(data.get("app_secret") or "")
        default_chat = default_chat or str(data.get("chat_id") or "")

    return FeishuConfig(app_id, app_secret, default_chat=default_chat)


def save_credentials(cfg: Config, app_id: str, app_secret: str) -> Path:
    """保存应用凭证。供 feishu-setup 使用。"""
    _update_feishu_file(cfg, {"app_id": app_id, "app_secret": app_secret})
    return _feishu_path(cfg)


def save_default_chat(cfg: Config, chat_id: str) -> None:
    """记住主控对话，下次启动直接用（无需用户手填 chat_id）。"""
    _update_feishu_file(cfg, {"chat_id": chat_id})


class Orchestrator:
    """飞书主对话的编排循环。"""

    def __init__(self, cfg: Config, fs_cfg: FeishuConfig, *, model: str = "") -> None:
        self.cfg = cfg
        self.model = model
        self.broker = DecisionBroker()
        self.feishu = FeishuAdapter(fs_cfg, self.broker)
        self._inbox: queue.Queue[IncomingMessage] = queue.Queue(maxsize=INBOX_MAX)
        self._stop = threading.Event()
        self._session: PiSession | None = None
        self._session_lock = threading.Lock()

    # ── 入队（供飞书回调线程调用） ──

    def submit(self, msg: IncomingMessage) -> None:
        """把消息放进待处理队列。队列满时丢弃并告知用户，不静默堆积。"""
        try:
            self._inbox.put_nowait(msg)
        except queue.Full:
            self.feishu.notify(
                "⚠ 我这边积压太多还没处理完，这条先丢了，稍后再说一次。",
                chat=msg.chat,
            )

    # ── 决策升级 ──

    def _on_ui_request(self, req: dict[str, Any]) -> dict[str, Any] | None:
        """pi 要求用户裁决。此函数在 pi_rpc 的独立线程里执行，可以安全阻塞。"""
        # 先在锁内取引用快照：_reset_session 可能在另一线程把它置空
        with self._session_lock:
            session = self._session
        project = session.cwd.name if session is not None else ""
        d = self.broker.open(req, project=project)
        self.feishu.request_decision(d)
        payload = self.broker.wait(d.id, timeout=DECISION_TIMEOUT)
        if payload is None:
            # 超时：把卡片改成已关闭，避免用户之后再点
            self.feishu.close_decision(d)
        self.broker.discard(d.id)
        return payload

    # ── 主对话会话 ──

    def _ensure_session(self, cwd: Path) -> PiSession:
        with self._session_lock:
            if self._session is None:
                s = PiSession(
                    self.cfg,
                    cwd=cwd,
                    model=self.model or None,
                    # 主对话用固定具名会话，重启后能接着聊
                    session_id="pi-main",
                    name="pi-main",
                    ui_handler=self._on_ui_request,
                )
                s.start()
                self._session = s
            return self._session

    def _reset_session(self) -> None:
        with self._session_lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    # ── 消息处理 ──

    def _handle(self, msg: IncomingMessage) -> None:
        if msg.chat:
            save_default_chat(self.cfg, msg.chat)

        text = msg.text.strip()
        if not text:
            return

        # 少量本地命令，不进模型，省额度也更快
        if text in ("/help", "帮助"):
            self.feishu.notify(
                "可用指令：\n"
                "· 直接说话 —— 与 Pi 主对话\n"
                "· /new 重开主对话\n"
                "· /pending 查看待决策项\n"
                "· /help 本帮助",
                chat=msg.chat,
            )
            return
        if text == "/new":
            self._reset_session()
            self.feishu.notify("已重开主对话。", chat=msg.chat)
            return
        if text == "/pending":
            items = self.broker.pending()
            if not items:
                self.feishu.notify("当前没有待决策项。", chat=msg.chat)
            else:
                lines = [f"· {d.describe()}（等待 {int(d.age_seconds)}s）" for d in items]
                self.feishu.notify("待决策：\n" + "\n".join(lines), chat=msg.chat)
            return

        try:
            s = self._ensure_session(self.cfg.repo_root)
            answer = s.ask(text, timeout=TURN_TIMEOUT)
        except PiRpcError as exc:
            # 会话可能已损坏，下次重建
            self._reset_session()
            self.feishu.notify(f"⚠ 出错了：{exc}", chat=msg.chat)
            return

        self.feishu.notify(answer or "（空回复）", chat=msg.chat)

    def _worker(self) -> None:
        """串行消费消息 —— 同一个 pi 会话不能并发喂。"""
        while not self._stop.is_set():
            try:
                msg = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(msg)
            except Exception as exc:  # worker 不能死
                self.feishu.notify(f"⚠ 处理消息时异常：{exc!r}")

    # ── 生命周期 ──

    def run(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()
        # start() 会阻塞在长连接上。走 submit 而非 _inbox.put，以获得满队列保护
        self.feishu.start(self.submit)

    def stop(self) -> None:
        self._stop.set()
        self.broker.cancel_all()
        self._reset_session()
        self.feishu.stop()
