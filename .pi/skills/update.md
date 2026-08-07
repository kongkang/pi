---
name: update
description: Sync the Pi-KK fork with upstream pi and upgrade to the latest version without overwriting local modifications. Use when the user wants to pull in upstream changes, update pi to a new version, or run /update.
---

# Update Pi-KK Fork from Upstream

This skill merges the latest `upstream/main` (earendil-works/pi) into the local
`pi-kk` branch while preserving local customizations (CloudOS provider, Feishu
gateway, pyagent orchestration, browser environments, changelog edits, etc.).

Git's three-way merge keeps local-only changes intact and only touches what
upstream changed. Conflicts are never auto-resolved with force flags; the skill
stops and reports so a human or the agent can resolve them.

## Safety rules (do not violate)

- Never run `git reset --hard`, `git checkout .`, `git clean -fd`, `git stash`,
  or any force/overwrite operation.
- Never auto-resolve conflicts with `-X theirs` / `-X ours`. Stop and resolve
  manually, preserving local intent.
- Never commit unless the user asks.
- Multiple pi sessions may run in this cwd. Only stage/commit files relevant to
  this update.

## Workflow

### 1. Preflight

- Verify the current branch is `pi-kk`:
  ```bash
  git branch --show-current
  ```
  If not on `pi-kk`, stop and ask the user.
- Check the working tree for uncommitted changes:
  ```bash
  git status --porcelain
  ```
- If there are uncommitted changes, do NOT proceed silently. Show them to the
  user and ask whether to (a) commit them first, (b) leave them (they will be
  carried into the merge if non-conflicting), or (c) proceed as-is. If a file
  with uncommitted changes also changed upstream, the merge will refuse or
  conflict; resolve that by committing the local change first.

### 2. Fetch upstream

```bash
git fetch upstream
```

### 3. Report what's new

Summarize the delta without changing the working tree:

```bash
# how far behind
git rev-list --count pi-kk..upstream/main
# upstream commits not yet in pi-kk
git log --oneline pi-kk..upstream/main
# local commits that will be preserved
git log --oneline upstream/main..pi-kk
```

Show the user the count and a short list of notable upstream commits, and
confirm they want to proceed before merging.

### 4. Merge upstream/main into pi-kk

```bash
git merge upstream/main -m "Merge remote-tracking branch 'upstream/main' into pi-kk"
```

- If the merge succeeds cleanly, proceed to step 6.
- If the merge stops due to conflicts, go to step 5.

### 5. Resolve conflicts (merge in progress)

Do NOT abort blindly. List conflicts:

```bash
git status
git diff --name-only --diff-filter=U
```

For each conflicted file:

- Open it and inspect the conflict markers.
- Decide which side to keep. Local customizations take precedence where the
  change is intentional; otherwise take upstream's newer logic and re-apply the
  local customization on top.
- Use `git add <file>` to mark resolved.
- After resolving all, run `git status` to confirm nothing is unmerged, then
  complete the merge with `git commit` (only if the user wants it committed).

### 6. Regenerate models (if generate-models.ts changed upstream)

If upstream touched `packages/ai/scripts/generate-models.ts`, regenerate:

```bash
node --run -C packages/ai generate-models
```

(replace with the actual script name from `packages/ai/package.json` if
different). Commit the resulting `models.generated.ts` together with the merge.

### 7. Verify

- Install/hydrate deps if the lockfile changed:
  ```bash
  npm install --ignore-scripts
  ```
- Run the full check:
  ```bash
  npm run check
  ```
- Fix any errors/warnings/infos introduced by the merge, preserving local
  behavior.

### 8. Report

Summarize for the user:

- New upstream version pulled in (e.g. from `git log --oneline -1 upstream/main`).
- Any conflicts encountered and how they were resolved.
- Whether local customizations were preserved (list them).
- Any follow-up actions needed (commit, push, etc.).

Never push or commit unless the user explicitly asks.