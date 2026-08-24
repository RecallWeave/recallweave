# Session handoff — RecallWeave task contract capability

Written at planner rotation. Resume from **durable repo state + Beads + this
file + `SWARM-RUNBOOK.md`**. Do not reconstruct the previous transcript;
everything needed is here or in `bd`. Treat every state fact in §11 as a
hypothesis until you re-probe it — it was true when written, not necessarily now.

## 1. Current objective

Ship a **task contract / scoped work-packet export** for RecallWeave: given a
task spec, emit a minimal, portable context bundle for another AI agent,
exposing only the context that task requires. Canonical output is JSON
(`recallweave.contract.v1`); the Markdown artifact is a **safe human-readable
projection** of it.

The capability is **built, reviewed, promoted and merged**. The objective is now
*stewardship*: keep it correct, keep the checkpoint durable, and do not reopen
settled design.

## 2. Current phase / checkpoint

**Phase: STEWARDSHIP — post-PR #4; fresh `foundry/steward` cut from `main`.**

- Integration branch: **`foundry/steward`** cut from `main` @ `374be23` (PR #4).
- `foundry/task-contracts` remains **HISTORICAL** (PR #1 lineage). **Never merge
  it into current work.**
- Latest adversarial verdict on the promoted tree: cycle **24i** `PASS WITH FIXES`
  (Medium test-scanner gaps only; no runtime Critical/High).
- `CHECKPOINT_NOT_APPROVED.md` is **restored** on the fresh steward.
- Supervisor may be **unpaused** after this cut.
- `SWARM-RUNBOOK.md` was adopted 2026-08-23 as operating doctrine. **Read its
  §11 first**: the generated body was rendered for `elevare-agent-factory` and
  its project facts do not describe this repo.

## 3. Architecture decisions already approved — DO NOT REOPEN

Decided deliberately, several after a failure. Re-litigating them repeats work
already paid for.

1. **Uniform inert Markdown rendering** (FROZEN INTERFACE v3, in `bd show
   recallweave-9ew`). Every operator-controlled or vault-derived string is
   emitted ONLY inside a fenced code block; only renderer-authored chrome is
   live Markdown. Do not reintroduce escaping.
2. **One fenced block PER FIELD**, each under its own trusted label. Never
   concatenate two document fields into one fence.
3. **mistletoe is a TEST-ONLY dependency.** Runtime stays third-party-free. AST
   assertions are the authoritative inertness gate.
4. **Connection evidence is rendered AND documented.** Do not "fix" the
   disclosure surface by hiding evidence again.
5. **Injectivity is scoped**, not absolute: over the projected field set, over
   well-formed documents, up to line-ending normalization.
6. **The builder must NOT emit every projected key unconditionally.**
   Applicability is evidence-class-dependent, defined by the tables in
   `contract.py`.
7. `spec.notes` is **REJECTED** as an unknown key. `suppressed_total` was
   **DROPPED** and must not be replaced by an overlapping aggregate.
8. **Absence in the Markdown projection is STRUCTURAL**, never in-band. A present
   field always renders a fenced block; an absent field renders its trusted
   label followed by the marker as a bare chrome line with no fence. Any in-band
   sentinel is forgeable.
9. **Malformed or unauthenticated persisted evidence FAILS THE EXPORT CLOSED**
   (Josh's decision). Diagnostics name the edge by **database id** and never
   carry vault content — no note path, no citation, no passage, no term.
10. **Verification reads the INDEX, never the vault.** `network_calls` and
    `vault_writes` stay 0. Evidence is attributed to the **indexed snapshot**,
    not the vault's current bytes. Do not add export-time file reads.
11. **Candidate existence, ranking and `score` are deliberately NOT recomputed**
    (Josh's decision). A candidate is checked to be *shaped and evidenced like*
    one the indexer produces, not to *be* one it produced.
12. **Evidence class names ORIGIN, not citation presence.** Operator wording
    stays `authored_by_operator` even when cited; `cited_passage` may only
    describe source-derived passage text. The supporting passage is projected
    under its own label. **No semantic-support inference in the evidence layer.**
13. **Git cadence** (CLAUDE.md / AGENTS.md): `main` protected; the integration
    branch auto-pushes as a checkpoint when the suite is green and the tree is
    clean. A failing review does NOT block the checkpoint push; it DOES block
    promotion, merge, release, deploy and milestone PR.
14. **Codex is the independent adversarial reviewer and must never be an
    implementer.**
15. **Squash is the promotion mechanism.** `main` enforces linear history and
    feature branches carry merge commits, so merge-commit and rebase are both
    refused by GitHub. Lineage lives on the retired branch and in the PR.

## 4. Open blockers

<!-- The line below is machine-checked against the committed Beads export by
tests/test_docs_per_field_projection.py. The declared set must EQUAL the set of
open beads labelled `blocker` or `needs-human` -- the same definition the Git
Cadence uses -- so both a stale entry and a MISSING one fail the suite. Write
`none`, or a comma-separated list of bead ids. -->

**Blocking beads:** none

No blocker- or needs-human-labelled beads are open. Nothing is stalled, and no
worker holds unintegrated or uncommitted work.

## 5. Unresolved human decisions

All four items from the 2026-08-23 rotation queue were resolved by Josh on
2026-08-23:

1. **Stale P0 epics closed.** `recallweave-9ew` and `recallweave-vzb` — all
   children shipped in PR #1.
2. **`recallweave-z1a` closed and integrated** (`2696bb9`, 2026-08-23).
3. **Context-health gap closed.** `rw_supervisor.py` now probes
   `ntm status --json` for `context_tokens` and enforces the 100K/130K/140K
   bands (rotate refuses dirty worktrees; dispatch withheld from checkpoint+).
4. **`eaf runbook` project-awareness** recorded in `bd remember` as a Factory
   fix (`recallweave-eaf-runbook-gap`), not actionable in this repo.

## 6. Active beads and worker assignments

- Epic **`recallweave-7o5`** — stewardship phase queue after PR #3:
  1. **`recallweave-rm3` closed**
  2. **`recallweave-41d` closed** — cycle-24d PASS (`review-20260824T064133Z.md`)
  3. **`recallweave-fh2` in progress** — promote to main (Codex ≤3; Josh
     pre-authorized merge)
- Also closed: `cxn`, `dle`. Supervisor **paused** for promotion.

## 7. Known failure modes and traps

- **The recurring defect class is "the invariant is asserted one level above the
  defect"** — equivalently, a rule applied to the values a function happens to
  touch rather than quantified over the class it governs. Eight consecutive
  cycles found the next level down. When adding a rule, ask where it is
  ENFORCED, not just where it is stated, and prove it by mutation. Cycle 19's
  complete-shape rule was real in the code and enforced by no test: removing it
  left all 397 tests green.
- **A fix can introduce the next finding.** `4su`→`w3k`; `dm4`→`e5w`;
  `o6r`→`ze7`; `5sy`→`kob`. Re-examine what a fix touches.
- **A green local suite is not evidence about platforms it does not run.** Three
  tests errored on Windows for ~17 review cycles while passing locally. The
  merge gate is the branch-protection required-check set, not the local suite.
- **Two independent reviewers, not one.** The Codex CLI gate reviews the working
  tree; the GitHub PR reviewer reviews the diff. They find different classes —
  the CLI gate passed a tree whose exclusion breach the PR reviewer caught.
  Neither alone is sufficient for promotion.
- **Reproduce every finding before acting on it.** This caught architect error
  and repeatedly showed a finding was larger or narrower than reported. Never
  file a bead from an unverified claim.
- **Mutation audits need a clean bytecode cache.** `compileall` writes
  `src/**/__pycache__`, and a `cp`-based restore can leave Python running stale
  bytecode — it produced a deterministic phantom failure. Run
  `find src -name __pycache__ -exec rm -rf {} +` between mutation steps.
- **A closed bead is NOT evidence of integrated work.** Confirm the commit is an
  ancestor of the integration branch.
- **`needs-human` blocks the push gate as well as dispatch.** Using it to park
  work freezes checkpoints. Use `bd defer` for undispatchable-but-durable work.
- **Promotion invalidates ancestry for every derived branch.** After a squash
  promotion, verify each lane holds zero commits absent from the retired branch
  (`git rev-list --count <retired>..<lane>` = 0), then reset it. Skipping this
  makes lanes read 50–107 commits "ahead" of a branch cut from the squash.
- **Leak assertions need DISTINCTIVE probes.** A one-character or common-word
  probe gives false positives — "x" and "shared" occur in ordinary message text.
- **Watch for tests that pass for the wrong reason.** A success fixture with an
  invented passage passes only because coordinates resolve.
- **`/tmp` is a trap for swarm workers.** Give worktree-local scratch:
  `mkdir -p ./.gate-tmp/tmp && export TMPDIR=$PWD/.gate-tmp/tmp`.
- **Never use `ntm interrupt`** — it Ctrl+Cs ALL panes. Use
  `ntm respawn <session> --panes=N --force --all`.
- **`bd label add` takes `<issue> <label>`**, in that order.
- **The beads pre-commit hook re-exports `issues.jsonl` on every commit.** The
  clean-tree gate deliberately ignores `.beads/` churn.
- **Codex's sandbox has no writable temp dir and cannot write to a database**, so
  `scripts/codex-review.sh` pre-runs the suite (twice — the second pass with
  `ResourceWarning` promoted to an error) and hands Codex the results. The
  reviewer cannot reproduce database-tampering findings itself.
- **Run the suite with the project venv, not system python.** Canonical:
  `PYTHONPATH=src .codex-reviews/.venv/bin/python -m unittest discover -s tests`.
  System python lacks `mistletoe`, so `discover` collects 56 tests with 21 import
  errors instead of 461 — a wrong invocation that looks like a broken tree.
- Oversized beads fail silently. Keep them narrow, with an explicit `OWNS:` list.

## 8. Supervisor / coordinator status

- **Running.** Supervisor was unpaused after PR #4 merge and fresh steward cut.
- **The supervisor owns the routine loop** — integrate, gate, rotate, nudge,
  push, dispatch. Do not hand-drive it in parallel; two dispatchers produce
  duplicate work and lost commits.

## 9. Review state

- Adversarial gate: cycle **24i PASS WITH FIXES** on promoted tree (PR #4);
  cycle **25** pending on path-binding scanner fix (`recallweave-07h`).

## 10. Do NOT replan or reconsider

- The fifteen approved decisions in §3.
- The Beads graph and its `OWNS:` boundaries.
- The git cadence and the checkpoint/promotion split.
- Codex's role as independent reviewer.
- `schema_version: "2"` and the `recallweave.contract.v1` JSON schema.
- The branch topology: `main` protected, `foundry/steward` active,
  `foundry/task-contracts` historical and never merged forward.
- The `SWARM-RUNBOOK.md` §11 overrides.

Anything not listed here is open to the next planner's judgement.

## 11. Verified state at handoff (2026-08-24 post-merge)

Re-probe before acting. Any un-rechecked read of live state is a hypothesis.

| Check | Value |
|---|---|
| Repo path | `/Users/josh/particle-workers/recallweave` |
| Git branch | `foundry/steward` (fresh cut from `main` @ `374be23`) |
| Marker | **present** |
| Supervisor | **running** (no `~/.particle-supervisor/PAUSED` marker) |
| Latest verdict | cycle 25 in progress on path-binding scanner fix |
| Beads authoritative | **yes** — `86e` cycle-25 gate open |
| Resumable without transcript | **yes** |

**Any approved planner (Claude, Cursor, Codex) can resume from this repo alone.**
Durable state lives in: this file, `SWARM-RUNBOOK.md`, `bd`, the git history,
`.codex-reviews/`, and `CLAUDE.md` / `AGENTS.md`. No decision needed to continue
exists only in a transcript.
