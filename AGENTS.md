# Agent Instructions

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

1. No note mutation — the vault is read-only.
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
