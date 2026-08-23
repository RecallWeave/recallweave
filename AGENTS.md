# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd prime` for full workflow context.

> **Architecture in one line:** Issues live in a local Dolt database
> (`.beads/dolt/`); cross-machine sync uses `bd dolt push/pull` (a
> git-compatible protocol), stored under `refs/dolt/data` on your git
> remote — separate from `refs/heads/*` where your code lives.
> `.beads/issues.jsonl` is a passive export, not the wire protocol.
>
> See [SYNC_CONCEPTS.md](https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md)
> for the one-screen overview and anti-patterns (don't treat JSONL as the
> source of truth; don't `bd import` during normal operation; don't
> reach for third-party Dolt hosting before trying the default).

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work atomically
bd close <id>         # Complete work
bd dolt push          # Push beads data to remote
```

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Commit and integrate per the Git Cadence below. Do not push to protected branches, open PRs, or merge without explicit approval. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Git Cadence

Approved standing policy. The goal: eliminate local-only backlog while keeping
review milestone-based. Josh's queue should hold genuine decisions, not routine
requests to back up clean, validated commits.

**Topology**
- `main` is **protected**. It is never pushed to, merged into, or auto-merged
  locally. Local `main` mirrors `origin/main` and nothing else.
- `foundry/task-contracts` is the **active integration branch** — the durable,
  continuously updated remote checkpoint for current work.
- Workers commit to their own `ntm/<session>/oc_N` branches in their worktrees.

**Routine cycle (automatic, no approval needed)**
```
worker commits
  -> coordinator integrates into the integration branch
  -> dependency sync
  -> full local test/gate suite
  -> refresh Beads/BV state (and commit the passive export churn)
  -> update clean worktrees
  -> push the integration branch to origin
  -> continue
```
Push only when ALL hold: suite green, conflicts resolved, tree clean, no open
bead labelled `blocker` or `needs-human`. Otherwise do not push — fix first.

**Milestone cycle (requires Josh)**
```
integration branch -> PR to protected main -> required review
  -> remediation -> human merge approval
```
Open or update a PR only at a meaningful review checkpoint, release gate, or
integration milestone — never as part of the routine cycle.

**Review verdicts: what they gate**

A failing or absent adversarial review does **not** block a checkpoint push. The
checkpoint exists purely for durability and recovery, and stranding validated
commits on one machine is the risk it removes.

A failing or absent review **does** block every one of: promotion, merge,
release, deploy, and milestone PR. Those need a PASS verdict *and* human
approval.

**Checkpoint branches are marked non-approved**

The integration branch carries `CHECKPOINT_NOT_APPROVED.md` at its root, naming
the branch and the latest review verdict. The supervisor refreshes it before
every push and refuses to push an unmarked branch, so the marker cannot drift or
go missing. Delete it as part of the promotion commit — its presence is the
machine-checkable signal that the branch is not approved.

Checkpoint branches must never be auto-mergeable: no auto-merge, no non-draft PR
opened by automation, no `gh pr merge`. The supervisor refuses `gh pr
merge|create|ready`, `gh release`, and any `--auto` flag outright; read-only `gh`
(list, view) still works.

**Never**
- push or merge to protected `main`/`master`
- auto-merge a PR, or let a checkpoint branch become auto-mergeable
- promote, release, or deploy on a non-PASS review
- push broken or dirty integration state
- push an integration branch missing its non-approved marker
- force-push without explicit approval
- push worker branches individually, except for recovery, debugging, or a
  specific review workflow

Automation lives in `/Users/josh/particle-workers/supervisor/rw_supervisor.py`
(`push_integration`), which refuses protected targets, never forces, and stops
retrying after two consecutive push failures.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->

<!-- BEGIN BEADS CODEX SETUP: generated by bd setup codex -->
## Beads Issue Tracker

Use Beads (`bd`) for durable task tracking in repositories that include it. Use the `beads` skill at `.agents/skills/beads/SKILL.md` (project install) or `~/.agents/skills/beads/SKILL.md` (global install) for Beads workflow guidance, then use the `bd` CLI for issue operations.

### Quick Reference

```bash
bd ready                # Find available work
bd show <id>            # View issue details
bd update <id> --claim  # Claim work
bd close <id>           # Complete work
bd prime                # Refresh Beads context
```

### Rules

- Use `bd` for all task tracking; do not create markdown TODO lists.
- Run `bd prime` when Beads context is missing or stale. Codex 0.129.0+ can load Beads context automatically through native hooks; use `/hooks` to inspect or toggle them.
- Keep persistent project memory in Beads via `bd remember`; do not create ad hoc memory files.

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.
<!-- END BEADS CODEX SETUP -->
