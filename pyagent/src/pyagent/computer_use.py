"""Computer Use：代替人做 GUI 上的机械操作（点浏览器的 Allow/Access 之类）。

为什么走 codex 而不是自己实现：本机 Codex CLI 已装并启用官方
`computer-use@openai-bundled` 插件，它以 MCP server 形式暴露 10 个工具
（list_apps / get_app_state / click / press_key / type_text / scroll / drag …）。
pi 目前不原生支持挂载 MCP server，所以由 codex 来驱动这一层最省事也最可靠 ——
不用逆向 SkyComputerUseService 的私有接口。

与派活的区别：GUI 操作不属于任何项目，且需要访问系统（辅助功能、窗口），
因此**不套内核沙箱**。这是有意的例外，调用点必须显式知道这一点。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# codex 插件市场里 computer-use 的固定位置
_PLUGIN_REL = Path(".tmp/bundled-marketplaces/openai-bundled/plugins/computer-use")
_MCP_LAUNCHER = Path("bin/computer-use-client-launcher")

# GUI 操作通常几秒到几十秒，给足但不无限
DEFAULT_TIMEOUT = 300.0


@dataclass
class Availability:
    ok: bool
    detail: str
    plugin_dir: Path | None = None


def check(codex_home: Path | None = None) -> Availability:
    """确认 Computer Use 可用。不可用时给出可执行的修复指引。"""
    home = codex_home or (Path.home() / ".codex")
    if not home.is_dir():
        return Availability(False, f"找不到 {home} —— 本机似乎没装 Codex CLI")

    plugin = home / _PLUGIN_REL
    if not (plugin / _MCP_LAUNCHER).is_file():
        return Availability(
            False,
            "codex 的 computer-use 插件未就位。修复：运行 `codex plugin list` 确认，"
            "必要时 `codex plugin install computer-use@openai-bundled`",
        )

    # 插件装了还要确认在 config.toml 里启用，否则 codex 不会加载它
    cfg = home / "config.toml"
    enabled = False
    if cfg.is_file():
        try:
            text = cfg.read_text()
            enabled = 'plugins."computer-use@openai-bundled"' in text and "enabled = true" in text
        except OSError:
            enabled = False
    if not enabled:
        return Availability(
            False,
            "插件已装但未在 ~/.codex/config.toml 启用。修复："
            "`codex plugin enable computer-use@openai-bundled`",
            plugin,
        )

    return Availability(True, "codex computer-use 插件已装且已启用", plugin)


def run(instruction: str, *, timeout: float = DEFAULT_TIMEOUT, cwd: Path | None = None) -> str:
    """让 codex 用 computer-use 执行一段 GUI 操作。

    注意：不套沙箱（GUI 操作要访问系统），也就是说这条路径上没有内核写入边界。
    只应用于明确的人工替代操作，不要拿它跑开发任务。
    """
    avail = check()
    if not avail.ok:
        raise RuntimeError(avail.detail)

    prompt = (
        "请用 computer-use 工具完成下面这件事。这是替代人工的机械操作，"
        "完成后用一句话说明你做了什么、当前界面状态如何。"
        "如果找不到目标元素，不要猜着乱点，直接说明你看到了什么。\n\n"
        f"要做的事：{instruction}"
    )
    argv = [
        "codex", "exec",
        "--skip-git-repo-check",
        "-s", "danger-full-access",  # GUI 操作需要，且本身不写项目文件
        prompt,
    ]
    try:
        out = subprocess.run(
            argv,
            cwd=str(cwd or Path.home()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 codex 命令") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Computer Use 超时（{timeout}s），界面可能在等别的东西") from exc

    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip()[-800:]
        raise RuntimeError(f"codex 退出码 {out.returncode}：{tail}")
    return (out.stdout or "").strip()


def mcp_tools(codex_home: Path | None = None, *, timeout: float = 30.0) -> list[str]:
    """列出 computer-use MCP server 提供的工具名。用于体检与排障。"""
    avail = check(codex_home)
    if not avail.ok or avail.plugin_dir is None:
        return []

    proc = subprocess.Popen(
        [str(_MCP_LAUNCHER), "mcp"],
        cwd=str(avail.plugin_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env={"CODEX_HOME": str(codex_home or Path.home() / ".codex"), "PATH": "/usr/bin:/bin"},
    )
    names: list[str] = []
    try:
        assert proc.stdin and proc.stdout
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "pyagent", "version": "0.1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        import threading

        def read() -> None:
            assert proc.stdout
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("id") == 2:
                    for t in (d.get("result") or {}).get("tools", []):
                        n = t.get("name")
                        if isinstance(n, str):
                            names.append(n)
                    return

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout=timeout)
    finally:
        proc.kill()
    return names
