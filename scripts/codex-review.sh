#!/bin/zsh
# Local, account-independent adversarial review gate.
#
# Runs the installed Codex CLI non-interactively against this repository with the
# review brief in docs/CODEX-REVIEW-PROMPT.md, under a read-only sandbox so the
# reviewer cannot modify the working tree. Verified against codex-cli 0.149.0:
#   - `codex exec` is the non-interactive entry point;
#   - `--cd DIR` sets the working root;
#   - `--sandbox read-only` blocks writes (verified: a write attempt reports
#     BLOCKED and leaves the tree unchanged);
#   - a trailing `-` reads the prompt from stdin;
#   - `--output-last-message FILE` captures the final review cleanly;
#   - `--ask-for-approval` is NOT an `exec` flag; passing it exits 2.
# Exit codes: 0 on a completed review, 2 on CLI usage error, non-zero otherwise.
# The review VERDICT is content, not an exit code — read the output file.

set -u

repo_root=${0:A:h:h}
cd "$repo_root" || exit 1

brief="docs/CODEX-REVIEW-PROMPT.md"
if [[ ! -f $brief ]]; then
  print -u2 "missing review brief: $brief"
  exit 1
fi

review_dir="$repo_root/.codex-reviews"
mkdir -p "$review_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$review_dir/review-$stamp.md"
log="$review_dir/review-$stamp.log"

print "running codex review from $repo_root"
print "  brief : $brief"
print "  output: $out"

codex exec \
  --cd "$repo_root" \
  --sandbox read-only \
  --color never \
  --output-last-message "$out" \
  - < "$brief" > "$log" 2>&1
status=$?

if [[ ! -s $out ]]; then
  print -u2 "codex produced no review (exit $status); see $log"
  exit ${status:-1}
fi

verdict=$(grep -m1 -E '^VERDICT:' "$out" || print "VERDICT: (not stated)")
print "$verdict"
print "$out"
exit $status
