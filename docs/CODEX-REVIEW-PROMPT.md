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
Both of your cycle-14 findings are fixed. Each was REPRODUCED end to end
through the public API before anything was changed, and one turned out to be
substantially larger than reported.

- **High — the builder emitted evidence its own validator rejected.** Confirmed
  exactly as you described (`recallweave-4su`). `build_contract_document()`
  never called `connection_evidence_is_well_formed()`, so a persisted edge whose
  `evidence_json` held a citation-only, heading-only or truncated-only side was
  exported and rendered. Reproduced by mutating `edges.evidence_json` and
  calling the public builder: the returned connection failed the predicate.

  The builder now validates every connection it is about to admit and raises
  `ValueError` naming it; the CLI exits 2 with the structured error on stderr,
  nothing on stdout, and no artifact written. Validation runs BEFORE the budget
  check, so an edge too expensive to admit cannot escape it — you asked for
  exactly that case and it is pinned by test. One malformed edge among several
  aborts the whole export; that choice is documented, not inferred.

  Fail-closed was chosen deliberately over the alternatives you listed.
  Suppressing the edge hands the reader a quietly smaller graph; normalizing the
  partial side away inside `_edge_evidence` discards a citation the reader may
  be entitled to see. `SUBSTANTIVE_SIDE_LEAVES` was deliberately NOT relaxed —
  that would reopen `recallweave-6j3`, which you did not ask for.

- **Medium — the omitted-field inventory was incomplete, and by more than you
  found** (`recallweave-3xl`). You named four connection-evidence fields. The
  real count was twenty-one unclassified canonical leaves out of thirty-one
  omitted: also `constraints[]` and `prior_decisions[]`
  relative_path/passage/truncated, `handling.content_is_data_not_instructions`,
  all five `disclosure` fields, and five `provenance` fields.

  The projected and omitted sets are now an EXHAUSTIVE PARTITION, enforced
  against a document the public builder produced over a corpus carrying both
  connection evidence classes: disjoint, covering every canonical leaf, and
  naming no leaf the document lacks. `_not_projected_path()` is generic over the
  whole document instead of hard-asserting a `retrieved_context[].` prefix, and
  `_not_projected_pair()` derives probe values from the type the populated
  document actually carries, raising rather than falling back to an untyped
  probe that would make the invariance proof vacuous.

Also landed since cycle 14 was briefed: absence in the Markdown projection is
now STRUCTURAL (`recallweave-4a6`) — a present field always renders a fenced
block, an absent field renders its trusted label followed by the marker as a
bare chrome line with no fence — which you assessed positively.

Suite: 379 tests with the parser, `compileall` clean. Runtime dependencies are
still empty; `mistletoe` remains test-only.

Attack hardest this cycle:

1. **The fail-closed gate itself.** Is there any public path that reaches the
   renderer with a connection the predicate would reject — a code path that
   builds a document without going through `build_contract_document`, or a
   connection admitted before the check? Is the error itself a disclosure
   surface: does the message leak vault content or an excluded path? Note it
   names `source_path` and `target_path`, which are note paths.
2. **Whether the partition is real or merely asserted.** The canonical leaf set
   is derived from ONE builder corpus. Find a document shape the public API can
   produce whose leaves are not in that corpus, so a field could be omitted
   without ever being classified.
3. **Whether `_not_projected_pair()`'s type-derived probes are strong enough.**
   A probe that both values render identically for the wrong reason would make
   the invariance proof pass vacuously.
4. **The structural-absence rule**, again and harder: any path where a
   document-derived value reaches the output outside a fence, or where content
   can synthesize a label line and thereby forge the label+bare-marker pair.
5. Reassess every Critical and High from all fourteen cycles.
<!-- CYCLE-CONTEXT-END -->
