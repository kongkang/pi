# Pi 持续对话与飞书双向 Gateway 方案

## 背景与目标

此前需求被错误拆成了“Pi + 独立 PyAgent”。正确目标是：

> 一个长期存活的 Pi 主会话，终端和飞书只是它的双向入口；Pi 可以安排任务、持续推进，并在需要人工决策时主动通过飞书联系用户。

只有 Pi 是 Agent。外层组件只负责连接、排队、通知、权限交互和会话恢复，不再增加第二个推理 Agent。

目标结构：

```text
                    ┌─ Pi TUI（后续接入）
用户 ── 飞书 ── Gateway
                    └─ 同一个长期 Pi 主会话
                              │
                              ├─ 安排项目任务
                              ├─ 查看项目状态
                              ├─ 恢复历史上下文
                              ├─ 等待人工决策
                              └─ 启动隔离的项目子 Pi 会话
```

期望行为：

1. 用户提出需求，主 Pi 安排任务并立即回执。
2. 子会话需要补充上下文或审批时，由主 Pi/Gateway 主动联系用户。
3. 不需要人工介入时，子会话根据上下文持续执行，直到完成、明确失败或触发安全边界。

技术上必须有常驻后台进程维持飞书长连接，但它只是 Pi 的宿主或 Gateway，不是另一个 Agent。

## 实施原则

按小步验证推进，不一次性重建完整编排系统：

1. 先实现一个可持续、双向的 Pi 对话。
2. 验证体验后再审计现有 `pyagent/`：保留可复用部分，其余删除，不保留无意义兼容或冗余。
3. 项目知识、长期记忆、开发习惯优先沉淀成 Skills，再逐步接入对话和编排。
4. 最后根据真实使用反馈打磨任务编排、主动通知和多项目能力。
5. 每个重要设计决定实施前先联网搜索成熟方案，避免重复造轮子。

删除现有 PyAgent 功能前必须先列出保留/删除清单并确认，不在第一阶段修改或删除它。

## 联网调研结论

调研日期：2026-07-27。

### 首选：cc-connect

- 仓库：<https://github.com/chenhg5/cc-connect>
- 许可证：MIT
- 成熟度：约 14k GitHub Stars，有持续发布版本
- 支持：Pi、飞书/Lark WebSocket、流式回复、交互卡片、会话恢复、文件和图片

`cc-connect` 已原生实现当前 Pi 的 RPC 协议。需要使用持久 RPC 模式，而不是每轮创建一次进程的 JSON 模式：

```toml
[[projects]]
name = "pi-main"

[projects.agent]
type = "pi"

[projects.agent.options]
work_dir = "/Users/kongkang/Developer/Pi-KK"
rpc = true
mode = "default"
thinking = "medium"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "${FEISHU_APP_ID}"
app_secret = "${FEISHU_APP_SECRET}"
```

其实际运行路径为：

```text
飞书
  ↕ WebSocket
cc-connect（通信与会话管理）
  ↕ JSONL RPC
pi --mode rpc --session-id ...
  ↕
持久 Pi 会话
```

RPC 模式会转发 Pi 的标准 `extension_ui_request`：

- `ctx.ui.confirm()`
- `ctx.ui.select()`
- `ctx.ui.input()`

用户在飞书中的决定通过 `extension_ui_response` 回写给同一个 Pi 会话。这是第一阶段需要验证的核心闭环。

### 备选与参考

#### Proma

- 仓库：<https://github.com/proma-ai/Proma>
- 许可证：AGPL-3.0
- 特点：直接嵌入 Pi Agent SDK，支持 Codex OAuth、飞书长连接、会话镜像、Skills、记忆、协作任务、后台任务和主动通知。

Proma 的 `pi-agent-adapter.ts`、`feishu-bridge.ts`、消息队列和 session-to-chat 映射与长期目标高度接近，适合作为后续架构参考。但它是完整 Electron 工作台，体量较大且受 AGPL 约束，不适合作为第一阶段基础。

#### Avibe

- 仓库：<https://github.com/avibe-bot/avibe>
- 特点：本地常驻、飞书接入、Skills、后台任务、主动通知，且没有额外推理层。

产品理念与目标接近，但目前直接驱动 Claude Code、Codex 和 OpenCode，不支持以当前 Pi 作为运行时，因此只作为交互设计参考。

#### pi-chat

- 仓库：<https://github.com/earendil-works/pi-chat>
- 特点：Pi 原生扩展，支持 Telegram/Discord、持久记忆、Skills 和远程控制。

缺点是目前不支持飞书，并强制使用 Gondolin/QEMU 微型虚拟机。对当前“先验证持续对话”的目标比 `cc-connect` 更重。

## 第一阶段：最小验证

第一阶段只验证 `cc-connect + Pi RPC + 飞书`，不启用多项目编排、Cron、复杂记忆或 Computer Use。

### 验证范围

1. 安装 `cc-connect`，使用独立测试配置。
2. 只配置 `Pi-KK` 一个项目。
3. Agent 类型设为 `pi`，强制 `rpc = true`。
4. 使用当前 Pi 的 `openai-codex` 登录态和默认模型。
5. 配置飞书 WebSocket 长连接。
6. 完成端到端验证。

### 飞书要求

