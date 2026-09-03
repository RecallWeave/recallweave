# Project Guide for AI Agents and Contributors


## Standing operator loop

After tests are green on a feature branch, do **not** wait for Josh:

1. **Push** the feature branch. Never force-push protected `main`/`master`.
2. **Open a PR.**
3. **Independent Codex review**, max **3 rounds per PR**, **unless P1s remain** (then keep remediating until P1s are gone). P2s do not block. GitHub Actions may be dead on spend — local tests + Codex still count.
4. **Merge** when no P1s remain, using this repo's merge path. If you temporarily drop required reviews to `0` to land, an EXIT trap **must** restore the prior count (typically `1`). Never force-push protected `main`/`master`.
5. **Close the tracker item** (`bd close`, or the equivalent).
6. **Immediately take the next ready work.** Escalate only for true emergencies.

This file orients an AI coding agent (or a new human contributor) working on
RecallWeave. It is generic guidance for the public OSS project — there is no
private tooling or account you need to reproduce anything here.

## What RecallWeave is

RecallWeave is a **local-first, evidence-cited discovery and resurfacing engine
for Obsidian vaults**. It reads a Markdown vault, builds a disposable external
SQLite index, and answers bounded, cited questions about what the vault says,
which notes may be connected, and which older notes are relevant again. It also
exports scoped, cited **task contracts** and a local **Atlas / Cold Trails**
graph viewer.

The vault is canonical and read-only: the core has **no write path back into
notes**, makes **no network calls**, and needs **no API key or model download**.

Start with [README.md](README.md) for usage and [ARCHITECTURE.md](ARCHITECTURE.md)
for the trust model, evidence classes, and the product boundary.

## Build & test

```bash
# Python package (editable install with test extra)
python -m pip install -e ".[test]"
python -m compileall -q src
python -m unittest discover -s tests -v

# Viewer (Node 22+; from the viewer/ directory)
cd viewer
npm ci
npm run lint
npm run typecheck
npm run build
npm test
```

CI runs the same steps across Linux/macOS/Windows and Python 3.11–3.13
(`.github/workflows/tests.yml`). Tests and examples must use only **synthetic**
notes — never real vault content, paths, or personal data.

## Non-interactive shell commands

When scripting file operations, prefer non-interactive flags so an automated
run never hangs waiting for a `y/n` prompt (`cp`/`mv`/`rm` are aliased to `-i`
on some systems):

```bash
cp -f source dest        # not: cp source dest
mv -f source dest        # not: mv source dest
rm -rf directory         # not: rm -r directory
```

`scp`/`ssh` accept `-o BatchMode=yes`; `apt-get` accepts `-y`.

## Contributing & review

See [CONTRIBUTING.md](CONTRIBUTING.md) for the pull-request checklist. In short:
run the full suite, keep test data synthetic, never stage vault paths / database
files / credentials / personal names, and preserve the invariants below.

A local, account-independent adversarial review helper is available at
[`scripts/codex-review.sh`](scripts/codex-review.sh) (runs the Codex CLI in a
read-only sandbox against [`docs/CODEX-REVIEW-PROMPT.md`](docs/CODEX-REVIEW-PROMPT.md)).
It is optional and requires only the `codex` CLI — no project-specific
infrastructure.

## Invariants to preserve

1. **No implicit note mutation** — the vault is read-only for the engine and
   every read stage (provably: no engine module can import the apply module);
   the only writer is `steward-apply`, which requires an explicit write
   policy, operator approval, and `--execute`, and journals verified
   backups with rollback. The index stays disposable output.
2. **No network calls** in the default core.
3. **Evidence-class separation** — keep authored (verified), discovery-candidate,
   and supporting (tag) signals visibly distinct; a candidate never becomes a fact.
4. **Bounded, cited output** — every passage carries a physical line-range
   citation, a character budget, and an explicit truncation flag.
5. **Versioned JSON** — see [docs/json-output.md](docs/json-output.md).

## Product boundary — local and single-user by construction

RecallWeave OSS is **local and single-user by design**. Anything that requires
hosted execution, cross-machine orchestration, multi-user or RBAC, centralized
approvals, managed secrets or connectors, fleet management, billing/metering, or
proprietary control-plane behavior **belongs outside this repository**, in the
separate commercial control plane — not in the OSS core.

When adding a feature, ask: *does this need a server, another user, or someone
else's credentials to work?* If yes, it does not belong here. See the "Product
boundary" section of [ARCHITECTURE.md](ARCHITECTURE.md).
