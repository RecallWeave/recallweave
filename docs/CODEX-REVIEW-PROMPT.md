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
Your cycle-24 finding is confirmed and reproduced exactly as you described:
claimed line 1, claimed line 999999, and `######` in place of `##` all
authenticate against an authentic indexed heading, while a claimed real body
line is correctly rejected. Good catch on the route this project had just added.

**The Medium is fixed; the High is deliberately deferred, and this cycle should
judge that decision.**

- **Medium — the documentation over-claimed.** Fixed. `docs/task-contracts.md`
  now separates the two cases plainly: a link in a section BODY is fully bound
  (exact physical line, whole-section parse, link at that line, resolver with
  uniqueness); a link on a HEADING line is **only partly bound**, and the
  paragraph says exactly what is not bound — the coordinate and the heading
  level — and why: `sections` records a body's `line_start` and `line_end` and
  never the heading's own physical line or its `#` count. It also says what IS
  still bound (heading text, link kind, link target, unique resolution), so a
  heading link cannot be invented, only mis-coordinated. The CHANGELOG's blanket
  "exact physical line" claim is corrected the same way, and a docs test pins
  the disclosure so it cannot quietly disappear before the property is real.

- **High — deferred as tracked follow-up (`recallweave-kob`, P0).** The
  exporter cannot close this from the index as it stands. Three options were
  weighed: bind only the derivable window (narrows, does not close); reject
  heading-line authored edges outright (fully honest, but refuses the whole
  export for a legitimate vault whose link sits on a heading — the false
  rejection cycle 23 exposed, reinstated deliberately); or record the heading's
  physical line and level in the index, which closes it properly but is a core
  index schema change and a re-index for existing users. Josh chose to disclose
  now and bind later, with the schema change tracked as its own P0. **Promotion
  to `main` stays blocked until it lands** — that is not being waved through.

Suite: 412 tests with the parser, green under `-W error::ResourceWarning`;
`compileall` clean. Runtime dependencies are still empty.

This is the last cycle of this session, so please make it a summary judgement
rather than only a defect hunt:

1. **Is deferring the heading-coordinate binding defensible**, given that it is
   disclosed in the docs, tracked as a P0, and blocks promotion? If you think
   one of the other two options should have been taken instead, say which and
   why — that is directly actionable for the next session.
2. **Is the documentation now TRUE?** That matters more than usual here, because
   the disclosure paragraph is the thing standing in for the missing property.
   Check `docs/task-contracts.md`, `ARCHITECTURE.md`, `PRIVACY.md` and
   `CHANGELOG.md` against the code and name anything still over-claimed.
3. **Is there a ninth level anywhere OTHER than the deferred one?** The last
   several findings have all been in the connections path; retrieved context and
   the operator-supplied constraint and decision citations have had far less
   scrutiny this run.
4. Reassess every Critical and High from all twenty-four cycles and say which
   remain closed.
5. State the push/promotion position plainly for the handoff: what this tree is
   safe for, and what it is not.
<!-- CYCLE-CONTEXT-END -->
