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
A sixth cycle just completed. Your cycle-5 findings:

- **High — bare CR escaped the blockquote.** Fixed: CR and CRLF are normalized at
  the renderer's single line-splitting choke point, and the test oracle now
  normalizes line endings before counting structure (it previously split on LF
  only, which is why it missed this). Verified: `handling.statement` of
  `safe# forged` produces no active heading after normalization.
- **High — unapproved compatibility regression on falsy citations.** Fixed: the
  prior truthiness condition is restored. Verified byte-identical to base
  `bf1a5e7` for citation values `""`, `0`, `False`, and `None`, in both
  single-line and multiline cited items, pinned by a golden test.
- **Low — `_Escaped` accepted raw text.** Addressed by narrowing it.

Suite: 301 tests passing.

This is the sixth review of this module. Your five previous cycles each found a
further escaping edge case: block structure, inline constructs, `handling`
routing, the multiline citation branch, and bare CR. The escaping approach is
hand-rolled and stdlib-only by project policy.

Two things worth your explicit judgment this round, stated plainly rather than
buried:

1. Whether the remaining risk in hand-rolled Markdown escaping is now acceptable,
   or whether the surface keeps producing edge cases and the approach itself
   should change — for example rendering every untrusted value inside a code span
   or fence uniformly, so inline escaping correctness stops mattering. If you
   believe the approach must change, say so directly; that is a design decision
   the maintainer will weigh, and repeated narrow findings are evidence for it.
2. Whether verification without a CommonMark parser is now sufficient. You
   previously judged that no single finding justified a first test dependency.
   With five escaping findings behind us, say whether that judgment still holds.
<!-- CYCLE-CONTEXT-END -->
