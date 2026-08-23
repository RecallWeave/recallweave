# Swarm Operations Runbook — elevare-agent-factory

> **GENERATED FILE — do not edit by hand.**
> Rendered by `eaf runbook` from `templates/SWARM-RUNBOOK.md` plus the source contract.
> Every operational fact below is derived from the code that implements it, so it cannot
> drift: `eaf runbook --check` fails the suite when this file and the source disagree.
> The one hand-authored part is **§11 Project-specific overrides**, which is preserved
> verbatim across re-renders. Edit that section; regenerate everything else.

**Read this before taking control of the swarm.** It is the whole operating doctrine.
You are not expected to reconstruct it from transcripts, and you should not invent
your own conventions alongside it.

| | |
|---|---|
| Project | `elevare-agent-factory` |
| Integration branch | `foundry/checkpoint-1` |
| Protected (never pushed to) | `main master` |
| Workers | 4 × `opencode` running `particle/dsv4-flash` |
| Trust tier | `PUBLIC` |

---

## 1. Operating model

Three actors, with different authority. Confusing them is the most common way a session
goes wrong.

- **Workers** hold beads, edit only inside their own worktree, and commit to their own
  lane branch. A worker never integrates, never pushes, and never answers a permission
  dialog.
- **The coordinator** (supervisor daemon) owns the routine loop: integrate → gate →
  rotate → nudge → push → dispatch. It is bounded by per-cycle budgets and refuses
  rather than guesses. It holds push authority. It never merges to a protected branch.
- **The planner** (you, or whichever harness has the lead) owns architecture, scope,
  exception handling and approvals. **The planner does not do the coordinator's job
  while the coordinator is running.** Hand-driving dispatch in parallel with an active
  supervisor produces double-dispatch and lost work.

The single rule that generalizes: **an unknown is never a safe value.** Every probe,
gate and query in this system either returns a verified answer or refuses. If you add
one, it must fail closed.

---

## 2. Supervisor tick order

One cycle, every **60s**, in this order. Derived from the coordinator's own
phase markers:

1. **integrate finished work (conflicts escalate, never auto-resolved)**
2. **gate after integration; a red suite stops further action**
3. **inspect + act per worker**
3a. **PUSH the integration branch when the cycle is genuinely clean**
3b. **DISPATCH: existing approved ready beads only**
4. **heartbeat (cycle complete)**

Per-cycle budgets cap blast radius: at most **4** integrations,
**2** dispatches, **1** rotation. A phase that cannot
determine its inputs refuses that phase for the tick rather than proceeding on partial
knowledge.

**Pausing.** `touch .eaf/coordinator/PAUSED` is the whole mechanism — it is checked
before every tick. The daemon keeps running and journals `paused`; it does not act.
Verify it is genuinely inert by reading the journal, not by trusting the file:

```
tail -5 .eaf/coordinator/journal.jsonl
```

**A resident daemon runs the code it started with.** Editing the coordinator changes
nothing until the job restarts. After any coordinator change:

```
launchctl kickstart -k gui/$(id -u)/com.elevare.eaf.coordinator
```

Check the daemon's start time against your fix's commit time before concluding a fix is
live. This has produced false "fixed" reports.

---

## 3. Dispatch admission checks

A bead is dispatched only when **all 11** checks pass. Any check that cannot
be evaluated refuses the dispatch — an UNKNOWN never admits. Derived from the
coordinator's numbered checks:

- **check 1** — dependency readiness
- **check 2** — trust-tier admission
- **check 3** — file-set concurrency safety
- **check 4** — context-health admission
- **check 5** — ownership metadata KNOWN (fails closed on an unpredictable file set)
- **check 6** — permission hold pending
- **check 7** — project not paused
- **check 8** — this coordinator still holds the lease
- **check 9** — refresh the target lane from the integration branch before
- **check 10** — every path the bead's file set names must EXIST in the target
- **check 11** — RUNTIME ENFORCEMENT (bead elevare-agent-factory-6tu)

