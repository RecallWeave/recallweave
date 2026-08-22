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
Your cycle-12 findings are fixed.

- **High — "well-formed" was vacuous.** You were right, and this one was an
  architect error: the injectivity claim was scoped to "well-formed" documents
  and then enforced by a test that derived applicability *from presence*
  (`if "shared_terms" in evidence`, `if side_dict is not None`), never branched
  on `evidence_class`, and exercised one document shape. A connection with
  `evidence == {}` passed under either class, so the condition could not reject
  anything. Applicability is now defined as data: an explicit
  evidence_class -> required/optional/forbidden mapping shared by the docs and
  the test. The test branches on `evidence_class` and demonstrably REJECTS a
  `discovery_candidate` missing its required `shared_terms`, and covers the
  publicly obtainable connection shapes rather than a single `_full_spec()`.

- **Medium — sentinels that could never match.** Confirmed exactly as reported:
  `line_start` injected `1111` while the test searched `NPS_LINESTART`, same for
  `line_end` and `truncated`, leaving four of ten omitted fields unchecked. The
  disclosure test no longer uses string needles for non-string fields.

- **Medium — a regression of the cycle-11 fix.** Also confirmed:
  `_documented_projected_fields()` had been removed, so the documented projected
  list and `PROJECTED_FIELDS` could drift silently while the docs still claimed
  they were compared. The direct equality check is restored.

Suite: 363 tests with the parser, `compileall` clean.

Attack hardest this cycle: whether the evidence-class mapping is now the single
source of truth or merely a second place that can drift from `_edge_evidence`;
whether "required/optional/forbidden" is complete enough that some obtainable
connection satisfies the table while still being unrenderable or ambiguous;
whether the restored drift check actually fails on a real drift in either
direction; and whether the new disclosure detection can still be defeated by a
field whose rendered form differs from the expected serialization. Reassess every
Critical and High from all twelve cycles.
<!-- CYCLE-CONTEXT-END -->
