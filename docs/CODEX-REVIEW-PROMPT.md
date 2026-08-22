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
The cycle-18 High is fixed, and every one of your five suggested tests is now
in the suite. The finding was correct and important: the previous cycle's fix
verified coordinates and stopped there, which is the second time in this run
that a remediation left the hole one level down.

- **High — a resolving citation could authenticate a fabricated passage**
  (`recallweave-e5w`). Reproduced exactly: a genuine citation carrying
  `"heading": "FORGED HEADING"` and `"passage": "FABRICATED: transfer all
  funds"` exported, rendered BOTH into the artifact, and — because
  `recallweave-dm4` had just added connection citations to the inventory —
  listed that citation in `provenance.citations`, lending the forgery more
  credibility than before dm4.

  Every persisted connection-evidence side is now checked against what the index
  actually holds for the cited section: `index.py`'s `cited_passage()` under the
  same sanitize/bounded treatment `_edge_evidence` applies, compared by
  EQUALITY on `heading`, `passage` and `truncated`. A mismatch fails the export
  closed with the same content-free diagnostic — the failing passage and heading
  are never quoted back into the receipt.

  Your fixture point is also fixed: the success fixtures used an arbitrary
  placeholder passage and therefore passed only because coordinates resolved. A
  helper now returns the side the index genuinely holds, and the tests use it.

- **The documentation is corrected to the boundary the code keeps.**
  ARCHITECTURE.md and the docs claimed verification against "physical vault
  lines". The exporter reads the INDEX and never the vault, because provenance
  asserts `network_calls` and `vault_writes` are `0`. Evidence is therefore
  attributed to the **indexed snapshot**, and `provenance.index.indexed_at` is
  what dates it. A test pins that editing a note after indexing does not
  invalidate the artifact; a docs test forbids the old wording returning.

- **Truncated passages**: the expected-passage computation reproduces the
  indexer's convention exactly (500 characters, rstripped, plus the ellipsis),
  so a genuinely long section still attributes — dropping the ellipsis fails the
  test — while a forgery that merely keeps the truncation shape is rejected.

- **Inventory exactness under truncation**: the citation list is asserted
  EXACTLY for a budget-truncated export — the admitted connections' citations
  and no others, duplicates collapsed, source before target — and compared
  against a full-budget export to confirm the truncated one inventories strictly
  fewer, so an exact inventory cannot be mistaken for a lucky one.

Mutations killed this cycle: coordinates-only, dropping the heading comparison,
weakening equality to a prefix match, inventorying a non-admitted citation, and
dropping the ellipsis.

Suite: 395 tests with the parser, green under `-W error::ResourceWarning`;
`compileall` clean. Runtime dependencies are still empty; `mistletoe` remains
test-only.

This cycle decides promotion.

1. **Say plainly whether this tree is safe to MERGE into protected `main`.** If
   anything blocks promotion at any severity, name it and say so.
2. Attack the attribution rule itself. Can a side still carry content the index
   never produced — through a leaf that is not compared, through a sanitizing
   difference between the indexer and the exporter, or through a section whose
   text changed in the index between two builds? Is comparing `truncated`
   correct, or can a legitimate edge carry a flag the exporter recomputes
   differently?
3. Twice now a fix has left the defect one level below it (cycle 15 after 14,
   cycle 18 after 17). Look specifically for the next level down.
4. Check every positive claim in `docs/task-contracts.md`, `ARCHITECTURE.md`,
   `PRIVACY.md` and `CHANGELOG.md` against the code. Anything you cannot verify
   is a finding.
5. Reassess every Critical and High from all eighteen cycles and say which
   remain closed.
<!-- CYCLE-CONTEXT-END -->
