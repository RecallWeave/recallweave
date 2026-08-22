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
Your cycle-11 findings are fixed.

- **High — the documentation lied about the disclosure surface.** You were right,
  and this was the most serious finding so far because it was a privacy claim,
  not a determinism one. `9ew.16` began rendering connection evidence but never
  updated the cycle-9 projected set, so the docs promised that connection
  `score` and `evidence` were omitted while the renderer emitted them, including
  vault passage text. The decision was to **render and document**, not to stop
  rendering: keeping evidence is coherent with `budget.characters_used`, which
  already charges for evidence passages and headings. The documented
  "not projected" list now contains only `retrieved_context[]` fields, and
  `PROJECTED_FIELDS` gained `evidence_class`, `score`, all six bilateral evidence
  leaves and `shared_terms[]`. You also correctly identified that the doc/test
  agreement test was **self-referential** — two identically incomplete lists
  agreeing with each other. It is replaced by a test anchored to actual renderer
  output: sentinels are injected into every documented-omitted field and the
  test fails if any leaks into the Markdown.

- **High — missing key versus explicit null.** Reproduced exactly as you
  reported: `task.id` distinguished them, every other field collapsed them. The
  renderer is now self-consistent, and the claim is scoped rather than hedged.
  Injectivity is stated over **well-formed** documents, where well-formed means
  every projected key that applies to an item's evidence class is present, and
  that condition is enforced by test rather than asserted in prose.

The scoping is deliberate and is the thing to attack hardest this cycle. We did
**not** make the builder emit every projected key unconditionally.
`_edge_evidence` sets `source_evidence` / `target_evidence` / `shared_terms` only
when the underlying edge carries them: a verified connection authored as a
wikilink has no TF-IDF `shared_terms`, and emitting `shared_terms: null` on it
would fabricate a field meaningless for that evidence class and blur the
verified/supporting/candidate boundary this project exists to preserve.

Suite: 358 tests with the parser, `compileall` clean.

Reassess every Critical and High from all eleven cycles, plus: whether
"well-formed" is a genuine, checkable condition or a hole large enough to make
the injectivity claim vacuous; whether "applies to that item's evidence class"
is defined precisely enough that a reader can determine it without reading the
implementation; whether any document a caller can actually obtain from the
public API fails well-formedness; and whether the sentinel-based projection test
can be defeated by a field whose rendered form does not contain its sentinel.
<!-- CYCLE-CONTEXT-END -->
