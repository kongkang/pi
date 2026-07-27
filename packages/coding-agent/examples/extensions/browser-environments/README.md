# Pi browser environments

This extension gives pi full control of two visible, persistent local Chrome environments through the official Playwright CLI.

| Environment | Purpose | Data directory |
| --- | --- | --- |
| `agent` | Autonomous browsing with dedicated agent accounts | `~/.pi/agent/browser/profiles/agent` |
| `delegated` | Temporary user login for explicitly delegated tasks | `~/.pi/agent/browser/profiles/delegated` |

The profiles do not reuse the user's normal Chrome profile. Browser data and command output stay outside the Git repository with owner-only directory permissions.

## Install

```bash
packages/coding-agent/examples/extensions/browser-environments/install.sh
```

The installer:

- installs the pinned `@playwright/cli@0.1.17` package with npm lifecycle scripts disabled;
- creates private profile and output directories;
- links the extension into `~/.pi/agent/extensions/browser-environments`;
- links `pi-browser` into `~/.pi/agent/bin`.

Run `/reload` in an existing pi session after installation.

## User commands

```text
/browser-open agent https://example.com
/browser-open delegated https://example.com/login
/browser-status
/browser-dashboard
/browser-reset delegated
```

The delegated browser is also a visible Chrome window. The user should type passwords, passkeys, MFA codes, and other credentials directly into that window.

## CLI

The extension's `browser` tool calls the same wrapper available to shell users:

```bash
pi-browser agent open https://example.com
pi-browser agent snapshot
pi-browser agent click e4
pi-browser delegated open https://example.com/login
pi-browser status
```

The wrapper fixes the browser channel, profile directory, session name, and output directory for each environment. Commands that attach another Chrome instance or globally destroy sessions are blocked to preserve isolation.

Set `PI_BROWSER_HOME` to override the data root. Set `PI_BROWSER_HEADED=0` for automated tests only.

## Test

```bash
packages/coding-agent/examples/extensions/browser-environments/test-e2e.sh
```

The test opens the public internet, verifies that agent state survives a browser restart, confirms that the delegated profile cannot see agent state, and validates accessibility snapshots.

## Remove

Close browser sessions, then remove the installed links if browser access should no longer load globally:

```bash
pi-browser agent close
pi-browser delegated close
rm ~/.pi/agent/extensions/browser-environments
rm ~/.pi/agent/bin/pi-browser
```

Browser profile data is intentionally retained. Delete it only through `/browser-reset` or an explicit manual cleanup.
