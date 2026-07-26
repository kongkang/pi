"""跨会话上下文还原：回答「这个对话当初要干什么、现在到哪了」。

场景：过几天回来做决策时，早忘了那个会话的目标。Pi 需要能主动去读历史
把上下文捞回来，而不是让人自己翻。

三种引擎的会话日志格式各不相同，但都是 JSONL 且都能定位到 role/content：
- pi     ~/.pi/agent/sessions/<slug>/*.jsonl        {"type":"message","message":{role,content}}
- claude ~/.claude/projects/<slug>/*.jsonl          {"type":"user","message":{role,content}}
- codex  ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl {"type":"response_item","payload":{role,content}}

解析刻意容错：格式是各家内部实现，随时可能变。解析失败时降级而不是报错，
但必须把降级状态**明确暴露**（quality 字段）—— 否则会悄悄把不可信上下文
灌给模型，那比读不到更糟。
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# 尾部保留多少条交互作为"最近进展"
_TAIL_KEEP = 12
# 单条文本截断长度
_SNIPPET = 300
# 扫描单个文件的行数上限，防止极大会话拖慢
_MAX_LINES = 20_000

QUALITY_EXACT = "exact"
QUALITY_DEGRADED = "degraded"
QUALITY_FAILED = "failed"


@dataclass
class SessionDigest:
    engine: str
    path: Path
    session_id: str = ""
    cwd: str = ""
    goal: str = ""
    recent: list[str] = field(default_factory=list)
    turns: int = 0
    quality: str = QUALITY_EXACT
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.quality != QUALITY_FAILED and bool(self.goal or self.recent)

    def render(self) -> str:
        """给人看（也给模型看）的摘要。"""
        lines = [f"引擎：{self.engine}    会话：{self.session_id or self.path.name}"]
        if self.cwd:
            lines.append(f"目录：{self.cwd}")
        lines.append(f"交互轮数：{self.turns}")
        if self.quality != QUALITY_EXACT:
            lines.append(f"⚠ 上下文质量：{self.quality}（{self.note}）—— 结论请谨慎采信")
        if self.goal:
            lines.append(f"\n当初的目标：\n{self.goal}")
        if self.recent:
            lines.append("\n最近进展：")
            lines.extend(f"  · {r}" for r in self.recent)
        return "\n".join(lines)


def _text_from_content(content: object) -> str:
    """content 可能是字符串，也可能是内容块数组（各家 type 命名不同）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
                continue
            if not isinstance(blk, dict):
                continue
            # pi/claude 用 "text"，codex 用 "input_text"/"output_text"
            if blk.get("type") in ("text", "input_text", "output_text"):
                t = blk.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts).strip()
    if isinstance(content, dict):
        # 有的格式把内容包成 {"text": ...} 或再套一层 content
        t = content.get("text")
        if isinstance(t, str):
            return t.strip()
        nested = content.get("content")
        if nested is not None:
            return _text_from_content(nested)
    return ""


def _extract(entry: dict) -> tuple[str, str] | None:
    """从一行日志里取出 (role, text)。三种格式统一走这里。"""
    for key in ("message", "payload"):
        node = entry.get(key)
        if isinstance(node, dict):
            role = node.get("role")
            if isinstance(role, str):
                text = _text_from_content(node.get("content"))
                if text:
                    return role, text
    return None