Two consequences worth stating plainly, because both have caused stalls:

- A worker whose **runtime enforcement is not verifiably in force** is not eligible for
  dispatch. After a plugin or policy change, workers must be **rotated** to pick it up;
  until then they are correctly refused.
- A lane that cannot be safely refreshed from the integration branch is refused rather
  than silently rebased.

---

## 4. Worker lifecycle and context rotation

Bands are read from the pane and are thousands of context tokens:

| Band | Threshold | Meaning |
|---|---|---|
| Warning | 100000 | Prepare handoff; keep new work small |
| Checkpoint | 130000 | Land what is in flight; do not start anything large |
| Mandatory rotate | 140000 | Rotate at the next clean boundary |

The coordinator rotates at **140K** and stops admitting work at
**130K**. An idle worker holding a claim is nudged after
**120s** of no observable progress.

**Rotation refuses on a dirty tree, and that refusal is correct** — respawning kills the
agent and any uncommitted work with it. So the sequence for a wedged-but-dirty worker is
always **preserve first, then rotate**:

1. Read the diff and decide whether it is authored work.
2. Commit it to the lane branch, or preserve it as a tag off-branch if it is incomplete
   and must not be integrated as though finished.
3. Then rotate.

**Degeneration is not purely a context problem.** Workers have looped at low context as
well as high. The reliable signals are a repeated line or repeated identical tool call,
a frozen context figure, and the runtime's "continue after repeated failures" dialog.
The response is **rotation, not a nudge** — allowing that dialog resumes the loop.

**Do not poll workers by hand while the coordinator is running.** It already owns
nudging and rotation, and two actors nudging the same pane produces duplicate work.

---

## 5. Integration rules

- Integration merges worker lanes into `foundry/checkpoint-1` **only when the root
  worktree is actually on that branch**. Any other checkout is refused rather than
  silently mutated.
- A red gate suite **rolls the integration back** and preserves the worker branch.
  Conflicts escalate; they are never auto-resolved.
- Review a lane with a **merge-base** diff (`git diff <branch>...<lane>`), never two-dot.
  Two-dot shows staleness as thousands of spurious deletions and has caused false alarms.
- **Closed ≠ integrated.** A bead can read closed while its commit is absent from the
  integration branch. Verify against the branch, never against bead status.
- Generated files (`.beads/`) are not authored work. They are excluded from dirty checks
  and staged explicitly — never with `git add -A`.

### Push preconditions

The integration branch is pushed only when **all** of these hold. An UNKNOWN on any of
them counts as a failure, never a pass:

1. AFFIRMATIVE gate result bound to the exact HEAD being pushed
2. no unresolved conflicts
3. integration tree clean
4. no blocking review issue open

Refused unconditionally regardless of preconditions: protected branches
(`main master`), merges to protected branches, PR auto-merge, force-push,
a dirty tree, and pushing individual worker branches.

---

## 6. Permission boundaries

- Deny-by-default. **22** canonical patterns live in `policy/deny.patterns` and
  are parsed by exactly one parser (`lib/eaf-deny-patterns.zsh`). Never add a second copy
  or a second parser.
- Allowed scopes are the worker's own worktree, its `.tmp/`, and an explicit reviewed
  allow-list that **ships empty**. Everything else is denied or held.
- Enforcement is at the **tool-execution hook**, before the call runs — not in the brief.
  Prose is not a guard: this project prohibited `/tmp` in three documents and four
  workers used it anyway. Every denial is journalled to `.eaf/enforcement/denials.jsonl`.
- Structural denials (a peer lane, a sibling repository, the home tree) are enforced by
  position, not pattern.
- **Never answer a worker's permission dialog on its behalf.** "Allow always" is never
  the answer. Commit the work and rotate.

---

## 7. Planner lease and failover

- One planner holds the lead. State that survives you lives in three places and nowhere
  else: **the repository**, **Beads**, and the **handoff document**. Transcripts are not
  state.
