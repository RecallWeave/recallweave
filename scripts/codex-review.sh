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

# Resolve the comparison base WITHOUT assuming any integration-branch name, so the
# helper works in a fresh clone or a contributor fork. Order of preference:
#   1. the checked-out branch's own upstream (`@{upstream}`),
#   2. the remote default branch (`origin/HEAD`),
#   3. a local default branch (`main`/`master`).
# Prints the resolved ref on stdout; returns non-zero if none is found. Operates
# on the git repository in the current directory.
resolve_base() {
  local base b
  if base=$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null) && [[ -n $base ]]; then
    print -- "$base"; return 0
  fi
  if base=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null) && [[ -n $base ]]; then
    print -- "$base"; return 0
  fi
  for b in main master; do
    if git rev-parse --verify --quiet "refs/heads/$b" >/dev/null 2>&1; then
      print -- "$b"; return 0
    fi
  done
  return 1
}

# Testable entry point: `codex-review.sh --print-base [DIR]` prints the resolved
# comparison base for DIR (default: current directory) and exits, or exits 3 with
# a precise message when no base can be determined. Handled before the cd below
# so it can be exercised against an arbitrary repository.
if [[ ${1:-} == --print-base ]]; then
  [[ -n ${2:-} ]] && { cd "$2" || exit 2; }
  if base=$(resolve_base); then print -- "$base"; exit 0; fi
  print -u2 "no comparison base found (no upstream, no origin/HEAD, no main/master)"
  exit 3
fi

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
# Fail closed: every suite command's status is captured. A later green command
# (e.g. "viewer tests: OK") must never mask an earlier failure. The brace group
# runs in the current shell (no subshell), so this flag persists past it.
suite_failed=0
{
  # Use an interpreter with the test extra installed, or the parser-backed
  # Markdown-inertness tests silently skip and the gate reports a green suite
  # that never ran its most important assertions.
  venv="$review_dir/.venv"
  if [[ ! -x $venv/bin/python ]]; then
    python3 -m venv "$venv" >/dev/null 2>&1 || suite_failed=1
  fi
  "$venv/bin/pip" install --quiet -e ".[test]" >/dev/null 2>&1 || suite_failed=1
  print "# PYTHONPATH=src .codex-reviews/.venv/bin/python -m unittest discover -s tests"
  PYTHONPATH=src "$venv/bin/python" -m unittest discover -s tests 2>&1 || suite_failed=1
  # Second pass with ResourceWarning promoted to an error. A leaked sqlite
  # connection or temp directory is finalized by the garbage collector at an
  # arbitrary later moment, and because the CLI tests capture stderr to parse
  # the JSON receipt, that warning can land in an unrelated test's captured
  # stream and break it. Promoting the warning makes the leak fail where it is
  # caused instead of somewhere else, hours later.
  print "\n# PYTHONPATH=src .codex-reviews/.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests"
  PYTHONPATH=src "$venv/bin/python" -W error::ResourceWarning -m unittest discover -s tests 2>&1 || suite_failed=1
  print "\n# python3 -m compileall -q src"
  if python3 -m compileall -q src 2>&1; then print "compileall: OK"; else suite_failed=1; fi
  if [[ -f viewer/package.json ]]; then
    print "\n# (cd viewer && npm test)"
    if (cd viewer && npm test) 2>&1; then print "viewer tests: OK"; else suite_failed=1; fi
  fi
  print "\n# suite_failed=$suite_failed"
} > "$suite" 2>&1
print "  $(tail -3 "$suite" | tr '\n' ' ')"

# Do NOT invoke the reviewer on a broken suite: a review that cites a failed or
# partial run is worse than no review. Preserve the full report and exit nonzero.
if (( suite_failed )); then
  print -u2 "suite FAILED; not invoking the reviewer. Full report: $suite"
  exit 1
fi

review_base=$(resolve_base 2>/dev/null || print "(none found; review the working tree as checked out)")

print "running codex review from $repo_root"
print "  base  : $review_base"
print "  brief : $brief"
print "  suite : $suite"
print "  output: $out"

codex exec \
  --cd "$repo_root" \
  --sandbox read-only \
  --color never \
  --output-last-message "$out" \
  - < <(cat "$brief"; \
        print "\n\n## Comparison base for this review\n"; \
        print "Compare HEAD against: $review_base"; \
        print "\n\n## Test results from this invocation\n"; \
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