在飞书开放平台创建企业自建应用，并配置：

- 启用机器人能力
- 事件订阅方式：长连接
- 事件：`im.message.receive_v1`
- 回调订阅方式：长连接
- 回调：`card.action.trigger`
- 消息读取和 `im:message:send_as_bot` 等必要权限
- 发布应用版本并将机器人加入目标会话

凭证不得提交到 Git。使用环境变量或权限为 `0600` 的本地配置文件。

### 验收用例

必须逐项验证：

1. **持续上下文**：在飞书连续进行至少三轮依赖前文的对话。
2. **工具调用**：Pi 在 `Pi-KK` 中执行一次只读工具调用并返回结果。
3. **流式反馈**：飞书能看到处理中状态或流式更新，而不是长时间无反馈。
4. **双向审批**：测试扩展调用 `ctx.ui.confirm()`，飞书展示交互并把决定送回 Pi。
5. **主动回执**：长任务完成后，即使用户没有继续发消息，飞书也能收到完成通知。
6. **会话恢复**：重启 `cc-connect` 后恢复同一个 Pi session，并正确回答重启前保存的信息。
7. **中断**：从飞书中止正在执行的任务，Pi 正确停止且会话仍可继续。
8. **故障恢复**：临时断网后 WebSocket 自动重连，不重复处理同一条消息。

只有以上用例通过，才认为“持续双向对话”第一阶段完成。

## 第一阶段已知限制

`cc-connect` 可以让飞书持续连接一个 Pi RPC 会话，也可以在服务重启后恢复 session；但普通 `pi` TUI 当前不能同时附着到同一个正在运行的 RPC 进程。

第一阶段接受以下交互方式：

- 飞书是持续在线的主界面。
- 终端用于服务管理、日志和故障排查。
- 暂不让普通 Pi TUI 与飞书并发控制同一个活跃进程。

体验验证通过后，再评估实现轻量 `pi attach`、Web/TUI 客户端，或复用 `cc-connect` Management API。不能让两个独立进程同时写同一个 Pi JSONL 会话文件。

## 后续阶段

### 阶段二：审计和精简现有 PyAgent

对 `pyagent/` 做逐文件审计，按三类整理：

- **保留并迁移**：飞书适配经验、决策卡片、项目扫描、上下文恢复、状态存储、沙箱策略中仍有价值的部分。
- **由成熟方案替代**：`cc-connect` 已可靠实现的连接、消息队列、RPC 生命周期和会话恢复。
- **删除**：独立 PyAgent 人格、重复 CLI、未接入主会话的编排骨架，以及没有真实使用价值的抽象。

先提交清单并确认，再删除代码。

### 阶段三：Skills 与知识沉淀

优先建立少量可验证的 Skills：

- 用户日常开发习惯
- 项目目录和关联关系
- 项目进度查询
- 跨项目只读咨询
- 决策前上下文恢复
- 任务完成判定和汇报格式

Skills 采用渐进披露，只把名称和触发描述常驻上下文，具体流程按需读取。

### 阶段四：轻量编排

在真实对话需求稳定后，再增加：

- 主 Pi 可调用的 `start_task`、`task_status`、`resume_task` 工具
- 每个任务绑定独立项目目录和子 Pi session
- 子会话事件回流主会话
- 缺少信息时向飞书升级
- 无需人工时自动推进到 `agent_settled`
- 主动完成/失败通知

主 Pi 是唯一顶层 Agent；项目子 Pi 是隔离执行会话，不形成第二套产品或人格。

## ask-project 现状

现有 Skill：

```text
~/.claude/skills/ask-project/SKILL.md
~/.codex/skills/ask-project/SKILL.md
```

它负责定位目标项目，然后委托 `ask-codex` 在目标目录使用 `read-only` 沙箱查询，并返回基于代码的证据。当前阶段可继续用它并行了解多个项目。

当前 Pi 不会自动发现 `~/.claude/skills` 或 `~/.codex/skills`。后续可将 `ask-project` 与依赖的 `ask-codex` 迁移或链接到 Pi 支持的位置：

```text
~/.pi/agent/skills/
~/.agents/skills/
```

迁移时应保留跨项目只读边界，并检查其中 Claude/Codex 专用命令是否需要适配 Pi。

## 下一台电脑继续工作的起点

1. 拉取 `pi-kk` 分支。
2. 阅读本文，不立即修改 `pyagent/`。
3. 检查本机 `pi --version`、`pi --mode rpc` 和 `openai-codex` 登录态。
4. 安装并审查 `cc-connect` 的稳定版本。
5. 创建只包含 `Pi-KK + Pi RPC + 飞书` 的最小测试配置。
6. 由用户完成飞书凭证和应用发布步骤。
7. 按“验收用例”逐项实测并记录结果。
8. 验证通过后再讨论 PyAgent 保留/删除清单。

## 决策记录

- 不继续把 PyAgent 作为独立 Agent 产品开发。
- 第一阶段优先复用 `cc-connect`，不自建飞书 Gateway。
- Proma 作为 Pi SDK + 飞书双向会话的重点参考实现。
- 所有重要架构决策默认先联网搜索成熟方案。
- 当前阶段不删除代码、不重建完整编排、不提前设计复杂记忆系统。
