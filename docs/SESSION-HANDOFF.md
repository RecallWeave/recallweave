# Session handoff — task contract capability

Written at session close. Resume from **durable repo state + Beads + this file**.
Do not attempt to recover the previous transcript; everything needed is here or
in `bd`.

## 1. Current objective

Ship a **task contract / scoped work-packet export** for RecallWeave: given a
task spec, emit a minimal, portable context bundle for another AI agent,
exposing only the context that task requires. Canonical output is JSON
(`recallweave.contract.v1`); a Markdown artifact is a **safe human-readable
projection** of it.

This is a durable capability, not a demo.

## 2. Current checkpoint / phase

**Phase: PROMOTED to a milestone PR, awaiting human merge.**

- Integration branch: `foundry/steward` — the durable remote checkpoint, cut
  from `main` after PR #1 merged. `foundry/task-contracts` is HISTORICAL: it is
  the implementation lineage behind that PR and must never be merged into
  current work.
- Suite green with the CommonMark parser and again under
  `-W error::ResourceWarning`; `compileall` clean; runtime dependencies still
  empty (`mistletoe` remains test-only). For the current count and verdict read
  the newest files in `.codex-reviews/` rather than trusting a number written
  here — volatile status goes stale faster than this document is rewritten.
- Adversarial review: **cycle 30 returned a clean PASS** — no findings at any
  severity, and the reviewer states the tree is safe to merge into protected
  `main`. For the run of verdicts read `.codex-reviews/review-*.md` in order;
  restating a count here only creates something else to go stale.
- The review gate is satisfied and Josh approved promotion. The milestone PR
  from `foundry/task-contracts` to `main` was merged, and
  `CHECKPOINT_NOT_APPROVED.md` was deleted in the promotion commit that opened
  it. **The merge itself is still Josh's** — no automation may merge it, mark
  it auto-mergeable, or make it ready on his behalf.

## 3. Architecture decisions already approved — DO NOT REOPEN

These were decided deliberately, several after a failure. Re-litigating them
will repeat work already paid for.

1. **Uniform inert Markdown rendering** (FROZEN INTERFACE v3, in `bd show
   recallweave-9ew`). Every operator-controlled or vault-derived string is
   emitted ONLY inside a fenced code block; only renderer-authored chrome is
   live Markdown. Do not reintroduce escaping.
2. **One fenced block PER FIELD**, each under its own trusted label. Never
   concatenate two document fields into one fence.
3. **mistletoe is a TEST-ONLY dependency.** Runtime stays third-party-free.
   AST assertions are the authoritative inertness gate.
4. **Connection evidence is rendered AND documented.** Do not "fix" the
   disclosure surface by hiding evidence again.
5. **Injectivity is scoped**, not absolute: over the projected field set, over
   well-formed documents, up to line-ending normalization.
6. **The builder must NOT emit every projected key unconditionally.**
   Applicability is evidence-class-dependent, defined by the tables in
   `contract.py`.
7. `spec.notes` is **REJECTED** as an unknown key. `suppressed_total` was
   **DROPPED** and must not be replaced by an overlapping aggregate.
8. **Absence in the Markdown projection is STRUCTURAL**, never in-band. A
   present field always renders a fenced block; an absent field renders its
   trusted label followed by the marker as a bare chrome line with no fence.
   Any in-band sentinel is forgeable — do not go back to one.
