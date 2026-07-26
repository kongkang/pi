"""macOS 内核级写入边界（sandbox-exec）。

为什么需要它：实测证明，靠分析 shell 命令文本来管控行为是不可靠的 ——
守卫拦掉 `rm -rf <file>` 之后，模型立刻改用 `python -c "os.remove(...)"` 达成
同样效果。任何解释器都能绕过文本匹配，黑名单不可能穷尽。

pi 官方 security.md 也明确讲了这点：真正的隔离必须来自操作系统或虚拟化边界，
进程内的部分沙箱容易被误当成安全边界。

所以这里用 macOS 自带的 sandbox-exec（Seatbelt）做**内核强制**的写入边界：
默认禁止一切写入，只放开当前项目目录与必要的运行时路径。python/node/perl
一律绕不过，因为拦截发生在系统调用层。

这同时把「不许碰其他项目」从约定变成了技术强制。

注意：sandbox-exec 在近版 macOS 上被标记为 deprecated，但仍然可用（已实测）。
若未来失效，替代路径是容器或 VM（见 pi 的 docs/containerization.md）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def available() -> bool:
    return Path(SANDBOX_EXEC).is_file()


def _quote(path: str) -> str:
    """Seatbelt profile 用的字符串字面量转义。"""
    return path.replace("\\", "\\\\").replace('"', '\\"')


def build_profile(writable: list[Path], *, allow_network: bool = True) -> str:
    """生成 Seatbelt profile：默认允许，但写入仅限白名单子树。

    刻意不用 `(deny default)` —— 那会连读文件、执行程序、加载动态库都禁掉，
    项目工具链（node/npm/git/pytest）根本跑不起来。我们要管的是**写**，
    因为破坏性操作与跨项目污染都体现在写上。
    """
    lines = [
        "(version 1)",
        "(allow default)",
        "",
        ";; 默认禁止一切写入与删除",
        "(deny file-write*)",
        "",
        ";; 仅放开这些子树",
    ]
    for p in writable:
        lines.append(f'(allow file-write* (subpath "{_quote(str(p))}"))')

    lines += [
        "",
        ";; 终端/设备等常规写入目标",
        '(allow file-write* (literal "/dev/null") (literal "/dev/stdout") (literal "/dev/stderr"))',
        "(allow file-write* (regex #\"^/dev/tty\"))",
        "(allow file-write-data (regex #\"^/dev/(null|zero|random|urandom)$\"))",
    ]

    if not allow_network:
        lines += ["", ";; 断网", "(deny network*)"]

    return "\n".join(lines) + "\n"


def writable_paths(project_dir: Path, *, extra: list[Path] | None = None) -> list[Path]:
    """默认可写集合：当前项目 + 运行时必须的路径。

    刻意**不含**项目根（~/Developer）—— 那正是要拦的跨项目写入。
    也不含 ~/.ssh、~/.aws、~/.codex 等凭证目录。
    """
    home = Path.home()
    paths = [
        project_dir.resolve(),
        # pi 自身要写会话、模型缓存
        home / ".pi",
        # 常见临时目录（编译产物、pytest 缓存、npm 临时文件）
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/folders"),
        Path("/private/var/folders"),
        # 包管理器缓存，否则 npm/uv/pip 会失败
        home / ".npm",
        home / ".cache",
        home / "Library" / "Caches",
    ]
    if extra:
        paths += [p.resolve() for p in extra]
    # 去重且保持顺序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def wrap(argv: list[str], profile_path: Path) -> list[str]:
    """把命令包进 sandbox-exec。"""
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]


def self_check(profile_path: Path, victim: Path) -> tuple[bool, str]:
    """自检：确认沙箱真的能挡住解释器写入。

    不信任配置就直接测一次 —— 沙箱失效而以为生效，比没有沙箱更危险。
    """
    if not available():
        return False, "sandbox-exec 不存在"
    py = shutil.which("python3") or shutil.which("python")
    if not py:
        return False, "找不到 python 解释器，无法自检"

    code = (
        "import os,sys\n"
        f"p={str(victim)!r}\n"
        "try:\n"
        "    open(p,'a').close(); sys.stdout.write('WROTE')\n"
        "except Exception as e:\n"
        "    sys.stdout.write('BLOCKED')\n"
    )
    try:
        out = subprocess.run(
            wrap([py, "-c", code], profile_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"自检执行失败：{exc}"

    if "BLOCKED" in out.stdout:
        return True, "沙箱生效：解释器写入被内核拦截"
    if "WROTE" in out.stdout:
        return False, "沙箱未生效：解释器仍能写入禁止路径"
    return False, f"自检结果无法判定：stdout={out.stdout!r} stderr={out.stderr[-200:]!r}"
