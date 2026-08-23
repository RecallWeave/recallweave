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
All three cycle-27 findings are fixed, and all seven of your suggested tests are
in the suite. **No open bead blocks promotion.**

- **Medium — non-canonical separator whitespace rejected a genuine heading
  link.** Confirmed exactly, including that it disproved the "complete physical
  line is reconstructed" claim. `note_headings` now stores the heading line
  EXACTLY as it appears — the same value the parser puts in `LinkEvidence.text`
  — and the exporter compares against that instead of rebuilding it from level
  and text. Storing beats reconstructing here: any canonical rebuild is a guess
  about formatting the source already settled, which is what made
  `##  Related` and `##<tab>Related` unrepresentable.

  Tests cover two spaces and a tab, for wikilinks and Markdown links, on bodied
  and bodyless headings — eight shapes, each first asserting the INDEXER really
  produces the edge. The mutation you asked for (reconstructing with canonical
  whitespace) fails, from both the query side and the parser side.

- **Medium — the spec-input section contradicted the authorship model.**
  Correct, and it was the sharper of the two doc findings: a reader who read
  only the input spec would come away with the classification
  `recallweave-nv0` removed. It now states that the evidence class depends on
  whether the gloss is present, and links to the detailed section.

- **Low — a heading-binding sentence named the removed `sections.heading`
  route.** Fixed; it names `note_headings.source_text`.

Both documentation fixes are pinned by tests asserting the obsolete wording
cannot return, and three mutations were killed.

Suite: **425 tests** with the parser, green under `-W error::ResourceWarning`;
`compileall` clean. Runtime dependencies still empty.

This route has now produced one bypass and three false rejections across four
cycles, every one of them a case where the exporter's idea of a heading was
narrower than the parser's. The binding no longer derives, reconstructs or
normalizes anything: it compares stored bytes.

1. **Say plainly whether this tree is safe to MERGE into protected `main`.** If
   nothing blocks promotion, say so explicitly and without qualification.
2. If you can find a fifth shape on this route — a heading the parser links from
   whose stored line the exporter would not match, or a stored line that could
   be matched by evidence the indexer would not have produced — name it. If you
   believe the route is now closed, say why.
3. Check every positive claim in `docs/task-contracts.md`, `ARCHITECTURE.md`,
   `PRIVACY.md` and `CHANGELOG.md` against the code. Two of the last three
   findings were documentation that had drifted behind the implementation.
4. Reassess every Critical and High from all twenty-seven cycles and say which
   remain closed.
<!-- CYCLE-CONTEXT-END -->
