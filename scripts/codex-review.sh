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

suite="$review_dir/suite-$stamp.txt"

# Codex reviews under a read-only sandbox, which leaves it no writable temp
# directory, so the suite cannot run inside that sandbox (a first attempt saw 165
# of 268 tests error on FileNotFoundError for a temp dir). Run it here and hand
# Codex the real result instead, so it reasons from evidence rather than from
# environment noise.
print "running test suite for the reviewer -> $suite"
{
  print "# PYTHONPATH=src python3 -m unittest discover -s tests"
  PYTHONPATH=src python3 -m unittest discover -s tests 2>&1
  print "\n# python3 -m compileall -q src"
  python3 -m compileall -q src 2>&1 && print "compileall: OK"
} > "$suite" 2>&1
print "  $(tail -3 "$suite" | tr '\n' ' ')"

print "running codex review from $repo_root"
print "  brief : $brief"
print "  suite : $suite"
print "  output: $out"

codex exec \
  --cd "$repo_root" \
  --sandbox read-only \
  --color never \
  --output-last-message "$out" \
  - < <(cat "$brief"; print "\n\n## Test results from this invocation\n"; \
        print "The suite was run by the harness before you were invoked, because your"; \
        print "read-only sandbox has no writable temp directory. Read it at:"; \
        print "  ${suite#$repo_root/}"; \
        print "Do not report sandbox-induced temp-directory errors as findings, and do"; \
        print "not re-run the suite yourself; cite these results.") > "$log" 2>&1
rc=$?

if [[ ! -s $out ]]; then
  print -u2 "codex produced no review (exit $rc); see $log"
  exit ${rc:-1}
fi

verdict=$(grep -m1 -E '^VERDICT:' "$out" || print "VERDICT: (not stated)")
print "$verdict"
print "$out"
exit $rc
