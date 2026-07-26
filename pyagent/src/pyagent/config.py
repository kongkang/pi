"""PyAgent 配置：解析 pi 运行时位置、项目根目录等。

设计约束：编排层必须固定使用满足 pi engines 要求的 node（>=22.19.0），
不能依赖调用方 shell 的 PATH —— 用户全局 default 可能是别的版本。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# pi 要求的最低 node 版本（见 pi 仓库根 package.json engines.node）
MIN_NODE = (22, 19, 0)

# 本仓库锁定的 node 版本（.nvmrc）
PINNED_NODE = "22.23.1"


# pi 构建产物的相对位置，用作识别仓库根的锚点
_PI_CLI_REL = Path("packages") / "coding-agent" / "dist" / "cli.js"


def _repo_root() -> Path:
    """定位 Pi-KK 仓库根。

    不用固定层数上溯（目录改名或打包后就会算错），而是向上找 pi 的构建产物
    或 .git 作为锚点；都找不到才退回固定层数。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / _PI_CLI_REL).is_file():
            return parent
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "packages").is_dir():
            return parent
    # 兜底：pyagent/src/pyagent/config.py → 上溯 3 层到仓库根
    return here.parents[3]


def _find_node() -> Path | None:
    """优先用 .nvmrc 锁定版本，其次找 PATH 里满足版本要求的 node。"""
    pinned = Path.home() / ".nvm" / "versions" / "node" / f"v{PINNED_NODE}" / "bin" / "node"
    if pinned.is_file():
        return pinned

    which = shutil.which("node")
    return Path(which) if which else None


@dataclass
class Config:
    repo_root: Path = field(default_factory=_repo_root)
    # 存放各项目开发进度与任务状态
    state_dir: Path = field(default_factory=lambda: Path.home() / ".pyagent")
    # 被编排的项目根目录（用户的 Developer 目录）
    projects_root: Path = field(default_factory=lambda: Path.home() / "Developer")
    node_bin: Path | None = field(default_factory=_find_node)

    @property
    def pi_cli(self) -> Path:
        """pi 构建产物入口。"""
        return self.repo_root / "packages" / "coding-agent" / "dist" / "cli.js"

    @property
    def pi_auth(self) -> Path:
        """pi 的凭证文件；Codex 订阅 OAuth token 存在这里。"""
        return Path.home() / ".pi" / "agent" / "auth.json"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "pyagent.sqlite3"

    def env(self) -> dict[str, str]:
        """给 pi 子进程的环境：把锁定 node 放到 PATH 首位。"""
        env = dict(os.environ)
        if self.node_bin:
            env["PATH"] = f"{self.node_bin.parent}{os.pathsep}{env.get('PATH', '')}"
        return env


def load() -> Config:
    cfg = Config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg
