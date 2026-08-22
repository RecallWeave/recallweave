# Session handoff — task contract capability

Written at planner/session rotation. Resume from **durable repo state + Beads + this
file**. Do not attempt to recover the previous transcript; everything needed is here
or in `bd`.

## 1. Current objective

Ship a **task contract / scoped work-packet export** for RecallWeave: given a task
spec, emit a minimal, portable context bundle for another AI agent, exposing only the
context that task requires. Canonical output is JSON (`recallweave.contract.v1`); a
Markdown artifact is a **safe human-readable projection** of it.

This is a durable capability, not a demo.

## 2. Current checkpoint / phase

**Phase: adversarial-review remediation.** Implementation is complete and integrated;
the work is hardening the artifact against an independent reviewer.

- Integration branch: `foundry/task-contracts` — the durable remote checkpoint.
- Suite: **369 tests green** with the CommonMark parser, `compileall` clean.
- Adversarial review: **13 cycles run, all 13 returned FAIL.** Every cycle found at
  least one genuine defect; every finding was reproduced before acting on it.
- Cycle-13 findings are all fixed and integrated (`6j3`, `0kl`).
- **Cycle 14 has NOT been run yet.** It is gated on `recallweave-4a6` landing.

## 3. Architecture decisions already approved — DO NOT REOPEN

These were decided deliberately, several after a failure. Re-litigating them will
repeat work already paid for.

1. **Uniform inert Markdown rendering** (FROZEN INTERFACE v3, in `bd show
   recallweave-9ew`). Every operator-controlled or vault-derived string is emitted
   ONLY inside a fenced code block; only renderer-authored chrome is live Markdown.
   This replaced context-specific escaping after **six consecutive** cycles each found
   another escaping defect. Do not reintroduce escaping.
2. **One fenced block PER FIELD**, each under its own trusted label. Never concatenate
   two document fields into one fence — that destroys the evidence boundary.
3. **mistletoe is a TEST-ONLY dependency.** Runtime stays third-party-dependency-free;
   `pip install -e .` must pull nothing. AST assertions are the authoritative
   inertness gate, not string heuristics.
