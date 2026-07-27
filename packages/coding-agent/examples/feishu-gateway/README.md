# Feishu Gateway

A small, single-user bridge between the official [`larksuite/cli`](https://github.com/larksuite/cli) event stream and Pi RPC mode.

```text
Feishu WebSocket -> lark-cli -> allowlist -> Pi RPC -> lark-cli -> Feishu
```

## Security model

The gateway requires all of these checks before forwarding a message to Pi:

- `im.message.receive_v1` event
- direct chat (`p2p`), never a group
- exact `chat_id`
- sender `open_id` in `allow_from`
- user sender, never a bot

`admin_from` must be a subset of `allow_from` and controls `/new` and `/status`. Processed message IDs are persisted, and outgoing messages use idempotency keys. The private config and state directories use owner-only permissions.

Pi still has its configured tools and is not an OS sandbox. Keep the Feishu app availability restricted to the intended member, use bot-only lark-cli mode, and use `approve_project: false` unless the workspace is trusted.

RPC extension dialogs are denied/cancelled because this headless gateway cannot safely present interactive approval UI.

## Requirements

- macOS arm64
- Node.js 22.19 or newer
- a published Feishu custom app with Bot enabled
- app scopes `im:message.p2p_msg:readonly` and `im:message`
- long-connection event `im.message.receive_v1`

The gateway adds a temporary `Typing` reaction while Pi is processing, removes it after delivery, and appends the elapsed processing time to each reply. Reaction failures are best-effort and never block the reply.

## Install

From the repository root:

```bash
nvm install
packages/coding-agent/examples/feishu-gateway/install.sh
```

The installer:

- installs the pinned official `lark-cli 1.0.77` binary after SHA-256 verification
- installs 27 official `lark-*` skills from verified source commit `a7865cd0a7416655535517a2a630848fde318761`
- links `pi-feishu-gateway` into `~/.local/bin`
- does not install dependencies or run lifecycle scripts

It does not write credentials or start a service.

## Configure lark-cli

Bind an existing app without putting its secret in shell arguments:

```bash
read -r -s APP_SECRET
printf '%s' "$APP_SECRET" | lark-cli config init \
  --app-id cli_REPLACE_ME \
  --app-secret-stdin \
  --brand feishu
unset APP_SECRET

lark-cli config strict-mode bot
lark-cli config default-as bot
lark-cli auth status --json --verify
```

## Discover the private chat IDs

Start a bounded listener, then send the bot a direct message:

```bash
lark-cli event consume im.message.receive_v1 \
  --as bot \
  --max-events 1 \
  --timeout 10m \
  --jq 'select(.chat_type=="p2p" and .sender_type=="user") | {chat_id, sender_id}'
```

Copy the example config and fill in the App ID, sender `open_id`, chat ID, and workspace path. Set `pi.command` to `pi` for an installed CLI, or to the repository's absolute `pi-test.sh` path for a source checkout:

```bash
install -d -m 700 ~/.lark-cli
install -m 600 \
  packages/coding-agent/examples/feishu-gateway/config.example.json \
  ~/.lark-cli/pi-gateway.json
$EDITOR ~/.lark-cli/pi-gateway.json

pi-feishu-gateway --validate
```

Do not commit `~/.lark-cli/pi-gateway.json`, the App Secret, tokens, or session state.

## Run

Foreground:

```bash
pi-feishu-gateway
```

Process one authorized message and exit, useful for testing:

```bash
pi-feishu-gateway --once
```

Commands available in the private chat:

- `/status` — show the active Pi model and session
- `/new` — start a fresh Pi session

## LaunchAgent

After configuration succeeds, install and start the per-user service:

```bash
packages/coding-agent/examples/feishu-gateway/install.sh --service
```

Logs are stored under `~/.pi/agent/feishu-gateway/logs/`. Stop and remove the service:

```bash
launchctl bootout "gui/$(id -u)/dev.pi.feishu-gateway"
rm ~/Library/LaunchAgents/dev.pi.feishu-gateway.plist
```

## Limits

- one allowlisted direct chat per gateway process
- replies are plain text and split into 20,000-character chunks
- each processed message uses one reaction-create and one reaction-delete request; Feishu documents a limit of 1,000 requests/minute and 50 requests/second per API, application, and tenant
- attachments and Feishu interactive cards are not yet bridged to Pi
- changing app permissions or events may require publishing a new Feishu app version