def digest_file(path: Path, engine: str) -> SessionDigest:
    """读一个会话文件，提取目标与最近进展。流式读取，不全量载入。"""
    d = SessionDigest(engine=engine, path=path)
    tail: deque[tuple[str, str]] = deque(maxlen=_TAIL_KEEP)
    bad_lines = 0
    total_lines = 0
    truncated = False

    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for total_lines, raw in enumerate(fh, start=1):
                if total_lines > _MAX_LINES:
                    truncated = True
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                if not isinstance(entry, dict):
                    bad_lines += 1
                    continue

                # 会话元信息：各家都在头部
                if not d.session_id:
                    for k in ("id", "session_id", "sessionId"):
                        v = entry.get(k)
                        if isinstance(v, str) and v:
                            d.session_id = v
                            break
                    payload = entry.get("payload")
                    if isinstance(payload, dict):
                        for k in ("session_id", "id"):
                            v = payload.get(k)
                            if isinstance(v, str) and v:
                                d.session_id = d.session_id or v
                if not d.cwd:
                    cwd = entry.get("cwd")
                    if not isinstance(cwd, str):
                        payload = entry.get("payload")
                        cwd = payload.get("cwd") if isinstance(payload, dict) else None
                    if isinstance(cwd, str) and cwd:
                        d.cwd = cwd

                got = _extract(entry)
                if got is None:
                    continue
                role, text = got
                # developer/system 是各家注入的指令，不是用户意图
                if role in ("developer", "system", "tool"):
                    continue
                d.turns += 1
                if role == "user" and not d.goal:
                    d.goal = text[:_SNIPPET * 2]
                tail.append((role, text))
    except OSError as exc:
        d.quality = QUALITY_FAILED
        d.note = f"读取失败：{exc}"
        return d

    d.recent = [f"[{r}] {t[:_SNIPPET]}" for r, t in tail]

    if d.turns == 0:
        d.quality = QUALITY_FAILED
        d.note = "未能从该文件解析出任何对话内容（格式可能已变更）"
    elif truncated:
        d.quality = QUALITY_DEGRADED
        d.note = f"文件过大，仅扫描前 {_MAX_LINES} 行"
    elif bad_lines > max(5, total_lines * 0.1):
        d.quality = QUALITY_DEGRADED
        d.note = f"{bad_lines}/{total_lines} 行无法解析"
    elif not d.goal:
        # 拿不到"当初要干什么"就等于没达成目的，必须显式降级而非假装成功
        d.quality = QUALITY_DEGRADED
        d.note = "未能定位到最初的用户诉求，只有最近片段"
    return d


# ── 定位某项目的会话 ──


def _claude_slug(project_path: Path) -> str:
    return str(project_path).replace("/", "-")


def _entry_cwd(path: Path, *, max_lines: int = 40) -> str | None:
    """只读文件头几行取 cwd。pi 与 codex 都在会话头部记录工作目录。

    不去猜各家的目录名转写规则 —— 那是内部实现，猜错就静默失配。
    按会话自己记录的 cwd 匹配才靠得住。
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    return None
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                cwd = d.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
                payload = d.get("payload")
                if isinstance(payload, dict):
                    cwd = payload.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except OSError:
        return None
    return None


def find_sessions(project_path: Path, *, limit_per_engine: int = 5) -> list[tuple[str, Path]]:
    """找出某项目下三种引擎的会话文件，最近修改的在前。"""
    home = Path.home()
    found: list[tuple[str, Path]] = []

    # 规范化后再比较：会话里记录的可能是符号链接或未展开的路径
    target = str(project_path.expanduser().resolve())

    def _same(recorded: str | None) -> bool:
        if not recorded:
            return False
        if recorded == target:
            return True
        try:
            return str(Path(recorded).expanduser().resolve()) == target
        except (OSError, RuntimeError):
            return False

    # pi：目录名是内部转写规则，按会话记录的 cwd 匹配更稳
    pi_root = home / ".pi" / "agent" / "sessions"
    if pi_root.is_dir():
        candidates = sorted(
            pi_root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:200]
        hits = [p for p in candidates if _same(_entry_cwd(p))][:limit_per_engine]
        found += [("pi", p) for p in hits]

    # claude code
    cc_dir = home / ".claude" / "projects" / _claude_slug(project_path)
    if cc_dir.is_dir():
        files = sorted(cc_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        found += [("claude", p) for p in files[:limit_per_engine]]

    # codex：按日期分目录，且未按项目分，需读 cwd 匹配 —— 只扫最近的若干文件
    codex_root = home / ".codex" / "sessions"
    if codex_root.is_dir():
        candidates = sorted(
            codex_root.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:120]
        hits: list[Path] = []
        for p in candidates:
            if _same(_entry_cwd(p)):
                hits.append(p)
                if len(hits) >= limit_per_engine:
                    break
        found += [("codex", p) for p in hits]

    return found


def recall(project_path: Path, *, limit: int = 3) -> list[SessionDigest]:
    """还原某项目最近的若干会话上下文。"""
    out: list[SessionDigest] = []
    for engine, path in find_sessions(project_path):
        d = digest_file(path, engine)
        if d.usable:
            out.append(d)
        if len(out) >= limit:
            break
    return out
