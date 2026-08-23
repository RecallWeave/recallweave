# Codex adversarial review brief

You are the independent final review gate for this repository. You were invoked
locally and non-interactively from the repository root. Nothing merges or pushes
without passing you.

Act only as a reviewer. **Do not modify the working tree.** You are running under
a read-only sandbox; if you find yourself wanting to change a file, report the
change you would make instead of attempting it.

## What to inspect

- the full current working tree, not only a diff;
- relevant Git history and diffs (`git log`, `git diff <base>..HEAD`), including
  the base commit named in the cycle notes below;
- the test suite, and what it does *not* cover;
- `ARCHITECTURE.md`, `SECURITY.md`, `PRIVACY.md`, `README.md`, `docs/`;
- consistency between what the code does, what the tests assert, and what the
  documentation promises.

Run the suite yourself:

```console
PYTHONPATH=src python3 -m unittest discover -s tests
python -m compileall -q src
```

Reproduce important findings where practical. A reproduction that fails on the
current tree is worth more than an inspection-only claim, and it is the only way
to distinguish a real defect from a plausible-sounding one. Use scratch paths
inside the repository working tree; note that on macOS `/var` and `/tmp` are
symlinks, and this project's hardened destination protocol deliberately refuses a
symlinked parent, so a test that writes there will fail for reasons unrelated to
the code under review.

## What to look for

- **Security**: injection into any generated artifact, sandbox and path handling,
  symlink and junction refusal, destination replacement protocol, disclosure of
  local paths on any route including error routes.
- **Privacy**: leakage of excluded or out-of-scope vault content into any emitted
  field or either output format; claims about what an artifact contains that do
  not match what it actually contains.
- **Correctness**: bounds, budgets, counters, truncation flags, off-by-one, and
  any field whose name promises more than the implementation delivers.
- **Evidence integrity**: this project separates verified, supporting, and
  candidate evidence, and never infers that a passage means something the author
  did not assert. Any code path that blurs those classes is a defect regardless
  of how convenient it is.
- **Backward compatibility**: existing command output, schema versions, exception
  messages, and public behavior must not change unless a change was explicitly
  approved. Compare against the base commit rather than against documentation.
- **Deterministic output**: where reproducibility is part of the contract, verify
  it byte-for-byte rather than trusting a test that normalizes the difference away.
- **Test quality**: tests that assert the presence of documented keys, rather than
  the correctness or stability of behavior, are a known failure mode in this
  repository. Say so when you see one.
- **Remediation-induced regressions**: when a remediation cycle is underway, check
  that fixes did not break something that previously worked, and that each claimed
  fix is actually covered by a test that fails without it.

Treat vault-derived and user-supplied text as untrusted input everywhere it is
rendered or serialized. Upstream sanitization is not a rendering-safety boundary.

## Output format

Begin your response with a single line, exactly:

```text
VERDICT: PASS
```

or `VERDICT: PASS WITH FIXES`, or `VERDICT: FAIL`.

Then:

1. **Findings**, each classified **Critical / High / Medium / Low**, naming exact
   files and lines or code regions, with a concrete failure scenario and, where
   you produced one, the reproduction and its output.
2. **Missing adversarial tests** — specific cases, not general advice.
3. **Positive findings** — properties you verified as actually holding, so the
   next reviewer does not re-derive them.
4. **Ranked recommended fixes.**
5. **Push safety**: state plainly whether the tree is safe to push.

Verdict guidance: `FAIL` for any unresolved Critical or High finding, failing
tests, evidence-integrity violation, privacy leakage, security boundary
violation, unapproved backward-compatibility regression, or reproducibility
failure where reproducibility is part of the contract. `PASS WITH FIXES` when
only Medium and Low findings remain. `PASS` when the tree is clean.

Do not soften a finding because the implementation matches a stated plan or
specification. If the specification is wrong, the specification is the defect.

## Current cycle

<!-- CYCLE-CONTEXT-START -->
Both cycle-26 Mediums are fixed, and all five of your suggested tests are in the
suite. **No open bead blocks promotion.**

- **Medium — a link in a bodyless heading could not be authenticated.**
  Confirmed and reproduced exactly as you derived it from the control flow, so
  your read-only diagnosis was right without a fixture. A note ending in
  `## Related [[Target]]` produced a genuine authored edge that the exporter
  then rejected for want of a coordinate: sections are BODY-DRIVEN and drop a
  heading with nothing beneath it, while links are extracted from every heading
  line. Failing closed, but on real indexer-produced data — the second time this
  route has traded a bypass for a false rejection.

  Heading coordinates now live in their own `note_headings` table, recording
  every heading line's position, `#` level and text independently of whether
  anything follows it. `sections` is restored to its previous shape, so the
  change ADDS a table rather than reinterpreting an existing one, and the
  capability probe looks for that table. Tests cover a terminal bodyless
  heading, one followed only by blank lines, and one between two bodied
  sections — each first asserting the INDEXER really produces the edge.

  Your last suggested test led somewhere useful: distinguishing format
  capability from per-row data pointed at fenced heading-looking lines. Those
  are excluded (`_heading_positions` skips anything inside a fence), and that is
  now pinned by a test forging an edge that cites a `## Fake [[Target]]` line
  inside a fence — the heading-route counterpart of the fenced-body-link case.

- **Medium — the CHANGELOG contradicted itself.** Correct, and you were right
  that it is worse than saying nothing in an evidence-integrity entry. The
  obsolete half ("the coordinate and heading level are not" bound, "which the
  docs disclose") is gone, and a docs test asserts it cannot come back.

Three mutations killed: deriving headings from sections again, recording fenced
headings, and unbinding the bodyless coordinate.

Suite: **423 tests** with the parser, green under `-W error::ResourceWarning`;
`compileall` clean. Runtime dependencies still empty.

1. **Say plainly whether this tree is safe to MERGE into protected `main`.** If
   nothing blocks promotion, say so explicitly and without qualification.
2. Judge `note_headings`: is a second table the right shape, and is it complete?
   It is populated from `_heading_positions`, the same source `_sections` uses,
   so the two cannot disagree about what a heading is — but say if you see a
   note shape where they would.
3. This route has now produced a bypass and two false rejections across three
   cycles. If there is a shape that still authenticates wrongly, or a genuine
   edge that still fails, name it.
4. Check every positive claim in `docs/task-contracts.md`, `ARCHITECTURE.md`,
   `PRIVACY.md` and `CHANGELOG.md` against the code.
5. Reassess every Critical and High from all twenty-six cycles and say which
   remain closed.
<!-- CYCLE-CONTEXT-END -->