4. **Connection evidence is rendered AND documented** (Josh's decision on `ah5`). Do
   not "fix" the disclosure surface by hiding evidence again: `budget.characters_used`
   already charges for evidence passages and headings.
5. **Injectivity is scoped**, not absolute: over the projected field set, over
   well-formed documents, up to line-ending normalization. CRLF/CR→LF normalization is
   REQUIRED for fence safety and must not be removed.
6. **The builder must NOT emit every projected key unconditionally.** A verified
   (authored-wikilink) connection has no TF-IDF `shared_terms`; fabricating
   `shared_terms: null` would blur the verified/supporting/candidate boundary that is
   this project's core premise. Applicability is evidence-class-dependent, defined by
   the table in `contract.py`.
7. `spec.notes` is **REJECTED** as an unknown key (not ignored). `suppressed_total` was
   **DROPPED** and must not be replaced by an overlapping aggregate.
8. **Git cadence** (see `## Git Cadence` in CLAUDE.md/AGENTS.md): `main` is protected
   and never pushed to or merged into locally; `foundry/task-contracts` auto-pushes as
   a checkpoint whenever the suite is green and the tree is clean. PRs are
   milestone-based and merging is Josh's call.
9. **A failing review does NOT block the checkpoint push** (durability only). It DOES
   block all promotion, merge, release, deploy and milestone-PR actions. Checkpoint
   branches carry `CHECKPOINT_NOT_APPROVED.md` and must never be auto-mergeable.
10. **Codex is the independent adversarial reviewer and must never be used as an
    implementer.** Preserving its independence is the point.

## 4. Open blockers and unresolved human decisions

- **No active blockers.** GitHub push access was granted mid-session; the checkpoint
  pushes cleanly.
- **Unresolved, needs Josh eventually:** final merge of `foundry/task-contracts` into
  `main` via milestone PR. Requires a Codex **PASS** plus explicit human approval.
  Nothing has been promoted; no PR exists.
- **Watch item (not yet a decision):** `recallweave-6j3` resolved the cycle-13 High by
  REJECTING a partial evidence side as malformed rather than by projecting side-level
  `truncated`. Both options were legitimate; the reject path was taken. Cycle 14 should
  be allowed to judge whether that is honest or merely moves the hole. Do not
  pre-emptively rewrite it.

## 5. Active beads and worker assignments

- `recallweave-4a6` — **P0, OPEN, not yet dispatched.** The only outstanding work.
- Two stale parent epics remain open by design and are NOT dispatchable (`bd` filters
  epics): `recallweave-9ew`, `recallweave-vzb`. `9ew`'s design field is FROZEN
  INTERFACE v3 — keep it; it is the architecture record.
- Swarm: 8 DSV4 workers, tmux session `recallweave`, panes 0-7 → `oc_1`..`oc_8`,
  worktrees under `.ntm/worktrees/recallweave/`. All idle and in sync at handoff.
- A launchd supervisor (`/Users/josh/particle-workers/supervisor/rw_supervisor.py`,
  5-minute interval) integrates, gates, pushes and dispatches autonomously. It was
  **left PAUSED** at handoff — see §7.

## 6. What was completed most recently

In order, all integrated and green:

- `ah5` — corrected the projection boundary; the docs no longer falsely claim
  connection evidence is omitted. Replaced a self-referential doc/test agreement check
  with one anchored to actual renderer output.
- `3jt` — missing-key vs explicit-null made consistent; injectivity restated over
  well-formed documents.
- `awa` — evidence-class applicability made explicit and well-formedness made
  *rejectable* (it previously derived applicability from presence and could not reject
  anything).
- `0v1` — repaired disclosure sentinels that could never match, and restored the
  docs↔`PROJECTED_FIELDS` drift check that had regressed.
- `6j3` — well-formedness now reaches inside evidence sides (cycle-13 High).
- `0kl` — disclosure test now proves omission by value-invariance rather than by
  matching today's formatting (cycle-13 Medium).
- `hl7` — projection order fidelity pinned for every projected collection.

## 7. Exact next actions, in order

1. **Resume the supervisor** if autonomous progression is wanted:
   `rm -f ~/.particle-supervisor/PAUSED`
   It will then dispatch `4a6`, integrate, gate and push on its own. Leave the PAUSED
   file in place to keep the swarm idle.
2. **Land `recallweave-4a6`** (`bd show recallweave-4a6` has the full diagnosis and a
   specified fix). It is the last known defect.
3. **Run adversarial review cycle 14**, after `4a6` integrates and the suite is green:
   - update the block between `<!-- CYCLE-CONTEXT-START -->` and
     `<!-- CYCLE-CONTEXT-END -->` in `docs/CODEX-REVIEW-PROMPT.md` to state what
     cycle-13 fixed and what to attack next;
   - `./scripts/codex-review.sh`
   - the verdict is the first line of the newest `.codex-reviews/review-*.md`.
4. **Reproduce every finding before acting on it.** This has caught reviewer error and
   has repeatedly caught architect error. Never file a bead from an unverified claim.
5. **On PASS:** stop and ask Josh. Opening the milestone PR and merging to `main` are
   his decisions, not the session's.

## 8. Known failure modes and traps

- **A closed bead is NOT evidence of integrated work.** `3jt` was closed while its
  commit sat unintegrated for hours. Always confirm the commit is an ancestor of the
  integration branch.
- **Workers must merge the integration branch FIRST.** `3jt` and a duplicate `9ew.17`
  were both built on stale bases and conflicted. Stale base is the single most common
  cause of failed integration here.
- **Self-fulfilling tests are the recurring defect class.** Three consecutive cycles
  found an invariant asserted at one level while the defect lived one level below it.
  When adding a test, ask: *if the implementation violated this, would this test
  actually fail?* Prove it by mutation rather than assuming.
- **A mutation audit is cheap and worth repeating.** One run found two working
  disclosure leaks, an unenforced ordering claim, and a P0 injectivity hole that
  thirteen review cycles had missed.
- **`/tmp` is a trap for workers.** Any path outside the worktree raises a blocking
  OpenCode permission modal. Give workers worktree-local scratch:
  `mkdir -p ./.gate-tmp/tmp && export TMPDIR=$PWD/.gate-tmp/tmp`. Reject such prompts;
  never grant "always".
- **Never use `ntm interrupt`** — it sends Ctrl+C to ALL panes and kills idle agents to
  a bare shell. Use `ntm respawn <session> --panes=N --force --all`, which reports
  "failed to restart cleanly" on a 15s timeout but usually succeeded. Respawn does NOT
  clear a worker's context.
- **`ntm assign` is unusable** (needs a `br` CLI that is not installed). Use
  `ntm send --panes=0.N --file <prompt>`.
- **`bd label add` takes `<issue> <label>`**, in that order. Reversed, it silently
  creates a label named after the issue id.
- **The beads pre-commit hook re-exports `issues.jsonl` on every commit**, and
  `bd export` leaves `.~*` temp files. The clean-tree gate therefore deliberately
  ignores `.beads/` churn and judges source cleanliness only. Do not "fix" this by
  making the gate stricter; it will block every push.
- **Codex's sandbox has no writable temp dir**, so `scripts/codex-review.sh` pre-runs
  the suite in a venv and hands Codex the results. Do not remove that step.
- Oversized beads fail silently: `9ew.2` and `9ew.10` each burned two workers and
  produced zero edits. Keep beads narrow, with an explicit `OWNS:` file list.

## 9. Do NOT replan or reconsider

- The ten approved decisions in §3, especially uniform inert rendering, the test-only
  parser, rendering connection evidence, and the evidence-class-conditional builder.
- The Beads graph and its `OWNS:` boundaries. Beads are sized and sequenced to keep
  workers off each other's files; re-splitting them will manufacture conflicts.
- The git cadence and the checkpoint/promotion split.
- Codex's role as independent reviewer.
- The core `schema_version: "2"` and the `recallweave.contract.v1` JSON schema — the
  JSON is canonical and does not change to accommodate rendering.

Anything not listed here is open to the next planner's judgement.
