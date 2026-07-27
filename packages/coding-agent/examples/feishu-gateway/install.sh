#!/usr/bin/env bash
set -euo pipefail

LARK_VERSION="1.0.77"
LARK_SOURCE_COMMIT="a7865cd0a7416655535517a2a630848fde318761"
LARK_ARCHIVE_SHA256="d82c4a864ebd1e2a2d95941aee39c34d7b91077c472d3fb5984b76826ef9f693"
LARK_BINARY_SHA256="bb663810f0eebd8c228f7673f755f4efed17fb7b3b07759f456cd9db8ccd11b0"
LARK_SOURCE_SHA256="3c58bcdffbb89d4f8c2f4dd9f9495ccc568943b1075f7345eceec5d385caf45f"
SERVICE_LABEL="dev.pi.feishu-gateway"

install_service=false
if [[ "${1:-}" == "--service" ]]; then
	install_service=true
	shift
fi
if [[ $# -ne 0 ]]; then
	echo "Usage: $0 [--service]" >&2
	exit 2
fi
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
	echo "This pinned installer currently supports macOS arm64 only." >&2
	exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin_dir="$HOME/.local/bin"
agent_dir="$HOME/.pi/agent"
vendor_dir="$agent_dir/vendor/larksuite-cli-$LARK_VERSION"
skills_dir="$agent_dir/skills"
temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/pi-feishu-gateway.XXXXXX")"
trap 'rm -rf "$temporary_dir"' EXIT

archive_name="lark-cli-$LARK_VERSION-darwin-arm64.tar.gz"
archive_path="$temporary_dir/$archive_name"
source_path="$temporary_dir/larksuite-cli-source.tar.gz"

curl -fL --proto '=https' --tlsv1.2 --max-redirs 3 \
	-o "$archive_path" \
	"https://github.com/larksuite/cli/releases/download/v$LARK_VERSION/$archive_name"
printf '%s  %s\n' "$LARK_ARCHIVE_SHA256" "$archive_path" | shasum -a 256 -c -
tar -xzf "$archive_path" -C "$temporary_dir"
printf '%s  %s\n' "$LARK_BINARY_SHA256" "$temporary_dir/lark-cli" | shasum -a 256 -c -
install -d -m 700 "$bin_dir"
install -m 755 "$temporary_dir/lark-cli" "$bin_dir/lark-cli"

curl -fL --proto '=https' --tlsv1.2 --max-redirs 3 \
	-o "$source_path" \
	"https://codeload.github.com/larksuite/cli/tar.gz/$LARK_SOURCE_COMMIT"
printf '%s  %s\n' "$LARK_SOURCE_SHA256" "$source_path" | shasum -a 256 -c -
tar -xzf "$source_path" -C "$temporary_dir"
source_root="$temporary_dir/cli-$LARK_SOURCE_COMMIT"

install -d -m 700 "$agent_dir/vendor" "$skills_dir"
if [[ -e "$vendor_dir" ]]; then
	if [[ ! -f "$vendor_dir/.source-commit" ]] || [[ "$(<"$vendor_dir/.source-commit")" != "$LARK_SOURCE_COMMIT" ]]; then
		echo "Refusing to replace unexpected vendor directory: $vendor_dir" >&2
		exit 1
	fi
else
	staging_dir="$agent_dir/vendor/.larksuite-cli-$LARK_VERSION.$$.tmp"
	install -d -m 700 "$staging_dir"
	cp -R "$source_root/skills" "$staging_dir/skills"
	printf '%s\n' "$LARK_SOURCE_COMMIT" >"$staging_dir/.source-commit"
	chmod 600 "$staging_dir/.source-commit"
	mv "$staging_dir" "$vendor_dir"
fi

installed_skills=0
for skill_dir in "$vendor_dir"/skills/lark-*; do
	skill_name="$(basename "$skill_dir")"
	if [[ "$skill_name" == "lark-demo" || ! -f "$skill_dir/SKILL.md" ]]; then
		continue
	fi
	target="$skills_dir/$skill_name"
	if [[ -L "$target" && "$(readlink "$target")" == "$skill_dir" ]]; then
		:
	elif [[ -e "$target" || -L "$target" ]]; then
		echo "Keeping existing skill: $target" >&2
		continue
	else
		ln -s "$skill_dir" "$target"
	fi
	installed_skills=$((installed_skills + 1))
done

wrapper_target="$bin_dir/pi-feishu-gateway"
wrapper_source="$script_dir/bin/pi-feishu-gateway"
if [[ -e "$wrapper_target" && ! -L "$wrapper_target" ]]; then
	echo "Refusing to replace regular file: $wrapper_target" >&2
	exit 1
fi
ln -sfn "$wrapper_source" "$wrapper_target"
chmod 755 "$wrapper_source" "$script_dir/gateway.ts"

printf 'Installed lark-cli %s and %d official skills.\n' "$LARK_VERSION" "$installed_skills"
printf 'Gateway command: %s\n' "$wrapper_target"

if [[ "$install_service" == false ]]; then
	echo "No service was installed. Configure ~/.lark-cli/pi-gateway.json first."
	exit 0
fi

config_path="$HOME/.lark-cli/pi-gateway.json"
if [[ ! -f "$config_path" ]]; then
	echo "Missing gateway config: $config_path" >&2
	exit 1
fi
"$wrapper_target" --config "$config_path" --validate

logs_dir="$agent_dir/feishu-gateway/logs"
launch_agents_dir="$HOME/Library/LaunchAgents"
plist_path="$launch_agents_dir/$SERVICE_LABEL.plist"
install -d -m 700 "$logs_dir" "$launch_agents_dir"
chmod 700 "$agent_dir/feishu-gateway" "$logs_dir"
rm -f "$plist_path"
plutil -create xml1 "$plist_path"
plutil -insert Label -string "$SERVICE_LABEL" "$plist_path"
plutil -insert ProgramArguments -json '[]' "$plist_path"
plutil -insert ProgramArguments.0 -string "$wrapper_target" "$plist_path"
plutil -insert ProgramArguments.1 -string --config "$plist_path"
plutil -insert ProgramArguments.2 -string "$config_path" "$plist_path"
plutil -insert RunAtLoad -bool YES "$plist_path"
plutil -insert KeepAlive -bool YES "$plist_path"
plutil -insert ThrottleInterval -integer 10 "$plist_path"
plutil -insert StandardOutPath -string "$logs_dir/stdout.log" "$plist_path"
plutil -insert StandardErrorPath -string "$logs_dir/stderr.log" "$plist_path"
chmod 600 "$plist_path"

service_target="gui/$(id -u)/$SERVICE_LABEL"
launchctl bootout "$service_target" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist_path"
launchctl kickstart -k "$service_target"
echo "Started LaunchAgent: $SERVICE_LABEL"
