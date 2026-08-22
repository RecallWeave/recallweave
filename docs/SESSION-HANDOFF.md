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

**Phase: adversarial-review remediation, still open.**

- Integration branch: `foundry/task-contracts` — the durable remote checkpoint.
- Suite: **412 tests green** with the CommonMark parser, green again under
  `-W error::ResourceWarning`, `compileall` clean, runtime dependencies still
  empty (`mistletoe` remains test-only).
- Adversarial review: **25 cycles run.** Cycles 16 and 19 returned PASS WITH
  FIXES; every other cycle returned FAIL. Cycle 25 is the latest: FAIL.
- **Two P0 beads block promotion** (see §4). Everything else found in cycles
  14-25 is fixed and integrated.

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
    `foundry/task-contracts` auto-pushes as a checkpoint whenever the suite is
    green and the tree is clean. A failing review does NOT block the checkpoint
    push; it DOES block promotion, merge, release, deploy and milestone PR.
13. **Codex is the independent adversarial reviewer and must never be used as
    an implementer.**

## 4. Open blockers — BOTH BLOCK PROMOTION

Two P0 beads. Both reproduced, both filed with a full diagnosis and options.

- **`recallweave-nv0` — an operator-written gloss is labeled `cited_passage`.**
  The most serious open item: it needs **no tampering**, only the documented
  spec format. A note-backed constraint or prior decision may carry an operator
  `statement`; the builder copies that gloss into `statement` and labels the
  whole item `cited_passage`. Nothing checks the passage says anything like it.
  Reproduced: vault says "We evaluated three vendors and picked none of them
  yet", operator gloss says "This architecture decision was approved", and the
  artifact emits the gloss under `cited_passage` with a real citation. The
  Markdown does not project `passage` for these items, so the reader never sees
  the contradiction. **Needs a decision on the evidence model** — three options
  are written up in the bead's design field; option A (classify by who wrote
  the statement, carry the citation as separate supporting evidence) is
  recommended. Note the projected/omitted partition moves with it.
- **`recallweave-kob` — heading-link re-derivation cannot bind the coordinate or
  heading level.** Deferred by Josh, deliberately. `sections` records a body's
  `line_start`/`line_end` but never a heading's own physical line or `#` count,
  so a tampered index can pair an authentic indexed heading with a false `line`
  or a changed level. The real fix is recording heading position and level in
  the index — a core schema change, out of scope for the contract work. The gap
  is **disclosed in `docs/task-contracts.md`** and pinned by a docs test.

Nothing has been promoted; no PR exists. Final merge needs a Codex **PASS** plus
explicit human approval.

## 5. What was completed this session (cycles 14-25)

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

## 6. Exact next actions, in order

1. **Decide `recallweave-nv0`** (the evidence model for an operator gloss). It
   is the only open item reachable without a tampered index, and it touches the
   project's central premise. `bd show recallweave-nv0` has the reproduction and
   three written-up options.
2. **Land `recallweave-nv0`**, then re-run the review.
3. **Decide `recallweave-kob`**: whether the index schema change is worth doing
   now or the disclosure stands for this release.
4. **Run the next adversarial cycle**: update the block between
   `<!-- CYCLE-CONTEXT-START -->` and `<!-- CYCLE-CONTEXT-END -->` in
   `docs/CODEX-REVIEW-PROMPT.md`, then `./scripts/codex-review.sh`. The verdict
   is the first line of the newest `.codex-reviews/review-*.md`.
5. **Reproduce every finding before acting on it.** This has repeatedly caught
   architect error, and twice this session it caught a defect the reviewer had
   under-described. Never file a bead from an unverified claim.
6. **On PASS:** stop and ask Josh. Opening the milestone PR and merging to
   `main` are his decisions.

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
