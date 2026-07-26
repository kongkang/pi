"""编排状态库（SQLite）。

存什么：派出去的任务、子会话映射、事件流。这是"我所有项目现在到哪一步了"
这个问题的数据来源，也是 Pi 主进程重启后能接上的依据。

为什么用 SQLite 而不是 JSON：多进程共享（serve 守护进程 + 手工 CLI 命令可能
同时读写），需要事务与 WAL。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 任务状态机：pending → running → done | failed | timeout | blocked | cancelled
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_TIMEOUT = "timeout"
STATE_BLOCKED = "blocked"  # 等用户决策
STATE_CANCELLED = "cancelled"

# 用固定 tuple 而非 set：查询里要按同一顺序绑定占位符
TERMINAL_STATES = (STATE_DONE, STATE_FAILED, STATE_TIMEOUT, STATE_CANCELLED)

# 每个版本是一组语句。刻意不用 executescript —— 它会隐式提交当前事务，
# 让外层的 BEGIN/COMMIT 失效（表现为 "cannot rollback - no transaction is active"）。
_MIGRATIONS: list[list[str]] = [
    # v1
    [
        """
    CREATE TABLE IF NOT EXISTS tasks (
        id              TEXT PRIMARY KEY,
        project         TEXT NOT NULL,
        project_path    TEXT NOT NULL,
        prompt          TEXT NOT NULL,
        engine          TEXT NOT NULL,
        engine_default  INTEGER NOT NULL DEFAULT 0,
        state           TEXT NOT NULL,
        session_ref     TEXT,
        result          TEXT,
        error           TEXT,
        created_at      REAL NOT NULL,
        started_at      REAL,
        finished_at     REAL,
        heartbeat_at    REAL,
        attempts        INTEGER NOT NULL DEFAULT 0
    )""",
        "CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project)",
        """
    CREATE TABLE IF NOT EXISTS events (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id   TEXT,
        project   TEXT,
        layer     TEXT NOT NULL,
        kind      TEXT NOT NULL,
        severity  TEXT NOT NULL DEFAULT 'info',
        detail    TEXT,
        at        REAL NOT NULL
    )""",
        "CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_at ON events(at)",
    ],
]


@dataclass
class Task:
    id: str
    project: str
    project_path: str
    prompt: str
    engine: str
    engine_default: bool
    state: str
    session_ref: str | None = None
    result: str | None = None
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    heartbeat_at: float | None = None
    attempts: int = 0

    @property
    def running_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def summary(self) -> str:
        one_line = " ".join(self.prompt.split())
        return one_line[:80] + ("…" if len(one_line) > 80 else "")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL)")
            row = c.execute("SELECT version FROM schema_meta").fetchone()
            current = int(row["version"]) if row else 0
            if current == 0:
                c.execute("INSERT INTO schema_meta (version) VALUES (0)")

            for idx in range(current, len(_MIGRATIONS)):
                # 每个迁移在单个事务内完成，失败即回滚，不留半套结构
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _MIGRATIONS[idx]:
                        c.execute(stmt)
                    c.execute("UPDATE schema_meta SET version = ?", (idx + 1,))
                    c.execute("COMMIT")
                except Exception:
                    if c.in_transaction:
                        c.execute("ROLLBACK")
                    raise

    # ── 任务 ──

    def create_task(
        self,
        *,
        project: str,
        project_path: str,
        prompt: str,
        engine: str,
        engine_default: bool = False,
    ) -> Task:
        t = Task(
            id=uuid.uuid4().hex[:10],
            project=project,
            project_path=project_path,
            prompt=prompt,
            engine=engine,
            engine_default=engine_default,
            state=STATE_PENDING,
            created_at=time.time(),
        )
        with self._conn() as c:
            c.execute(
                """INSERT INTO tasks
                   (id, project, project_path, prompt, engine, engine_default, state, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (t.id, t.project, t.project_path, t.prompt, t.engine,
                 int(t.engine_default), t.state, t.created_at),
            )
        self.log(task_id=t.id, project=project, layer="orchestrator", kind="task_created",
                 detail=f"engine={engine}{' (默认)' if engine_default else ''}")
        return t

    def mark_running(self, task_id: str, session_ref: str | None = None) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """UPDATE tasks SET state=?, started_at=COALESCE(started_at,?),
                   heartbeat_at=?, session_ref=COALESCE(?, session_ref),
                   attempts=attempts+1 WHERE id=?""",
                (STATE_RUNNING, now, now, session_ref, task_id),
            )

    def heartbeat(self, task_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE tasks SET heartbeat_at=? WHERE id=?", (time.time(), task_id))

    def finish(
        self, task_id: str, *, state: str, result: str | None = None, error: str | None = None
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tasks SET state=?, result=?, error=?, finished_at=? WHERE id=?",
                (state, result, error, time.time(), task_id),
            )
        self.log(task_id=task_id, layer="orchestrator", kind=f"task_{state}",
                 severity="info" if state == STATE_DONE else "warning",
                 detail=(error or "")[:500] or None)

    def set_state(self, task_id: str, state: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))

    def get_task(self, task_id: str) -> Task | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._to_task(row) if row else None

    def tasks(
        self, *, project: str | None = None, active_only: bool = False, limit: int = 50
    ) -> list[Task]:
        sql = "SELECT * FROM tasks"
        clauses: list[str] = []
        args: list[Any] = []
        if project:
            clauses.append("project = ?")
            args.append(project)
        if active_only:
            placeholders = ",".join("?" * len(TERMINAL_STATES))
            clauses.append(f"state NOT IN ({placeholders})")
            args.extend(TERMINAL_STATES)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._to_task(r) for r in rows]

    def project_rollup(self) -> list[dict[str, Any]]:
        """按项目聚合任务状态 —— 「每个项目到哪一步了」的数据来源。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT project,
                          COUNT(*) AS total,
                          SUM(state='running')  AS running,
                          SUM(state='pending')  AS pending,
                          SUM(state='blocked')  AS blocked,
                          SUM(state='done')     AS done,
                          SUM(state IN ('failed','timeout')) AS failed,
                          MAX(COALESCE(finished_at, started_at, created_at)) AS last_at
                   FROM tasks GROUP BY project ORDER BY last_at DESC"""
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], project=row["project"], project_path=row["project_path"],
            prompt=row["prompt"], engine=row["engine"],
            engine_default=bool(row["engine_default"]), state=row["state"],
            session_ref=row["session_ref"], result=row["result"], error=row["error"],
            created_at=row["created_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], heartbeat_at=row["heartbeat_at"],
            attempts=row["attempts"],
        )

    # ── 事件 ──

    def log(
        self,
        *,
        layer: str,
        kind: str,
        task_id: str | None = None,
        project: str | None = None,
        severity: str = "info",
        detail: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO events (task_id, project, layer, kind, severity, detail, at)
                   VALUES (?,?,?,?,?,?,?)""",
                (task_id, project, layer, kind, severity, detail, time.time()),
            )

    def events(self, *, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        args: list[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            args.append(task_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── 恢复 ──

    def reap_stale(self, *, stale_after: float = 3600.0) -> list[Task]:
        """把心跳超时的 running 任务标为 timeout。

        Pi 主进程崩溃重启后，上一批 running 任务已没有对应进程，必须收尾，
        否则它们会永远显示"进行中"。
        """
        cutoff = time.time() - stale_after
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM tasks WHERE state=? AND COALESCE(heartbeat_at, started_at, 0) < ?",
                (STATE_RUNNING, cutoff),
            ).fetchall()
            stale = [self._to_task(r) for r in rows]
            for t in stale:
                c.execute(
                    "UPDATE tasks SET state=?, error=?, finished_at=? WHERE id=?",
                    (STATE_TIMEOUT, "心跳超时，进程可能已消失", time.time(), t.id),
                )
        for t in stale:
            self.log(task_id=t.id, project=t.project, layer="orchestrator",
                     kind="task_reaped", severity="warning", detail="心跳超时被回收")
        return stale