9. **Malformed or unauthenticated persisted evidence FAILS THE EXPORT CLOSED**
   (Josh's decision). Not suppressed, not normalized away. Diagnostics name the
   edge by **database id** and never carry vault content — no note path, no
   citation, no passage, no term.
10. **Verification reads the INDEX, never the vault.** `network_calls` and
    `vault_writes` stay 0, so evidence is attributed to the **indexed
    snapshot**, not the vault's current bytes. Do not add export-time file
    reads.
11. **Candidate existence, ranking and `score` are deliberately NOT recomputed**
    (Josh's decision). A candidate is checked to be *shaped and evidenced like*
    one the indexer produces, not to *be* one it produced. The docs say so.
12. **Git cadence** (see CLAUDE.md/AGENTS.md): `main` is protected;
    the active integration branch auto-pushes as a checkpoint whenever the suite is
    green and the tree is clean. A failing review does NOT block the checkpoint
    push; it DOES block promotion, merge, release, deploy and milestone PR.
13. **Codex is the independent adversarial reviewer and must never be used as
    an implementer.**

## 4. Open blockers

<!-- The line below is machine-checked against the committed Beads export by
tests/test_docs_per_field_projection.py. The declared set must EQUAL the set of
open beads labelled `blocker` or `needs-human` -- the same definition the Git
Cadence uses -- so both a stale entry and a MISSING one fail the suite. Write
`none`, or a comma-separated list of bead ids. -->

**Blocking beads:** none

Both P0 beads that blocked promotion at the previous handoff are closed:

- `recallweave-nv0` — an operator-written gloss was labeled `cited_passage`.
  Fixed architecturally: an evidence class now names the ORIGIN of a statement,
  never the presence of a citation. Operator wording stays
  `authored_by_operator` even when cited, with the citation and passage carried
  beside it as support; `cited_passage` may only describe source-derived
  passage text. The Markdown projection shows the supporting passage under its
  own label. No semantic-support inference was added, deliberately — see §3.
- `recallweave-kob` — a heading link's coordinate and level were unbound. Fixed
  by recording every heading in `note_headings` (line, level and the exact
  stripped source line) and comparing stored bytes.

What remains between this branch and `main` is **the merge itself**. Cycle 30
returned a clean PASS and recorded the implementation as complete, with every
Critical and High from cycles 1-29 closed, and Josh approved promotion.

## 5. What was completed (cycles 14-28)

All integrated, green, and each mutation-proven against the pre-fix tree:

- `4a6` — absence made STRUCTURAL, not an in-band marker (41 of 44 projected
  fields could forge absence).
- `4su` — the builder now ENFORCES its own well-formedness predicate; it never
  called it. Validation runs before the budget check.
- `3xl` — projected and omitted field sets made an exhaustive partition; the
  omitted list grew from 10 to 31.
- `w3k` — the malformed-evidence diagnostic stopped disclosing vault note paths
  (a defect introduced by `4su`). Also fixed a latent `ResourceWarning` test
  isolation bug it exposed.
- `e1y` — partition proved across five builder shapes; invariance probes now
  vary cardinality and falsiness (a truthiness read had been undetectable).
- `dm4` — connection-evidence citations resolved against the index and added to
  `provenance.citations`; a passage now requires a citation.
- `e5w` — attribution by CONTENT, not just coordinates: a fabricated passage
  behind a valid citation had been accepted and rendered.
- `zwj` — a present evidence side must carry the complete indexed leaf set;
  omitting `truncated` was a false claim by silence.
- `5vk` — a discovery candidate's own evidence authenticated: shared terms must
  be two or more strings both notes carry, and `method`/`explanation` must be
  the indexer's.
- `o6r` — the persisted edge RECORD authenticated, not just its payload; a
  hand-written row had exported as an authored, verified relationship.
- `ze7` — authored links re-derived through the indexer's own parser and
  resolver, binding line, kind, target and unique resolution.
- `5sy` — whole-section parsing restored the parser's fenced-code state, and a
  false rejection of genuine heading-line links was fixed alongside it.
- `nv0` — evidence classes separated from citation presence (see §4).
- `kob` — heading coordinates and levels bound, via `note_headings`.
- Three follow-on rounds on the same heading route: bodyless headings recorded
  (a heading with no body produces no section but can still carry a link), the
  exact source line stored rather than reconstructed (any canonical rebuild is
  a guess about formatting the source already settled), and the documentation
  brought back in line with the code in four separate places.

## 6. Exact next actions, in order

1. **The milestone PR is open and awaiting Josh's merge.** Do not merge it,
   enable auto-merge, or push to `main`. That is the whole point of the
   cadence's promotion split.
2. **If a milestone PR needs changes**, push them to the active integration
   branch and
   **re-run the gate**: update the block between `<!-- CYCLE-CONTEXT-START -->`
   and `<!-- CYCLE-CONTEXT-END -->` in `docs/CODEX-REVIEW-PROMPT.md`, then
   `./scripts/codex-review.sh`. A PASS ages the moment the tree changes.
3. **Keep the launchd supervisor PAUSED while the PR is open.** It rewrites
   `CHECKPOINT_NOT_APPROVED.md` before every push and refuses to push an
   unmarked branch, so resuming it would recreate the marker the promotion
   commit deleted. Resume it (`rm -f ~/.particle-supervisor/PAUSED`) only after
   the merge, or after deciding the branch is a checkpoint again.
4. **Reproduce every finding before acting on it.** This repeatedly caught
   architect error and several times showed a finding was larger or narrower
   than reported. Never file a bead from an unverified claim.

## 7. Known failure modes and traps

- **The recurring defect class is "the invariant is asserted one level above the
  defect."** Eight consecutive cycles found the next level down. When adding a
  rule, ask where it is ENFORCED, not just where it is stated — and prove it by
  mutation. Cycle 19's complete-shape rule was real in the code and unenforced
  by any test: removing it left all 397 tests green.
- **A fix can introduce the next finding.** `4su` caused `w3k`; `dm4` caused
  `e5w`; `o6r` caused `ze7`; `5sy` caused `kob`. Re-examine what a fix touches.
- **Mutation audits need a clean bytecode cache.** `compileall` writes
  `src/**/__pycache__`, and a `cp`-based restore can leave Python running stale
  bytecode — it produced a deterministic phantom failure this session. Run
  `find src -name __pycache__ -exec rm -rf {} +` between mutation steps.
- **A closed bead is NOT evidence of integrated work.** Confirm the commit is an
  ancestor of the integration branch.
- **Leak assertions need DISTINCTIVE probes.** Asserting a one-character or
  common-word probe is absent from a diagnostic gives false positives — "x" and
  "shared" both occur in ordinary message text.
- **Watch for tests that pass for the wrong reason.** A success fixture with an
  invented passage passes only because coordinates resolve; a partition proved
  over one corpus passes because that corpus has no empty collections.
- **`/tmp` is a trap for swarm workers.** Give worktree-local scratch:
  `mkdir -p ./.gate-tmp/tmp && export TMPDIR=$PWD/.gate-tmp/tmp`.
- **Never use `ntm interrupt`** — it Ctrl+Cs ALL panes. Use `ntm respawn
  <session> --panes=N --force --all`.
- **`bd label add` takes `<issue> <label>`**, in that order.
- **The beads pre-commit hook re-exports `issues.jsonl` on every commit.** The
  clean-tree gate deliberately ignores `.beads/` churn.
- **Codex's sandbox has no writable temp dir and cannot write to a database**,
  so `scripts/codex-review.sh` pre-runs the suite (twice — the second pass with
  `ResourceWarning` promoted to an error) and hands Codex the results. It also
  means the reviewer cannot reproduce database-tampering findings itself:
  reproduce those locally before acting.
- Oversized beads fail silently. Keep them narrow, with an explicit `OWNS:` list.

## 8. Do NOT replan or reconsider

- The thirteen approved decisions in §3.
- The Beads graph and its `OWNS:` boundaries.
- The git cadence and the checkpoint/promotion split.
- Codex's role as independent reviewer.
- The core `schema_version: "2"` and the `recallweave.contract.v1` JSON schema —
  except as `recallweave-kob` may require, which is exactly why it is deferred
  rather than patched.

Anything not listed here is open to the next planner's judgement.