- Write handoff state **continuously, not at the end**. The planner cannot measure its own
  context, so its degradation is unobservable and its failure is unannounced.
- Record mechanisms and pointers, not volatile numbers. A handoff that contradicts itself
  is worse than one that is plainly stale.
- On resume: read the handoff, then verify it against live state before acting on it. Any
  un-rechecked read of live state is a hypothesis.

---

## 8. Escalation rules

Escalate a **genuine human decision**: a policy change, a trust-tier or scope question, an
approved-contract change, a destructive or outward-facing action, or a spec that no worker
can satisfy under its permissions.

Do **not** escalate what you can verify yourself. Reproduce before filing.

A worker that stops because it **refused to fabricate** looks identical to one that died.
Distinguish them before acting: read the bead notes and the pane. A refusal is a result.

Escalations are recorded durably in the coordinator journal and on the bead. Treat an
escalation that nothing consumes as an open problem, not as a delivered signal.

---

## 9. Review routing

- Local gate first: the full suite must be green **at the exact HEAD** being reviewed. A
  pass ages the instant the tree changes.
- Automated review runs against the pushed head. **Read inline comments through the API,
  not the review body** — the body carries only boilerplate:

```
gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate
```

- Check the SHA a review actually covered; a review can report an older commit than the
  current head and pass as a clean gate.
- Review comments are **re-anchored** onto the current head, so grouping by commit
  overstates what is new. Separate rounds by `created_at`.
- **Verify a finding against HEAD before dispatching it.** Findings are filed against the
  SHA they were found on and may already be remediated; re-fixing live code wastes a
  worker cycle and produces a confusing no-op diff.
- Every finding becomes a bead with file:line and a remediation, and blocks the push
  precondition until closed.

---

## 10. Recovery and resume

Enter the repository and run, in this order:

```
bd ready
zsh bootstrap/eaf-fleet-status.zsh     # workers, context bands, dirty/ahead, dialogs
zsh bootstrap/eaf-fleet-check.zsh      # daemon health, lease, manifest-vs-live, push gates
```

Both are read-only. Then, before acting:

1. **Verify the daemon's real state** — running is not the same as acting. Read the
   journal for the last few ticks.
2. **Check each lane for uncommitted work** before any rotation or reset. Workers finish
   work and go idle without committing; that work is invisible to integration and is
   destroyed by a careless rotate.
3. **Confirm identity from the manifest**, not from pane position. Verify pane liveness by
   `list-panes` membership, never by a command that resolves a dead pane to a default.
4. Only then integrate, rotate, or dispatch.

Never resolve a collision by discarding. `git reset --hard`, `git checkout .` and their
relatives are not collision-resolution mechanisms; preserve the other side first.

---

## 11. Project-specific overrides

> This section is **authored, not generated**, and is preserved verbatim when the runbook
> is re-rendered. Everything outside it is regenerated and will be overwritten.
> Record local deviations here with a reason and a date.

<!-- EAF:PROJECT-OVERRIDES:BEGIN -->

**Generated body describes a different project — read this section first.**
`eaf runbook` renders from the Agent Factory's own contract and has no
project-awareness; `--out` redirects the file but not the content. Everything
above was rendered for `elevare-agent-factory`, and every entry in its "Sources
of truth" table (`bootstrap/eaf-coordinator.zsh`, `lib/eaf-permit.zsh`,
`policy/deny.patterns`, `lib/eaf-context-health.zsh`, `policy/trust-tiers.toml`,
`docs/PERMISSION-POLICY.md`, `bootstrap/WAVE0-BRIEF.md`) is **absent from this
repository** — verified 2026-08-23. Treat the doctrine as authoritative and the
project facts as overridden here until the generator takes a project argument.

