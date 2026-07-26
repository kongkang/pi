"""pi RPC 客户端：以 `pi --mode rpc` 作为 Agent 引擎。

为什么走 RPC 而不是自己写 agent loop：pi 已内置自动压缩（防上下文爆炸）、
自动重试、会话树与 fork、以及 extension UI 请求协议。编排层只需驱动它。

分帧要求（见 pi 的 docs/rpc.md）：RPC 是严格 JSONL，LF 是唯一记录分隔符。
不能使用会把 U+2028/U+2029 当换行的通用行读取器 —— 那两个字符在 JSON
字符串内部是合法的。Python 的 io 按 \\n 切分，此处再显式剥掉尾部 \\r。
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import sandbox
from .config import Config

# 已知的 fire-and-forget UI 方法：这些不需要（也不能）回响应。
# 白名单方向刻意反转 —— 除这些之外的一切 UI 请求都按 dialog 处理并给出默认响应。
# 若 pi 未来新增 dialog 方法，我们会安全地回 cancelled，而不是让 pi 无限等待。
_FIRE_AND_FORGET_METHODS = frozenset(
    {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}
)

# 长跑会话的资源上界：事件队列满时丢弃非关键事件，避免消费不及时导致 OOM
_EVENT_QUEUE_MAX = 10_000
# 这些事件决定控制流，队列满时也不能丢
_CRITICAL_EVENTS = frozenset({"agent_settled", "__closed__", "extension_ui_request"})
# stderr 只保留最近若干行，防止长会话把日志堆在内存里
_STDERR_MAX_LINES = 2_000


class PiRpcError(RuntimeError):
    pass


def _reject_option_like(name: str, value: str) -> str:
    """拒绝以 - 开头的参数值。

    这些值会被拼进 argv。虽然没有 shell 注入（未经 shell），但一个以 `-` 开头的
    值会被 pi 的参数解析器当成选项，从而改变 pi 行为（选项注入）。
    """
    if value.startswith("-"):
        raise PiRpcError(f"{name} 不能以 '-' 开头（会被当作命令行选项）: {value!r}")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise PiRpcError(f"{name} 不能包含换行或空字符: {value!r}")
    return value


class PiSession:
    """一个 pi RPC 子进程 = 一个 Agent 会话，绑定到某个项目工作目录。"""

    def __init__(
        self,
        cfg: Config,
        cwd: Path,
        *,
        model: str | None = None,
        session_id: str | None = None,
        name: str | None = None,
        no_session: bool = False,
        trust_project: bool = False,
        guard: bool = True,
        # 默认开启：编排器要无人看管地自动推进，而实测证明守卫层可被解释器绕过，
        # 只有内核边界拦得住。宁可少数场景需要显式关掉，也不裸奔。
        sandboxed: bool = True,
        sandbox_extra_writable: list[Path] | None = None,
        extra_args: list[str] | None = None,
        ui_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.cwd = Path(cwd)
        self.model = model
        self.session_id = session_id
        self.name = name
        self.no_session = no_session
        # 是否信任目标项目的 .pi/ 本地资源（extensions/skills/settings 等）。
        # 默认 False 并显式传 --no-approve：项目本地 extension 是任意 TS 代码执行通道，
        # 编排器要在几十个项目目录里跑，绝不能依赖全局 defaultProjectTrust 的默认值。
        self.trust_project = trust_project
        # 是否加载授信守卫扩展（tool_call 执行前拦截越界操作）。
        # 注意：守卫是"提醒层"而非安全边界 —— 命令文本黑名单可被解释器绕过，
        # 已实测（rm -rf 被拦后模型改用 python os.remove 成功）。真边界靠 sandboxed。
        self.guard = guard
        # 是否用 macOS 内核沙箱强制写入边界（真正的边界）
        self.sandboxed = sandboxed
        self.sandbox_extra_writable = sandbox_extra_writable or []
        self._profile_path: Path | None = None
        self.extra_args = extra_args or []
        # 授信升级钩子：收到 dialog 类 UI 请求时调用，返回要回传的 payload。
        # 未提供时默认取消（不自动批准危险操作）。
        self.ui_handler = ui_handler

        self._proc: subprocess.Popen[str] | None = None
        self._events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_EVENT_QUEUE_MAX)
        self._dropped_events = 0
        self._responses: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._resp_lock = threading.Lock()
        # UI 请求在独立线程回复，与主线程发命令并发 → stdin 写入必须串行，
        # 否则两条 JSON 会交错破坏协议。close() 也走这把锁，避免写入途中 fd 被关。
        self._write_lock = threading.Lock()
        self._stderr_buf: deque[str] = deque(maxlen=_STDERR_MAX_LINES)
        self._closed = False
        # 会话终止信号。刻意不依赖 _events —— 那是有界队列，理论上可能塞满而
        # 丢掉 __closed__，导致 events() 消费者永久阻塞。Event 让终止语义不可丢。
        self._finished = threading.Event()

    # ── 生命周期 ──

    def _argv(self) -> list[str]:
        if self.cfg.node_bin is None:
            raise PiRpcError("找不到可用的 node，请先安装 node >= 22.19.0")
        if not self.cfg.pi_cli.is_file():
            raise PiRpcError(f"pi 未构建：{self.cfg.pi_cli} 不存在，请在仓库根运行 npm run build")

        argv = [str(self.cfg.node_bin), str(self.cfg.pi_cli), "--mode", "rpc"]
        if self.model:
            argv += ["--model", _reject_option_like("model", self.model)]
        if self.no_session:
            argv.append("--no-session")
        elif self.session_id:
            argv += ["--session-id", _reject_option_like("session_id", self.session_id)]
        if self.name:
            argv += ["--name", _reject_option_like("name", self.name)]
        # 显式表态，不依赖全局 defaultProjectTrust 的当前值
        argv.append("--approve" if self.trust_project else "--no-approve")
        # 守卫用 -e 加载：这类扩展在项目信任解析之前就生效，不会被 --no-approve 挡掉
        if self.guard and self.cfg.guard_extension.is_file():
            argv += ["-e", str(self.cfg.guard_extension)]
        argv += self.extra_args

        if self.sandboxed:
            argv = self._wrap_sandbox(argv)
        return argv

    def _wrap_sandbox(self, argv: list[str]) -> list[str]:
        """套上内核写入边界。沙箱不可用或自检不通过时拒绝启动。

        宁可起不来也不能"以为有边界其实没有" —— 那比没有沙箱更危险。
        """
        if not sandbox.available():
            raise PiRpcError("要求沙箱但本机没有 sandbox-exec")

        state = self.cfg.state_dir / "sandbox"
        state.mkdir(parents=True, exist_ok=True)
        writable = sandbox.writable_paths(self.cwd, extra=self.sandbox_extra_writable)
        profile = state / f"profile-{abs(hash(str(self.cwd))) % 10**10}.sb"
        profile.write_text(sandbox.build_profile(writable))
        self._profile_path = profile

        # 用一个必然在边界外的路径做自检
        ok, detail = sandbox.self_check(profile, Path.home() / ".pyagent-sandbox-selfcheck")
        if not ok:
            raise PiRpcError(f"沙箱自检未通过，拒绝启动：{detail}")

        return sandbox.wrap(argv, profile)

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._argv(),
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.cfg.env(),
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def close(self) -> None:
        if self._proc is None:
            # 未启动就关闭：同样要置位终止信号，否则 events() 消费者会空等
            self._closed = True
            self._finished.set()
            return
        # 先置标志再抢写锁：正在写的 request 会写完，之后的写入直接报可预期错误，
        # 而不是撞上已关闭的 fd 抛 OSError/ValueError
        with self._write_lock:
            self._closed = True
            try:
                if self._proc.stdin and not self._proc.stdin.closed:
                    self._proc.stdin.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        # 兜底唤醒：进程被 kill 时 _read_stdout 可能来不及投递 __closed__
        self._wake_all_waiters()

    def __enter__(self) -> PiSession:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr_buf)

    @property
    def dropped_events(self) -> int:
        """因队列满被丢弃的非关键事件数（可观测性用）。"""
        return self._dropped_events

    # ── 读取与分派 ──

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for raw in self._proc.stdout:
            line = raw[:-1] if raw.endswith("\n") else raw
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # 非协议输出（例如启动期日志），留给 stderr 视图，不打断协议
                continue
            self._dispatch(msg)
        self._wake_all_waiters()

    def _wake_all_waiters(self) -> None:
        """进程结束：唤醒事件消费者与所有挂起的请求。可重复调用。"""
        # 先置 Event：即使下面的 __closed__ 因队列塞满未能入队，消费者仍能退出
        self._finished.set()
        self._put_event({"type": "__closed__"})
        with self._resp_lock:
            pending = list(self._responses.values())
        for q in pending:
            try:
                q.put_nowait({"type": "response", "success": False, "error": "pi 进程已退出"})
            except queue.Full:
                pass

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for raw in self._proc.stderr:
            self._stderr_buf.append(raw)

    def _put_event(self, msg: dict[str, Any]) -> None:
        """入队事件。队列满时牺牲非关键事件，保住控制流事件。

        丢掉 agent_settled 会让 ask() 一直等到超时，所以关键事件必须真的保住 ——
        不能像"丢掉队首再入队"那样盲丢，队首本身可能就是 agent_settled。
        """
        try:
            self._events.put_nowait(msg)
            return
        except queue.Full:
            pass

        if msg.get("type") not in _CRITICAL_EVENTS:
            self._dropped_events += 1
            return

        # 罕见路径（消费者卡住才会到这里）：整理队列，只保留关键事件腾出空间。
        # 本方法只在 reader 单线程调用，故无需额外加锁；消费者并发 get 拿到的
        # 仍是合法事件，最坏情况是短暂取空后被重新填充唤醒。
        kept: list[dict[str, Any]] = []
        while True:
            try:
                kept.append(self._events.get_nowait())
            except queue.Empty:
                break

        for ev in kept:
            if ev.get("type") in _CRITICAL_EVENTS:
                try:
                    self._events.put_nowait(ev)
                except queue.Full:
                    self._dropped_events += 1
            else:
                self._dropped_events += 1

        try:
            self._events.put_nowait(msg)
        except queue.Full:
            self._dropped_events += 1

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == "response":
            rid = msg.get("id")
            if rid:
                with self._resp_lock:
                    q = self._responses.get(str(rid))
                if q is not None:
                    q.put(msg)
                    return
            self._put_event(msg)
            return

        if mtype == "extension_ui_request":
            # 关键：必须另起线程处理。dialog 类请求可能要等人很久（飞书卡片点按钮），
            # 若在读取线程里同步等待，pi 的 stdout 缓冲区会填满并把 pi 卡死。
            threading.Thread(target=self._handle_ui_request, args=(msg,), daemon=True).start()
            self._put_event(msg)
            return

        self._put_event(msg)

    def _handle_ui_request(self, req: dict[str, Any]) -> None:
        """扩展请求用户交互。dialog 类必须回一条 response，否则 pi 会一直阻塞。"""
        if req.get("method") in _FIRE_AND_FORGET_METHODS:
            return

        payload: dict[str, Any] | None = None
        if self.ui_handler is not None:
            try:
                payload = self.ui_handler(req)
            except Exception as exc:  # 处理器自身出错不能让 pi 卡死
                self._stderr_buf.append(f"[pyagent] ui_handler 异常: {exc!r}\n")
                payload = None

        # 安全默认：没有处理器或处理器失败时一律取消，绝不自动批准
        if payload is None:
            payload = {"cancelled": True}

        try:
            self._send_raw({"type": "extension_ui_response", "id": req.get("id"), **payload})
        except PiRpcError as exc:
            # 会话已关闭等情况：记录即可，pi 也随之退出，无需再回
            self._stderr_buf.append(f"[pyagent] 回复 UI 请求失败: {exc}\n")

    # ── 发送 ──

    def _send_raw(self, obj: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise PiRpcError("会话尚未启动")
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._write_lock:
            if self._closed or self._proc.stdin.closed:
                raise PiRpcError("会话已关闭")
            self._proc.stdin.write(line)
            self._proc.stdin.flush()

    def request(self, cmd_type: str, *, timeout: float = 120.0, **fields: Any) -> dict[str, Any]:
        """发送带 id 的命令并阻塞等待对应 response。"""
        rid = uuid.uuid4().hex[:12]
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._resp_lock:
            self._responses[rid] = q
        try:
            self._send_raw({"id": rid, "type": cmd_type, **fields})
            try:
                return q.get(timeout=timeout)
            except queue.Empty as exc:
                raise PiRpcError(f"命令 {cmd_type} 等待响应超时（{timeout}s）") from exc
        finally:
            with self._resp_lock:
                self._responses.pop(rid, None)

    # ── 高层用法 ──

    def events(self) -> Iterator[dict[str, Any]]:
        """消费事件流，直到进程结束。

        终止判据是 `_finished` 与队列排空的组合，不单靠 __closed__ 事件 ——
        后者走有界队列，极端情况下可能被丢弃。
        """
        while True:
            try:
                ev = self._events.get(timeout=0.5)
            except queue.Empty:
                if self._finished.is_set():
                    return
                continue
            if ev.get("type") == "__closed__":
                return
            yield ev

    def drain_events(self) -> int:
        """丢弃当前积压的事件，返回丢弃条数。

        每轮 ask() 之前必须调用：_events 是会话级共享队列，上一轮遗留的
        agent_settled 会让本轮立即误判"已跑完"并返回上一轮的答案。
        """
        n = 0
        while True:
            try:
                self._events.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def ask(self, message: str, *, timeout: float = 900.0) -> str:
        """发一条 prompt，等到 agent_settled，返回最后一条 assistant 文本。

        用 agent_settled 而非 agent_end：后者之后仍可能有自动重试、压缩重试
        或排队消息续跑，只有 settled 才代表真正跑完。
        """
        # 清掉上一轮残留，否则会读到旧的 agent_settled 而提前返回
        self.drain_events()

        resp = self.request("prompt", message=message, timeout=60.0)
        if not resp.get("success"):
            raise PiRpcError(f"prompt 被拒绝: {resp.get('error')}")

        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        try:
            settled = False
            while not deadline.is_set():
                try:
                    ev = self._events.get(timeout=1.0)
                except queue.Empty:
                    # 队列空时顺带检查进程是否已死：__closed__ 走有界队列可能被丢，
                    # 靠 Event 兜底才能立刻失败，而不是白等满整个 timeout
                    if self._finished.is_set():
                        raise PiRpcError(
                            f"pi 进程意外退出。stderr:\n{self.stderr_text[-2000:]}"
                        ) from None
                    continue
                etype = ev.get("type")
                if etype == "__closed__":
                    raise PiRpcError(f"pi 进程意外退出。stderr:\n{self.stderr_text[-2000:]}")
                if etype == "agent_settled":
                    settled = True
                    break
            if not settled:
                raise PiRpcError(f"等待 agent_settled 超时（{timeout}s）")
        finally:
            timer.cancel()

        got = self.request("get_last_assistant_text", timeout=30.0)
        if not got.get("success"):
            raise PiRpcError(f"读取回复失败: {got.get('error')}")
        return (got.get("data") or {}).get("text") or ""

    def state(self) -> dict[str, Any]:
        resp = self.request("get_state", timeout=30.0)
        if not resp.get("success"):
            raise PiRpcError(f"get_state 失败: {resp.get('error')}")
        return resp.get("data") or {}

    def available_models(self) -> list[dict[str, Any]]:
        resp = self.request("get_available_models", timeout=60.0)
        if not resp.get("success"):
            raise PiRpcError(f"get_available_models 失败: {resp.get('error')}")
        return (resp.get("data") or {}).get("models") or []
