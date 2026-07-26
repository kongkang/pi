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
    sandboxed: bool = typer.Option(
        True,
        "--sandbox/--no-sandbox",
        help="内核沙箱强制写入边界，只允许写当前项目（默认开启）",
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
        sandboxed=sandboxed,
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


@app.command()
def recall(
    project: str = typer.Argument(..., help="项目名或路径"),
    limit: int = typer.Option(2, "--limit", "-n", help="还原几个会话"),
) -> None:
    """还原某项目历史会话的上下文 —— 「这对话当初要干什么、现在到哪了」。"""
    from . import context as ctxmod

    cfg = cfgmod.load()
    cand = Path(project).expanduser()
    path = (cand if cand.is_absolute() else cfg.projects_root / project).resolve()
    if not path.is_dir():
        console.print(f"[red]目录不存在：{path}[/red]")
        raise typer.Exit(1)

    found = ctxmod.find_sessions(path)
    if not found:
        console.print(f"[yellow]没找到 {path.name} 的历史会话[/yellow]")
        console.print("[dim]已查：pi / Claude Code / Codex 三处会话目录[/dim]")
        return

    by_engine: dict[str, int] = {}
    for engine, _ in found:
        by_engine[engine] = by_engine.get(engine, 0) + 1
    console.print(
        f"[dim]{path.name} 找到 "
        + "、".join(f"{k} {v} 个" for k, v in by_engine.items())
        + " 会话[/dim]\n"
    )

    digests = ctxmod.recall(path, limit=limit)
    if not digests:
        console.print("[yellow]会话文件存在但无法解析出对话内容[/yellow]")
        return
    for d in digests:
        console.print(d.render())
        console.print()


@app.command()
def dispatch(
    project: str = typer.Argument(..., help="派给哪个项目"),
    task: str = typer.Argument(..., help="要它做什么"),
    engine: str = typer.Option(
        "", "--engine", "-e", help="pi / codex / claude（不指定则用 pi 并在回执标注）"
    ),
) -> None:
    """把任务派到某个项目目录的子会话 —— Pi 自己不执行，只编排。"""
    from .dispatcher import ENGINES, Dispatcher
    from .store import Store

    cfg = cfgmod.load()
    st = Store(cfg.db_path)

    if engine and engine.lower() not in ENGINES:
        console.print(f"[red]未知引擎 {engine}[/red]，可选：{'/'.join(ENGINES)}")
        raise typer.Exit(1)

    def on_ui(req: dict) -> dict | None:
        console.print(f"\n[yellow]⚠ 子 Agent 请求决策[/yellow] {req.get('title')}")
        console.print("  [dim]本地 CLI 模式按安全默认拒绝；接入飞书后可点按钮批准。[/dim]")
        return None

    disp = Dispatcher(cfg, st, ui_handler=on_ui)
    try:
        # CLI 是一次性进程，必须同步等完 —— 后台线程会随进程退出被杀，
        # 任务会永远卡在 running。真正的异步派活由 serve 守护进程承担。
        t = disp.dispatch(project, task, engine=engine.lower() or None, wait=True)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    fresh = st.get_task(t.id)
    if fresh is None:
        console.print("[red]任务记录丢失[/red]")
        raise typer.Exit(1)

    tag = f"{fresh.engine}{'（未指定，已用默认）' if fresh.engine_default else ''}"
    console.print(f"[dim]任务 {fresh.id} · {fresh.project} · 引擎 {tag} · "
                  f"耗时 {fresh.running_seconds:.0f}s[/dim]\n")
    if fresh.state == "done":
        console.print(fresh.result or "[dim](空结果)[/dim]")
    else:
        console.print(f"[red]状态 {fresh.state}[/red]：{fresh.error or '无错误信息'}")
        raise typer.Exit(1)


@app.command()
def tasks(
    project: str = typer.Option("", "--project", "-p", help="只看某项目"),
    active: bool = typer.Option(False, "--active", help="只看未结束的"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """列出派出去的任务。"""
    from .store import Store

    cfg = cfgmod.load()
    st = Store(cfg.db_path)
    reaped = st.reap_stale()
    if reaped:
        console.print(f"[yellow]回收了 {len(reaped)} 个心跳超时的任务[/yellow]")

    items = st.tasks(project=project or None, active_only=active, limit=limit)
    if not items:
        console.print("[dim]还没有任务记录。用 pyagent dispatch 派一个。[/dim]")
        return

    colors = {"done": "green", "running": "cyan", "pending": "yellow",
              "blocked": "magenta", "failed": "red", "timeout": "red"}
    table = Table("任务", "项目", "引擎", "状态", "耗时", "内容")
    for t in items:
        c = colors.get(t.state, "white")
        table.add_row(
            t.id, t.project,
            t.engine + ("*" if t.engine_default else ""),
            f"[{c}]{t.state}[/{c}]",
            f"{t.running_seconds:.0f}s" if t.started_at else "-",
            t.summary(),
        )
    console.print(table)
    console.print("[dim]引擎带 * 表示派活时未指定、用了默认值[/dim]")


@app.command()
def status() -> None:
    """项目进度全景：git 状态 × 编排任务状态。"""
    from .store import Store

    cfg = cfgmod.load()
    st = Store(cfg.db_path)
    st.reap_stale()

    rollup = {r["project"]: r for r in st.project_rollup()}
    items = registry.scan(cfg.projects_root)

    table = Table("项目", "最近提交", "分支", "未提交", "在跑", "待决策", "已完成", "失败")
    shown = 0
    for p in items:
        r = rollup.get(p.name)
        # 没有任何编排记录且近期无提交的项目就不占屏
        if r is None and (p.last_commit_date or "") < "2026-06":
            continue
        table.add_row(
            p.name,
            p.last_commit_date or "-",
            (p.branch or "-")[:22],
            str(p.dirty_files) if p.dirty_files else "",
            str(r["running"] or "") if r else "",
            str(r["blocked"] or "") if r else "",
            str(r["done"] or "") if r else "",
            str(r["failed"] or "") if r else "",
        )
        shown += 1
    console.print(table)
    console.print(f"[dim]显示 {shown} 个（近期活跃或有编排记录），共扫描 {len(items)} 个项目[/dim]")


@app.command("log")
def show_log(
    task_id: str = typer.Option("", "--task-id", "-t", help="只看某任务"),
    limit: int = typer.Option(40, "--limit", "-n"),
) -> None:
    """事件流回放 —— 排障时定位是哪一层出的问题。"""
    import time as _time

    from .store import Store

    cfg = cfgmod.load()
    st = Store(cfg.db_path)
    evts = st.events(task_id=task_id or None, limit=limit)
    if not evts:
        console.print("[dim]暂无事件[/dim]")
        return
    table = Table("时间", "层", "事件", "任务", "详情")
    for e in evts:
        sev = e["severity"]
        color = {"warning": "yellow", "error": "red"}.get(sev, "white")
        table.add_row(
            _time.strftime("%m-%d %H:%M:%S", _time.localtime(e["at"])),
            e["layer"],
            f"[{color}]{e['kind']}[/{color}]",
            (e["task_id"] or "")[:10],
            (e["detail"] or "")[:60],
        )
    console.print(table)


@app.command("computer-use")
def computer_use_cmd(
    instruction: str = typer.Argument(
        "", help="要做的 GUI 操作，例如「在 Chrome 的权限弹窗里点 Allow」"
    ),
    check_only: bool = typer.Option(False, "--check", help="只体检，不执行"),
) -> None:
    """替你做 GUI 上的机械操作（点浏览器 Allow/Access 之类）。

    走 codex 已启用的官方 computer-use 插件。注意这条路径不套内核沙箱，
    因为 GUI 操作需要访问系统 —— 只用于人工替代操作，别拿它跑开发任务。
    """
    from . import computer_use as cu

    avail = cu.check()
    style = "green" if avail.ok else "red"
    console.print(f"[{style}]{'可用' if avail.ok else '不可用'}[/{style}]：{avail.detail}")

    if avail.ok and check_only:
        tools = cu.mcp_tools()
        if tools:
            console.print(f"[dim]提供 {len(tools)} 个工具：{', '.join(tools)}[/dim]")
        else:
            console.print("[yellow]MCP server 未返回工具列表（可能首次启动较慢）[/yellow]")
    if check_only:
        raise typer.Exit(0 if avail.ok else 1)
    if not avail.ok:
        raise typer.Exit(1)
    if not instruction.strip():
        console.print("[red]请给出要做的操作[/red]，或用 --check 只做体检")
        raise typer.Exit(1)

    console.print("[yellow]⚠ 即将操作你的桌面[/yellow]，请不要同时使用鼠标键盘\n")
    try:
        console.print(cu.run(instruction))
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command("feishu-setup")
def feishu_setup(
    app_id: str = typer.Option(..., "--app-id", prompt="飞书 App ID"),
    app_secret: str = typer.Option(..., "--app-secret", prompt="飞书 App Secret", hide_input=True),
) -> None:
    """保存飞书应用凭证（写入状态目录，权限 0600，不进 git）。"""
    from .orchestrator import save_credentials

    cfg = cfgmod.load()
    # 走统一的加锁 RMW + 原子替换：该文件同时存 chat_id，serve 进程也会写它
    path = save_credentials(cfg, app_id.strip(), app_secret.strip())
    console.print(f"[green]已保存[/green] → {path}（权限 0600）")
    console.print("下一步：[cyan]pyagent serve[/cyan] 启动，然后在飞书给机器人发一句话。")


@app.command()
def serve(
    model: str = typer.Option("", "--model", "-m", help="主对话使用的模型"),
) -> None:
    """启动飞书主对话守护进程（WebSocket 长连接，免公网 IP）。"""
    from .orchestrator import Orchestrator, load_feishu_config

    cfg = cfgmod.load()
    fs = load_feishu_config(cfg)
    if not fs.configured:
        console.print("[red]缺少飞书凭证[/red]")
        console.print("先运行 [cyan]pyagent feishu-setup[/cyan]，或设置环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET")
        raise typer.Exit(1)

    # 凭证文件可能损坏，不能让 JSONDecodeError 冒成堆栈
    try:
        has_model_auth = bool(json.loads(cfg.pi_auth.read_text() or "{}")) if cfg.pi_auth.is_file() else False
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]读取 pi 凭证失败[/red]：{exc}")
        console.print("修复：重新运行 [cyan]pyagent adopt-codex-auth[/cyan]（可用 --revert 回退）")
        raise typer.Exit(1) from exc
    if not has_model_auth:
        console.print("[red]pi 未配置模型凭证[/red] —— 先运行 [cyan]pyagent adopt-codex-auth[/cyan]")
        raise typer.Exit(1)

    orch = Orchestrator(cfg, fs, model=model)
    console.print("[green]正在连接飞书[/green]（长连接，无需公网 IP）…")
    console.print(f"[dim]主控对话：{fs.default_chat or '未设置 —— 在飞书给机器人发一句话即自动绑定'}[/dim]")
    console.print("[dim]Ctrl+C 退出[/dim]\n")
    try:
        orch.run()
    except KeyboardInterrupt:
        console.print("\n正在退出…")
    except Exception as exc:
        # 常见于凭证无效、应用未发布、网络不通
        console.print(f"[red]飞书连接失败[/red]：{type(exc).__name__}: {exc}")
        console.print(
            "[dim]排查：凭证是否正确、应用是否已发布、"
            "「事件与回调」是否选了长连接并订阅 im.message.receive_v1[/dim]"
        )
        raise typer.Exit(1) from exc
    finally:
        orch.stop()


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