| Fact | Runbook says | This project | Recorded |
|---|---|---|---|
| Project | `elevare-agent-factory` | `recallweave` | 2026-08-23 |
| Integration branch | `foundry/checkpoint-1` | **`foundry/steward`** | 2026-08-23 |
| Coordinator | `bootstrap/eaf-coordinator.zsh` | `/Users/josh/particle-workers/supervisor/rw_supervisor.py` (launchd `com.particle.rw-supervisor`) | 2026-08-23 |
| Tick interval | 60s | **300s** | 2026-08-23 |
| Workers | 4 lanes | **8 lanes** `oc_1`..`oc_8`, tmux session `recallweave` | 2026-08-23 |

**Context-health rotation bands are enforced via `ntm status --json`.** The doctrine
specifies warning/checkpoint/mandatory bands at 100K/130K/140K. As of 2026-08-23
`rw_supervisor.py` probes `context_tokens` each tick, rotates idle lanes at the
mandatory band (refusing dirty worktrees), and withholds dispatch from lanes in
the checkpoint or rotate bands. An unavailable probe fails closed and holds
dispatch rather than assuming healthy.

**Standing project decisions that supplement the doctrine.** These predate the
runbook and continue to win where they differ:

- **Retired checkpoint branches are unreachable by name.** `foundry/task-contracts`
  was promoted to `main` (PR #1, 2026-08-23) and is historical. Merging a retired
  branch into current work is prohibited: a worker did exactly that, replaying 170
  already-squashed commits onto a fresh checkpoint. `CLAUDE.md` and `AGENTS.md`
  carry the rule and the precedence tie-break (**the branch you were dispatched
  onto wins over a name you remember**). (2026-08-23)
- **Squash is the promotion mechanism.** `main` enforces linear history and feature
  branches carry merge commits, so merge-commit and rebase are both refused.
  Implementation lineage is preserved on the retired branch and in the PR.
  (2026-08-23)
- **Promotion invalidates ancestry for every derived branch.** After a squash
  promotion, every worker lane must be verified to hold zero commits absent from
  the retired branch (`git rev-list --count <retired>..<lane>` = 0) and then reset
  onto the new checkpoint. Skipping this makes lanes read 50-107 commits "ahead"
  of a branch cut from the squash. (2026-08-23)
- **Local gate parity is not assumed.** The merge gate is the branch-protection
  required-check set (Windows x3, macOS x3, Linux x3, `viewer`), not the local
  suite. A green local suite is not evidence about platforms it does not run:
  three tests errored on Windows for ~17 review cycles while passing locally.
  (2026-08-23)
- **Two independent reviewers, not one.** The Codex CLI gate reviews the working
  tree; the GitHub PR reviewer reviews the diff. They find different classes — the
  CLI gate passed a tree whose exclusion breach the PR reviewer caught. Neither
  alone is sufficient for promotion. (2026-08-23)
- **Deferred, not `needs-human`, for undispatchable-but-durable work.** In this
  coordinator `needs-human` blocks dispatch *and* the push gate, so using it to
  park work freezes checkpoints. `recallweave-z1a` is deferred instead.
  (2026-08-23)
<!-- EAF:PROJECT-OVERRIDES:END -->

---

## Sources of truth

This runbook summarizes; it does not replace. When a detail matters, read the source:

| Topic | Source |
|---|---|
| Tick order, dispatch checks, push preconditions | `bootstrap/eaf-coordinator.zsh` |
| Permission decision function | `lib/eaf-permit.zsh` |
| Deny patterns (canonical, single copy) | `policy/deny.patterns` |
| Runtime enforcement | `.opencode/plugin/eaf-enforce.js`, `libexec/eaf-enforce-decide.zsh` |
| Context bands | `lib/eaf-context-health.zsh` |
| Trust tiers | `policy/trust-tiers.toml` |
| Full permission policy | `docs/PERMISSION-POLICY.md` |
| Worker conduct rules | `bootstrap/WAVE0-BRIEF.md` |
| Current session state | `HANDOFF.md` |

Regenerate this file with `eaf runbook`. Verify it has not drifted with
`eaf runbook --check`.
