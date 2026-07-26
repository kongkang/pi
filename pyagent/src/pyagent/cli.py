"""PyAgent 命令行入口。

第一步范围：体检、项目清单、以及通过 pi RPC 真实驱动一次 Codex 订阅调用。
编排（派活到子会话 / 飞书接入 / 授信升级）在后续步骤加入。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config as cfgmod
from . import registry
from .pi_rpc import PiRpcError, PiSession

app = typer.Typer(add_completion=False, help="Pi 编排器：统一管理多项目开发会话")
console = Console()


# 凭证备份保留个数：备份里含 refresh token，无限累积会持续扩大泄漏面
_AUTH_BACKUP_KEEP = 5


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.lstrip("v").split(".")[:3])


def _auth_backups(auth_path: Path) -> list[Path]:
    """按时间戳排序的备份列表（旧→新）。"""

    def ts(p: Path) -> int:
        try:
            return int(p.name.rsplit(".", 1)[-1])
        except ValueError:
            return 0

    return sorted(auth_path.parent.glob(f"{auth_path.name}.bak.*"), key=ts)


def _prune_auth_backups(auth_path: Path) -> None:
    stale = _auth_backups(auth_path)[:-_AUTH_BACKUP_KEEP]
    for old in stale:
        try:
            old.unlink()
        except OSError:
            pass
    if stale:
        console.print(f"[dim]已清理 {len(stale)} 个过期凭证备份[/dim]")


@app.command()
def doctor() -> None:
    """体检：node 版本、pi 构建产物、Codex 订阅登录态、项目根目录。"""
    cfg = cfgmod.load()
    table = Table("检查项", "结果", "说明", show_lines=False)
    ok = True

    # node
    if cfg.node_bin is None:
        table.add_row("node", "[red]缺失[/red]", "需要 node >= 22.19.0")
        ok = False
    else:
        import subprocess

        ver = subprocess.run([str(cfg.node_bin), "-v"], capture_output=True, text=True).stdout.strip()
        good = _version_tuple(ver) >= cfgmod.MIN_NODE
        table.add_row(
            "node",
            "[green]OK[/green]" if good else "[red]版本过低[/red]",
            f"{ver} @ {cfg.node_bin}",
        )
        ok = ok and good

    # pi 构建产物
    built = cfg.pi_cli.is_file()
    table.add_row(
        "pi 构建产物",
        "[green]OK[/green]" if built else "[red]缺失[/red]",
        str(cfg.pi_cli) if built else "在仓库根运行 npm run build",
    )
    ok = ok and built

    # Codex 订阅登录态
    auth_desc = "未登录 —— 运行 pyagent login-hint 查看步骤"
    logged_in = False
    if cfg.pi_auth.is_file():
        try:
            data = json.loads(cfg.pi_auth.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
        if data:
            keys = ", ".join(sorted(data.keys()))
            auth_desc = f"已配置 provider: {keys}"
            logged_in = True
    table.add_row(
        "Codex 订阅认证",
        "[green]OK[/green]" if logged_in else "[yellow]待登录[/yellow]",
        auth_desc,
    )

    # 项目根
    n = len([p for p in cfg.projects_root.iterdir() if p.is_dir()]) if cfg.projects_root.is_dir() else 0
    table.add_row(
        "项目根目录",
        "[green]OK[/green]" if n else "[red]找不到[/red]",
        f"{cfg.projects_root}（{n} 个子目录）",
    )

    console.print(table)
    if not logged_in:
        console.print("\n[yellow]下一步[/yellow]：需要你手动完成一次 Codex 订阅 OAuth 登录（见 pyagent login-hint）。")
    raise typer.Exit(0 if ok else 1)


@app.command("login-hint")
def login_hint() -> None:
    """打印把 Codex 订阅接到 pi 上的手动步骤（OAuth 需浏览器，无法自动化）。"""
    cfg = cfgmod.load()
    console.print("[bold]把 Codex 订阅接到 PyAgent 上[/bold]\n")
    console.print("1) 启动 pi 交互界面：")
    console.print(f"   [cyan]{cfg.node_bin} {cfg.pi_cli}[/cyan]\n")
    console.print("2) 在里面输入斜杠命令：[cyan]/login[/cyan]")
    console.print("3) 选择 [bold]ChatGPT Plus/Pro (Codex)[/bold]，浏览器完成授权")
    console.print(f"4) 凭证会写入 [cyan]{cfg.pi_auth}[/cyan]（过期自动刷新）")
    console.print("5) 回来运行 [cyan]pyagent doctor[/cyan] 确认变为 OK\n")
    console.print("[dim]说明：pi 不带 pi login 子命令，OAuth 必须在交互界面里完成，因此这步无法自动执行。[/dim]")


@app.command("adopt-codex-auth")
def adopt_codex_auth(
    revert: bool = typer.Option(False, "--revert", help="回退到最近一次备份"),
) -> None:
    """复用本机 Codex CLI 已有的 ChatGPT 登录态，免去在 pi 里再登录一次。

    可行原因：pi 的 openai-codex OAuth 用的 client_id 与 Codex CLI 相同，
    凭证可直接换算。只读 ~/.codex/auth.json，不修改它。

    取舍：两边共用同一个 refresh token，而 OpenAI 的 refresh token 是轮转式的
    （pi 源码注释提到 rotated token）。谁先刷新，另一边就需要重新登录。
    想彻底隔离，请在 pi 里执行一次 /login 单独授权。
    """
    import base64
    import time

    cfg = cfgmod.load()
    auth_path = cfg.pi_auth
    auth_path.parent.mkdir(parents=True, exist_ok=True)

    if revert:
        backups = _auth_backups(auth_path)
        if not backups:
            console.print("[red]找不到备份文件[/red]")
            raise typer.Exit(1)
        latest = backups[-1]
        fd = os.open(auth_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(latest.read_text())
        console.print(f"[green]已回退[/green] ← {latest.name}")
        return

    codex_auth = Path.home() / ".codex" / "auth.json"
    if not codex_auth.is_file():
        console.print(f"[red]找不到 {codex_auth}[/red] —— 请先用 codex CLI 登录 ChatGPT")
        raise typer.Exit(1)

    try:
        codex = json.loads(codex_auth.read_text())
        tokens = codex["tokens"]
        access, refresh = tokens["access_token"], tokens["refresh_token"]
    except (json.JSONDecodeError, KeyError) as exc:
        console.print(f"[red]解析 {codex_auth} 失败：{exc}[/red]")
        raise typer.Exit(1) from exc

    # 与 pi 的 credentialsFromToken 同逻辑：accountId 来自 access_token 的 JWT claim
    # exp 是秒，pi 存毫秒（其 OAuth 流程写的是 Date.now() + expires_in*1000）
    try:
        seg = access.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
        if not isinstance(claims, dict):
            raise TypeError("JWT payload 不是对象")
        auth_claim = claims.get("https://api.openai.com/auth")
        if not isinstance(auth_claim, dict):
            raise TypeError("JWT 缺少 openai auth claim")
        account_id = auth_claim["chatgpt_account_id"]
        expires_ms = int(claims["exp"]) * 1000
    # 注：binascii.Error 是 ValueError 的子类，畸形 base64 已被下面的 ValueError 覆盖
    except (IndexError, KeyError, ValueError, TypeError, AttributeError) as exc:
        console.print(f"[red]无法从 access_token 解析 accountId/exp：{exc}[/red]")
        raise typer.Exit(1) from exc

    if expires_ms <= time.time() * 1000:
        console.print("[yellow]警告：codex 的 access_token 已过期，请先在 codex CLI 里刷新一次[/yellow]")

    # 备份现有凭证，便于 --revert。备份同样含 refresh token，权限必须一并收紧，
    # 否则默认 umask 可能让同机其他用户读到。
    if auth_path.is_file():
        backup = auth_path.with_name(f"{auth_path.name}.bak.{int(time.time())}")
        fd = os.open(backup, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(auth_path.read_text())
        console.print(f"[dim]已备份 → {backup.name}[/dim]")
        _prune_auth_backups(auth_path)

    existing: dict = {}
    if auth_path.is_file():
        try:
            existing = json.loads(auth_path.read_text() or "{}")
        except json.JSONDecodeError:
            existing = {}

    existing["openai-codex"] = {
        "type": "oauth",
        "access": access,
        "refresh": refresh,
        "expires": expires_ms,
        "accountId": account_id,
    }
    auth_path.write_text(json.dumps(existing, indent=2))
    auth_path.chmod(0o600)

    console.print("[green]已接入 Codex 订阅[/green]")
    console.print(f"  provider  : openai-codex")
    console.print(f"  accountId : {account_id}")
    console.print(f"  plan      : {auth_claim.get('chatgpt_plan_type', '?')}")
    console.print(f"  过期时间  : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_ms / 1000))}")
    console.print("\n[dim]注意：与 codex CLI 共用 refresh token，轮转后另一边需重新登录。[/dim]")
    console.print("[dim]想彻底隔离：在 pi 交互界面执行 /login 单独授权（见 pyagent login-hint）。[/dim]")


@app.command()
def projects(
    include_nongit: bool = typer.Option(False, "--include-nongit", help="同时列出非 git 目录"),
    limit: int = typer.Option(20, "--limit", "-n", help="显示条数，0 表示全部"),
) -> None:
    """列出被编排的项目，按最近提交时间倒序。"""
    cfg = cfgmod.load()
    items = registry.scan(cfg.projects_root, include_nongit=include_nongit)
    shown = items if limit == 0 else items[:limit]

    table = Table("最近提交", "项目", "分支", "未提交", "CC 会话", "最后一次改动")
    for p in shown:
        table.add_row(
            p.last_commit_date or "-",
            p.name,
            p.branch or "-",
            str(p.dirty_files) if p.dirty_files else "",
            str(p.claude_sessions) if p.claude_sessions else "",
            (p.last_commit_subject or "")[:48],
        )
    console.print(table)
    console.print(f"[dim]共 {len(items)} 个项目，显示 {len(shown)} 个[/dim]")


@app.command()
def models(search: str = typer.Argument("", help="过滤关键字")) -> None:
    """列出登录后可用的模型（用于确认订阅是否生效）。"""
    cfg = cfgmod.load()
    with PiSession(cfg, cwd=cfg.repo_root, no_session=True) as s:
        try:
            found = s.available_models()
        except PiRpcError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    table = Table("provider", "模型 ID", "上下文", "推理")
    for m in found:
        mid = str(m.get("id", ""))
        prov = str(m.get("provider", ""))
        if search and search.lower() not in f"{prov}/{mid}".lower():
            continue
        table.add_row(prov, mid, str(m.get("contextWindow", "")), "是" if m.get("reasoning") else "")
    console.print(table)
    console.print(f"[dim]共 {len(found)} 个模型可用[/dim]")


@app.command()
def ask(
    message: str = typer.Argument(..., help="要问的内容"),
    project: str = typer.Option("", "--project", "-C", help="在某项目目录下运行（默认本仓库）"),
    model: str = typer.Option("", "--model", "-m", help="模型，如 openai-codex/gpt-5.3-codex"),
    timeout: float = typer.Option(900.0, "--timeout", help="等待上限（秒）"),
    session: str = typer.Option("", "--session", help="复用/创建具名持久会话（默认一次性不留档）"),
    allow_any_path: bool = typer.Option(
        False, "--allow-any-path", help="允许 --project 指向项目根之外的目录（默认禁止）"
    ),
) -> None:
    """通过 pi RPC 真实跑一轮 —— 用于验证 Codex 订阅链路是否打通。"""
    cfg = cfgmod.load()
    cwd = cfg.repo_root
    if project:
        cand = Path(project).expanduser()
        cwd = (cand if cand.is_absolute() else cfg.projects_root / project).resolve()
        if not cwd.is_dir():
            console.print(f"[red]目录不存在：{cwd}[/red]")
            raise typer.Exit(1)
        # 子会话会以当前用户权限在该目录里跑工具（pi 无内置沙箱），因此默认
        # 只许落在项目根内，避免 ../.. 或绝对路径把 Agent 放到 /etc 这类地方
        root = cfg.projects_root.resolve()
        inside = cwd == root or root in cwd.parents
        if not (inside or cwd == cfg.repo_root.resolve() or allow_any_path):
            console.print(f"[red]拒绝：{cwd} 不在项目根 {root} 之内[/red]")
            console.print("[dim]确实需要请显式加 --allow-any-path[/dim]")
            raise typer.Exit(1)

    def on_ui(req: dict) -> dict | None:
        """第一步：把授信升级请求打到终端，默认拒绝（不自动批准危险操作）。"""
        console.print(f"\n[yellow]⚠ 需要决策[/yellow] {req.get('method')}: {req.get('title')}")
        if req.get("options"):
            console.print(f"  选项: {req['options']}")
        console.print("  [dim]第一步尚未接入飞书，按安全默认拒绝。[/dim]")
        return None

    with PiSession(
        cfg,
        cwd=cwd,
        model=model or None,
        no_session=not session,
        session_id=session or None,
        ui_handler=on_ui,
    ) as s:
        try:
            st = s.state()
            m = st.get("model") or {}
            sid = st.get("sessionId", "?")
            console.print(
                f"[dim]会话就绪 · 目录 {cwd} · 模型 {m.get('provider', '?')}/{m.get('id', '?')}"
                f" · session {sid}{' (持久)' if session else ' (一次性)'}[/dim]\n"
            )
            answer = s.ask(message, timeout=timeout)
        except PiRpcError as exc:
            console.print(f"[red]{exc}[/red]")
            if s.stderr_text:
                console.print(f"[dim]{s.stderr_text[-1500:]}[/dim]")
            raise typer.Exit(1) from exc
        finally:
            if s.dropped_events:
                console.print(f"[yellow]提示：有 {s.dropped_events} 条事件因队列积压被丢弃[/yellow]")

    console.print(answer or "[dim](空回复)[/dim]")


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
