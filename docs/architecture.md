# Pi 编排器架构（PyAgent）

## 定位

PyAgent 是**核心编排器**，自己不开发任何项目。职责四件：

1. 接收想法（最终从飞书进来）
2. 判断该派给 `~/Developer` 下哪个项目
3. 在该项目目录启动子 Agent 会话去解决
4. 汇总各项目进度；需要决策时主动来问

## 关键架构决策：复用 pi 作为 Agent 引擎

初版方案打算用 Python 自建 Agent 循环（subprocess 包 `codex exec`）。调研后改为
**以 [pi](https://github.com/earendil-works/pi) 为 Agent 引擎，PyAgent 通过 RPC 驱动它**。

决策依据：

- **Codex 订阅是 pi 的一等公民**。`/login` 选 ChatGPT Plus/Pro (Codex) 走 OAuth，
  凭证存 `~/.pi/agent/auth.json` 并自动刷新。无需 API Key，官方标注 "Codex for OSS"。
- **`pi --mode rpc` 是干净的程序化接口**：JSONL 协议，官方文档直接给出 Python 客户端示例。
- **pi 已内置我们本来要自建的东西**，见下表。

| 原计划自建 | pi 已内置 |
|---|---|
| 分段记忆防上下文爆炸 | `compact` + `set_auto_compaction`（自动压缩） |
| 重试策略与错误分类 | `set_auto_retry` + `auto_retry_*` 事件 |
| 「这个对话当初要干什么」 | `get_entries`（`since` 游标增量拉取）/ `get_tree` |
| 决策升级机制 | `extension_ui_request` 协议（阻塞等客户端回应） |
| 事件归一化 | 完整事件流（`agent_settled` / `tool_execution_*` / `queue_update`） |

这同时解决了「PyAgent 要是 Python」与「用成熟 harness」的矛盾：
**pi(TS) 当引擎，PyAgent(Python) 当编排层**。

## 分层

```
飞书（第二步）/ 本地 CLI（第一步已通）
        │
        ▼
PyAgent 编排层 (Python)
  ├─ pi_rpc.py    驱动 pi --mode rpc，严格 JSONL 分帧
  ├─ registry.py  扫描 ~/Developer，关联历史会话
  ├─ config.py    锁定 node 版本与 pi 产物路径
  └─ cli.py       doctor / projects / models / ask
        │ 每项目一个 RPC 子进程
        ▼
pi (TypeScript) —— Agent 引擎，模型额度走 Codex 订阅 OAuth
        │
        ▼
各项目目录（子 Agent 在此干活）
```

## 授信边界：两层，详见 security-model.md

完整推导与实测记录见 [security-model.md](security-model.md)。要点：

**只靠 extension 拦命令文本是不成立的。** 实测中守卫拦掉 `rm -rf <file>` 后，
模型立刻改用 `python -c "os.remove(...)"` 并成功删除了目标 —— 任何解释器都能
绕过文本黑名单。

因此改为两层，职责不同：

1. **内核层（真边界，默认开启）**：`sandbox-exec` 强制写入白名单 —— 只允许写
   当前项目、`~/.pi`、临时目录与包缓存。不含项目根 `~/Developer`，也不含凭证目录。
   同一个 python 绕过手法在这一层拿到 `PermissionError`。
   启动前必做自检，不通过就拒绝开会话。
2. **守卫层（提醒层）**：pi extension 拦 `tool_call`，把「这操作要不要问你」
   送到飞书卡片。它管的是 `git push`、`npm publish` 这类权限上合法但你该知情的操作 ——
   内核沙箱表达不了这个语义。

这也把「跨项目操作硬禁止」从约定变成了内核强制。

安全默认：无处理器 / 处理器异常 / 超时 / 未知 dialog 方法，一律不批准。
宁可卡住也不误批。

## Computer Use

本机 Codex CLI 的 `computer-use@openai-bundled` 插件（v1.0.1000502）已安装启用，
`SkyComputerUseService` 就位。点浏览器 Allow 这类操作复用它，不自建。留到第三步。

## Codex 订阅接入：复用已有登录态

pi 的 `openai-codex` OAuth 与 Codex CLI **使用同一个 client_id**
（`app_EMoamEEZ73f0CkXaXp7hrann`），凭证格式可直接换算：

| Codex CLI (`~/.codex/auth.json`) | pi (`~/.pi/agent/auth.json`) |
|---|---|
| `tokens.access_token` | `openai-codex.access` |
| `tokens.refresh_token` | `openai-codex.refresh` |
| access_token JWT 的 `exp` × 1000 | `openai-codex.expires`（毫秒） |
| JWT claim `chatgpt_account_id` | `openai-codex.accountId` |

`pyagent adopt-codex-auth` 完成这个换算（只读 codex 文件，不修改），
`--revert` 可回退。这样无需在 pi 里重新走一次 OAuth。

**取舍**：两边共用同一 refresh token，而 OpenAI 的 refresh token 是轮转式的
（pi 源码注释 "rotated token" 可证）。谁先刷新，另一边就需重新登录。
想彻底隔离，在 pi 交互界面执行一次 `/login` 单独授权即可。

## 当前进度

**第一步已完成并端到端验证通过**：

- pi 从源码构建通过（0.82.1）
- PyAgent 骨架可用：`doctor` / `login-hint` / `adopt-codex-auth` / `projects` / `models` / `ask`
- RPC 握手通过，`autoCompaction` 确认生效
- 项目扫描通过，识别 84 个 git 项目
- Codex 订阅接入成功：plan = pro，7 个模型可用
  （gpt-5.3-codex-spark / gpt-5.4 / gpt-5.4-mini / gpt-5.5 / gpt-5.6-luna / sol / terra）
- **真实模型调用通过**：模型正确应答且成功执行工具调用（`pwd`）
- **跨项目子会话通过**：在 `english-game` 目录读 README 并正确总结出
  「千小时英语 MVP / Flutter 技术栈」，证明编排器可在任意项目目录派活
- **持久会话跨进程通过**：`--session` 双轮验证（第一轮记住数字，新进程第二轮正确答出），
  这是第三步「读回上下文」能力的基础
- 单测 6/6 通过（`pyagent/tests/test_pi_rpc.py`）

## 已落地的安全加固

第一步就实现，因为编排器要在几十个项目目录里跑子 Agent：

| 加固 | 原因 |
|---|---|
| 子会话显式传 `--no-approve` | 项目本地 `.pi/extensions` 是任意 TS 代码执行通道。不依赖全局 `defaultProjectTrust` 的当前值（用户可能误设 `always`，或在父目录存过信任决定 —— 信任决定按父目录继承） |
| `--project` 限制在 `projects_root` 内 | 防 `../..` 或绝对路径把 Agent 放到 `/etc` 之类目录；逃生阀 `--allow-any-path` |
| UI 方法白名单**反转** | 改为只列 fire-and-forget 方法，其余一切（含 pi 未来新增的 dialog 方法）默认回 `cancelled`，不会漏放危险确认 |
| 凭证备份 `os.open(..., 0o600)` 原子创建 | 备份含 refresh token，默认 umask 可能让同机其他用户可读 |
| 凭证备份仅保留最近 5 个 | 无限累积会持续扩大泄漏面 |
| `model`/`session_id`/`name` 拒绝 `-` 开头 | 这些值拼进 argv，以 `-` 开头会被 pi 当选项解析（选项注入） |

**明确接受的残余风险**：pi 无内置沙箱（其 `security.md` 明示这是有意设计），
子 Agent 以当前用户权限运行；`AGENTS.md`/`CLAUDE.md` 无论信任与否都会加载，
存在 prompt injection 面（pi 官方也承认无法可靠防御）。因用户 84 个项目均为自有、
`CLAUDE.md` 是资产，故保留加载。第二步用 extension 执行前拦截落地授信边界。

## 已知的并发陷阱（后续改动务必保留）

`pi_rpc.py` 里有三处不能退化的设计，都是踩过或审出来的：

1. **UI 请求必须另起线程处理**。dialog 类请求会阻塞等人回应（飞书点按钮可能几分钟），
   若在 reader 线程同步等待，pi 的 stdout 缓冲区会填满并把 pi 卡死。
2. **stdin 写入必须持 `_write_lock`**。UI 线程与主线程并发写会让两条 JSON 交错破坏协议。
   `close()` 也走这把锁，避免写入途中 fd 被关。
3. **`ask()` 开头必须 `drain_events()`**。`_events` 是会话级共享队列，上一轮遗留的
   `agent_settled` 会让本轮立即误判"已跑完"并返回上一轮答案。复用会话时必然踩。
4. **终止信号不走队列**。`_finished` 是 `threading.Event`；`__closed__` 走有界队列，
   极端情况下会被丢弃，若靠它判终止则 `events()` 永久阻塞。

**后续**：

- 第二步：飞书接入（`lark-oapi` WebSocket 长连接，免公网 IP），实现 `extension_ui_request` → 卡片按钮闭环
- 第三步：编排能力（派活、进度聚合、待决策队列、跨会话上下文还原）
- 第四步：Computer Use 与自动巡检续推

## 环境注意事项

- pi 要求 node >= 22.19.0。本仓库用 `.nvmrc` 锁定 22.23.1。
  安装该版本导致 nvm 的 `default -> 22` 别名解析从 22.18.0 变为 22.23.1（22 系 LTS 补丁升级）。
  若需钉回：`nvm alias default 22.18.0`。
- `npm install --ignore-scripts`（README 供应链要求）会漏装 tsgo 的平台包，
  需补 `npm install --no-save --ignore-scripts @typescript/native-preview-darwin-arm64@<版本>`。
- 本机无任何 LLM API Key 环境变量，登录后 pi 只会走 Codex 订阅额度。

## 与参考方案的关系

调研过的对标项目：

- [cyhhao/vibe-remote](https://github.com/cyhhao/vibe-remote)（现 Avibe）：Python + 飞书 WebSocket，
  最贴近的 IM 接入范式。但它是**遥控器**（直接转发 prompt，无中间推理循环），不是编排器。
- [chenhg5/cc-connect](https://github.com/chenhg5/cc-connect)：Go，飞书/企微免公网 IP 接入范式。
- [earendil-works/pi-chat](https://github.com/earendil-works/pi-chat)：pi 官方 chat 扩展，
  但只支持 Discord/Telegram，且依赖 QEMU + Gondolin micro-VM，对本场景过重。

结论：IM 接入借鉴前两者做法，编排层自建，Agent 引擎用 pi。
