#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
browser_home="$(mktemp -d "${TMPDIR:-/tmp}/pi-browser-e2e.XXXXXX")"
export PI_BROWSER_HOME="$browser_home"
export PI_BROWSER_HEADED=0
export PI_BROWSER_SESSION_PREFIX="pi-browser-e2e-$$"

cleanup() {
  "$script_dir/bin/pi-browser" agent close >/dev/null 2>&1 || true
  "$script_dir/bin/pi-browser" delegated close >/dev/null 2>&1 || true
  rm -rf -- "$browser_home"
}
trap cleanup EXIT

assert_equal() {
  local expected="$1"
  local actual="$2"
  local label="$3"
  if [[ "$actual" != "$expected" ]]; then
    echo "$label: expected '$expected', got '$actual'" >&2
    exit 1
  fi
}

echo "[1/5] Open the autonomous browser on the public internet"
"$script_dir/bin/pi-browser" agent open https://example.com >/dev/null
agent_title="$("$script_dir/bin/pi-browser" agent eval '() => document.title' --raw)"
assert_equal '"Example Domain"' "$agent_title" "agent title"

echo "[2/5] Write autonomous profile state"
"$script_dir/bin/pi-browser" agent localstorage-set pi-browser-e2e autonomous >/dev/null
"$script_dir/bin/pi-browser" agent close >/dev/null

echo "[3/5] Reopen and verify autonomous state persistence"
"$script_dir/bin/pi-browser" agent open https://example.com >/dev/null
agent_state="$("$script_dir/bin/pi-browser" agent eval '() => localStorage.getItem("pi-browser-e2e") ?? "missing"' --raw)"
assert_equal '"autonomous"' "$agent_state" "agent persistent state"

echo "[4/5] Verify delegated profile isolation"
"$script_dir/bin/pi-browser" delegated open https://example.com >/dev/null
delegated_state="$("$script_dir/bin/pi-browser" delegated eval '() => localStorage.getItem("pi-browser-e2e") ?? "missing"' --raw)"
assert_equal '"missing"' "$delegated_state" "delegated isolated state"

echo "[5/5] Verify browser snapshots are available to the agent"
snapshot="$("$script_dir/bin/pi-browser" agent snapshot --raw)"
if [[ "$snapshot" != *"Example Domain"* ]]; then
  echo "snapshot did not contain Example Domain" >&2
  exit 1
fi

printf 'Browser end-to-end test passed.\n'
