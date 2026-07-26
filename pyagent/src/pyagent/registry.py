"""项目注册表：扫描 ~/Developer 下的开发项目，并关联已有的 Agent 会话历史。

关联历史是「我忘了这个对话当初要干什么」的基础：
- Claude Code 会话：~/.claude/projects/<路径转写>/*.jsonl
- Codex 会话：~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
- pi 会话：由 pi 自己按项目管理（--session-id / --session-dir）
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Project:
    name: str
    path: Path
    is_git: bool
    branch: str | None = None
    last_commit_date: str | None = None
    last_commit_subject: str | None = None
    dirty_files: int = 0
    claude_sessions: int = 0

    @property
    def active(self) -> bool:
        """有 git 历史即视为在管项目；非 git 目录标为 inactive。"""
        return self.is_git


def _git(path: Path, *args: str, timeout: float = 15.0) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def claude_slug(path: Path) -> str:
    """Claude Code 把项目绝对路径的 / 转写为 - 作为目录名。"""
    return str(path).replace("/", "-")


def _count_claude_sessions(path: Path) -> int:
    d = Path.home() / ".claude" / "projects" / claude_slug(path)
    if not d.is_dir():
        return 0
    return len(list(d.glob("*.jsonl")))


def _inspect(path: Path) -> Project:
    name = path.name
    if not (path / ".git").exists():
        return Project(name=name, path=path, is_git=False)

    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    log = _git(path, "log", "-1", "--format=%cd%x1f%s", "--date=short")
    date = subject = None
    if log and "\x1f" in log:
        date, subject = log.split("\x1f", 1)

    status = _git(path, "status", "--porcelain")
    dirty = len([ln for ln in status.splitlines() if ln.strip()]) if status else 0

    return Project(
        name=name,
        path=path,
        is_git=True,
        branch=branch,
        last_commit_date=date,
        last_commit_subject=subject,
        dirty_files=dirty,
        claude_sessions=_count_claude_sessions(path),
    )


def scan(projects_root: Path, *, include_nongit: bool = False, workers: int = 16) -> list[Project]:
    """并发扫描项目根目录。按最近提交时间倒序返回（最活跃在前）。"""
    if not projects_root.is_dir():
        return []

    candidates = [p for p in sorted(projects_root.iterdir()) if p.is_dir() and not p.name.startswith(".")]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        projects = list(pool.map(_inspect, candidates))

    if not include_nongit:
        projects = [p for p in projects if p.is_git]

    return sorted(projects, key=lambda p: (p.last_commit_date or "", p.name), reverse=True)
