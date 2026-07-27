#!/usr/bin/env bash
set -euo pipefail

playwright_cli_version="0.1.17"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
extension_link="$agent_dir/extensions/browser-environments"
bin_link="$agent_dir/bin/pi-browser"
browser_root="${PI_BROWSER_HOME:-$agent_dir/browser}"

link_resource() {
  local source="$1"
  local destination="$2"
  if [[ -e "$destination" && ! -L "$destination" ]]; then
    echo "refusing to replace non-symlink: $destination" >&2
    exit 1
  fi
  ln -sfn "$source" "$destination"
}

echo "Installing @playwright/cli@$playwright_cli_version with lifecycle scripts disabled..."
npm install --global --ignore-scripts "@playwright/cli@$playwright_cli_version"

if [[ "$(playwright-cli --version)" != "$playwright_cli_version" ]]; then
  echo "unexpected playwright-cli version: $(playwright-cli --version)" >&2
  exit 1
fi

if [[ "$(uname -s)" == "Darwin" && ! -d "/Applications/Google Chrome.app" ]]; then
  echo "Google Chrome is required at /Applications/Google Chrome.app" >&2
  exit 1
fi

umask 077
install -d -m 700 \
  "$agent_dir/bin" \
  "$agent_dir/extensions" \
  "$browser_root" \
  "$browser_root/profiles" \
  "$browser_root/profiles/agent" \
  "$browser_root/profiles/delegated" \
  "$browser_root/output" \
  "$browser_root/output/agent" \
  "$browser_root/output/delegated"
chmod 700 \
  "$browser_root" \
  "$browser_root/profiles" \
  "$browser_root/profiles/agent" \
  "$browser_root/profiles/delegated" \
  "$browser_root/output" \
  "$browser_root/output/agent" \
  "$browser_root/output/delegated"

chmod +x "$script_dir/bin/pi-browser"
link_resource "$script_dir" "$extension_link"
link_resource "$script_dir/bin/pi-browser" "$bin_link"

cat <<EOF
Installed browser environments.

Agent profile:     $browser_root/profiles/agent
Delegated profile: $browser_root/profiles/delegated
Browser command:   $bin_link
Pi extension:      $extension_link

Run /reload in an existing pi session, or start a new session.
EOF
