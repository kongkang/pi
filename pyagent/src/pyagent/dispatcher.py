"""派活：在指定项目目录启动子会话去干具体的事。

这是"编排器"与"聊天机器人"的分界 —— Pi 自己不写任何项目的代码，它把活派到
对应项目目录里的子 Agent。

三种引擎都是一等公民，由下达任务时显式指定：
- pi     ：本仓库的 pi（走 Codex 订阅），默认
- codex  ：用户已有的 codex CLI 会话
- claude ：用户已有的 Claude Code 会话

未显式指定时用 pi 兜底，但会在任务记录里标记 engine_default=True 并在回执里
说明用了哪个，方便事后纠正。
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .pi_rpc import PiSession
from .store import STATE_DONE, STATE_FAILED, Store, Task

ENGINE_PI = "pi"
ENGINE_CODEX = "codex"
ENGINE_CLAUDE = "claude"
ENGINES = (ENGINE_PI, ENGINE_CODEX, ENGINE_CLAUDE)

# 子会话单轮上限
SUBTASK_TIMEOUT = 3600.0


class Dispatcher:
    """把任务派到项目目录，跟踪状态到结束。"""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        ui_handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        on_finish: Callable[[Task], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.ui_handler = ui_handler
        self.on_finish = on_finish
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ── 项目解析 ──

    def resolve_project(self, name_or_path: str) -> Path:
        """把项目名或路径解析成绝对目录，并强制落在项目根内。

        子会话会在该目录里跑工具，所以边界必须在派活前就定死。
        """
        cand = Path(name_or_path).expanduser()
        path = (cand if cand.is_absolute() else self.cfg.projects_root / name_or_path).resolve()
        if not path.is_dir():
            raise ValueError(f"项目目录不存在：{path}")

        root = self.cfg.projects_root.resolve()
        if not (path == root or root in path.parents):
            raise ValueError(f"拒绝：{path} 不在项目根 {root} 之内")
        return path

    # ── 派活 ──

    def dispatch(
        self,
        project: str,
        prompt: str,
        *,
        engine: str | None = None,
        wait: bool = False,
    ) -> Task:
        """创建任务并执行。

        wait=False 只在**常驻进程**里可用（serve 守护进程）。一次性 CLI 进程
        必须用 wait=True —— 后台线程是 daemon，进程退出会被强杀，任务将永远
        停在 running 状态。
        """
        path = self.resolve_project(project)
        engine_default = engine is None
        chosen = (engine or ENGINE_PI).lower()
        if chosen not in ENGINES:
            raise ValueError(f"未知引擎 {chosen!r}，可选：{'/'.join(ENGINES)}")

        task = self.store.create_task(
            project=path.name,
            project_path=str(path),
            prompt=prompt,
            engine=chosen,
            engine_default=engine_default,
        )

        if wait:
            self._run(task)
        else:
            t = threading.Thread(target=self._run, args=(task,), daemon=True)
            with self._lock:
                self._threads[task.id] = t
            t.start()
        return task

    def active_count(self) -> int:
        """在跑的任务数。顺手清掉已结束线程的引用。"""
        with self._lock:
            for tid in [k for k, t in self._threads.items() if not t.is_alive()]:
                self._threads.pop(tid, None)
            return len(self._threads)

    # ── 执行 ──

    def _run(self, task: Task) -> None:
        self.store.mark_running(task.id)
        try:
            if task.engine == ENGINE_PI:
                result = self._run_pi(task)
            elif task.engine == ENGINE_CODEX:
                result = self._run_codex(task)
            else:
                result = self._run_claude(task)
            self.store.finish(task.id, state=STATE_DONE, result=result)
        except Exception as exc:
            self.store.finish(task.id, state=STATE_FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            # 否则常驻进程里 _threads 只增不减
            with self._lock:
                self._threads.pop(task.id, None)
            fresh = self.store.get_task(task.id)
            if fresh and self.on_finish:
                try:
                    self.on_finish(fresh)
                except Exception:
                    pass

    def _run_pi(self, task: Task) -> str:
        """用本仓库的 pi（Codex 订阅）跑子会话。带守卫与内核沙箱。"""
        # 每个项目一个具名会话，便于日后续接与上下文还原
        session_id = f"pi-task-{task.project}"
        with PiSession(
            self.cfg,
            cwd=Path(task.project_path),
            session_id=session_id,
            name=session_id,
            ui_handler=self.ui_handler,
        ) as s:
            self.store.mark_running(task.id, session_ref=session_id)
            stop = threading.Event()

            def pulse() -> None:
                # 心跳让 reap_stale 能区分"还在跑"与"进程已消失"
                while not stop.wait(30.0):
                    self.store.heartbeat(task.id)

            pulse_thread = threading.Thread(target=pulse, daemon=True)
            pulse_thread.start()
            try:
                return s.ask(task.prompt, timeout=SUBTASK_TIMEOUT)
            finally:
                stop.set()
                pulse_thread.join(timeout=5.0)

    def _run_codex(self, task: Task) -> str:
        """用用户已有的 codex CLI 跑。沙箱等级交给 codex 自己的机制。"""
        argv = [
            "codex", "exec",
            "-C", task.project_path,
            "-s", "workspace-write",
            "--skip-git-repo-check",
            task.prompt,
        ]
        return self._run_cli(task, argv, "codex")

    def _run_claude(self, task: Task) -> str:
        """用用户已有的 Claude Code 跑。"""
        argv = ["claude", "-p", task.prompt, "--output-format", "text"]
        return self._run_cli(task, argv, "claude", cwd=task.project_path)

    def _run_cli(
        self, task: Task, argv: list[str], label: str, *, cwd: str | None = None
    ) -> str:
        self.store.log(task_id=task.id, project=task.project, layer="engine",
                       kind=f"{label}_start", detail=" ".join(argv[:4]))
        try:
            out = subprocess.run(
                argv,
                cwd=cwd or task.project_path,
                capture_output=True,
                text=True,
                timeout=SUBTASK_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"找不到 {label} 命令，请确认已安装并在 PATH 中") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{label} 执行超时（{SUBTASK_TIMEOUT}s）") from exc

        if out.returncode != 0:
            tail = (out.stderr or out.stdout or "").strip()[-800:]
            raise RuntimeError(f"{label} 退出码 {out.returncode}：{tail}")
        return (out.stdout or "").strip()
