# Agent Instructions


## Standing operator loop

After tests are green on a feature branch, do **not** wait for Josh:

1. **Push** the feature branch. Never force-push protected `main`/`master`.
2. **Open a PR.**
3. **Independent Codex review**, max **3 rounds per PR**, **unless P1s remain** (then keep remediating until P1s are gone). P2s do not block. GitHub Actions may be dead on spend — local tests + Codex still count.
4. **Merge** when no P1s remain, using this repo's merge path. If you temporarily drop required reviews to `0` to land, an EXIT trap **must** restore the prior count (typically `1`). Never force-push protected `main`/`master`.
5. **Close the tracker item** (`bd close`, or the equivalent).
6. **Immediately take the next ready work.** Escalate only for true emergencies.

Guidance for AI coding agents working on RecallWeave. This mirrors
[CLAUDE.md](CLAUDE.md); see that file and [ARCHITECTURE.md](ARCHITECTURE.md) for
the full picture. Everything here is generic to the public OSS project — no
private tooling or account is required.

## What this project is

RecallWeave is a local-first, evidence-cited discovery and resurfacing engine
for Obsidian vaults. The vault is canonical and read-only; the SQLite index is
disposable output. The default core makes no network calls and never mutates a
note. See [README.md](README.md).

## Build & test

```bash
python -m pip install -e ".[test]"
python -m compileall -q src
python -m unittest discover -s tests -v

cd viewer && npm ci && npm run lint && npm run typecheck && npm run build && npm test
```

Use only synthetic notes in tests and examples. Never stage real vault content,
absolute vault paths, database files, credentials, or personal names.

## Non-interactive shell commands

Prefer non-interactive flags so automated runs never hang on a confirmation
prompt (`cp`/`mv`/`rm` are aliased to `-i` on some systems):

```bash
cp -f source dest        # not: cp source dest
mv -f source dest        # not: mv source dest
rm -rf directory         # not: rm -r directory
```

`scp`/`ssh` accept `-o BatchMode=yes`; `apt-get` accepts `-y`.

## Invariants to preserve

1. No implicit note mutation — the vault is read-only for the engine and
   every read stage; only the policy-gated, operator-approved
   `steward-apply` may write, with journaled verified backups and rollback.
2. No network calls in the default core.
3. Evidence-class separation — authored (verified), discovery-candidate, and
   supporting (tag) signals stay visibly distinct; a candidate never becomes a fact.
4. Bounded, cited output — physical line-range citations, character budget, and
   an explicit truncation flag.
5. Versioned JSON output ([docs/json-output.md](docs/json-output.md)).

## Product boundary — local and single-user by construction

RecallWeave OSS is local and single-user by design. Anything requiring hosted
execution, cross-machine orchestration, multi-user/RBAC, centralized approvals,
managed secrets/connectors, fleet management, billing/metering, or proprietary
control-plane behavior **belongs outside this repository**, in the separate
commercial control plane. If a feature needs a server, another user, or someone
else's credentials to work, it does not belong in the OSS core. See the "Product
boundary" section of [ARCHITECTURE.md](ARCHITECTURE.md).

## Contributing

Follow [CONTRIBUTING.md](CONTRIBUTING.md). An optional local adversarial-review
helper lives at [`scripts/codex-review.sh`](scripts/codex-review.sh) and needs
only the `codex` CLI.
