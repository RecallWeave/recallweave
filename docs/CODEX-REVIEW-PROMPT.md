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
Both cycle-22 findings are fixed, and all eight of your suggested tests are in
the suite. You were right and the framing was the useful part: I had built a
proxy and called it a re-derivation. Checking the parts is not re-derivation.

- **High — the authored-link check verified the parts, not the binding**
  (`recallweave-ze7`). Your reproduction stands as reported: a line reading
  "This line contains no link at all." authenticated a verified relationship
  between unlinked notes.

  The check now uses the INDEXER'S OWN code rather than a parallel matcher that
  can drift from it. The exact physical line is read back out of the indexed
  section covering the claimed coordinate and the quoted source text must BE
  that line, not any nearby text; that one line is parsed with
  `parser._links`, and an extracted link must match the edge's kind AND target;
  and that link is resolved with `index._resolve_link`, requiring exactly one
  candidate and no ambiguity reason — so ambiguous names are rejected as the
  indexer rejects them, and path resolution uses the indexer's exact keys
  instead of the previous suffix match.

  One implementation note worth your scrutiny: the `note_names` lookup is
  de-duplicated per normalized name, because one note contributes several rows
  (title and stem) and without that a single unambiguous note looks like two
  candidates and EVERY genuine link is rejected. Distinct ids under one name is
  what ambiguity means. The ambiguity test tries BOTH ambiguous endpoints, so a
  resolver that returned several candidates and took the first cannot pass by
  luck.

  Everything still reads the index — `_links` runs over one line already stored
  in `sections.text` — so the exporter performs no file reads.

- **Medium — the score wording overstated the implementation.** Corrected. A
  candidate's `score` is described as **persisted and range-checked**, not as a
  recomputed cosine, and the disclaimer now appears where the `connections`
  contract is first described rather than only in a later section. The
  CHANGELOG's false claim is fixed. Two docs tests pin the wording.

Five mutations killed: dropping the link/kind/target binding, unbinding the
declared kind, unbinding the link target, relaxing uniqueness, and relaxing the
exact physical line to a substring of the section.

Suite: 408 tests with the parser, green under `-W error::ResourceWarning`;
`compileall` clean. Runtime dependencies are still empty.

This cycle decides promotion.

1. **Say plainly whether this tree is safe to MERGE into protected `main`.** If
   nothing blocks promotion, say so explicitly.
2. Six cycles have found the next level down. Is there a seventh? The authored
   link now re-derives through the indexer's own parser and resolver, so the
   remaining gap would have to be in what the INDEX itself records — or in the
   retrieved-context and operator-citation paths, which have had far less
   attention this run than connections have.
3. Scrutinise the de-duplication above specifically: it is the one place where
   the exporter reconstructs an input to the indexer's resolver rather than
   reusing it, and getting it wrong in the other direction would reject genuine
   links or accept ambiguous ones.
4. Check every positive claim in `docs/task-contracts.md`, `ARCHITECTURE.md`,
   `PRIVACY.md` and `CHANGELOG.md` against the code.
5. Reassess every Critical and High from all twenty-two cycles and say which
   remain closed.
<!-- CYCLE-CONTEXT-END -->
