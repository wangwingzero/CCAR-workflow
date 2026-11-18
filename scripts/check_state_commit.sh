#!/usr/bin/env bash
# Regression test for the "Commit state update" step in
# .github/workflows/check-updates.yml.
#
# Background — the failure this prevents (run 44348423270, 2026-11-18):
#   For `schedule` events `github.sha` is the default-branch head *at trigger
#   time*. The concurrency queue then delays the job, so by the time it starts
#   another run has already committed its own full snapshot of `data/*.json` and
#   `JS/*` (machine-generated files, rewritten in full every run). Committing a
#   second full snapshot on the stale base made `git pull --rebase` conflict on
#   `JS/manifest.json` and `data/downloads.json`; because `git rebase --abort`
#   always returned to the same stale base, all three retries reproduced the same
#   conflict and the job failed.
#
# The step now (1) fast-forwards to the branch tip before crawling and (2) commits
# only the state files the run actually rewrote, by parking them, hard-syncing to
# the newest tip, and copying them back — a snapshot replace, never a content
# merge. This script executes the real `run:` blocks extracted from the workflow
# file, so it cannot drift from what CI does.
#
# Usage: bash scripts/check_state_commit.sh   (needs git + ruby; exit 0 = pass)

set -u

repo_root=$(cd "$(dirname "$0")/.." && pwd)
workflow="$repo_root/.github/workflows/check-updates.yml"
ruby_bin=${RUBY:-ruby}

fail=0
note() { printf '%s\n' "$*"; }
check() { # check <description> <expected> <actual>
  if [ "$2" = "$3" ]; then
    note "  PASS  $1"
  else
    note "  FAIL  $1 (expected '$2', got '$3')"
    fail=1
  fi
}

if ! command -v "$ruby_bin" >/dev/null 2>&1; then
  note "SKIP: ruby not found (set RUBY=... to point at it)"
  exit 0
fi
if [ ! -f "$workflow" ]; then
  note "ERROR: workflow not found at $workflow"
  exit 1
fi

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

# Extract the real run blocks so the test always executes current CI logic.
"$ruby_bin" -ryaml -e '
  wf = YAML.load_file(ARGV[0])
  steps = wf["jobs"]["check-updates"]["steps"]
  ["Fast-forward to the branch tip", "Commit state update"].each do |name|
    step = steps.find { |s| s["name"] == name }
    abort("step not found: #{name}") unless step && step["run"]
    File.write(File.join(ARGV[1], name.tr(" ", "_") + ".sh"), step["run"])
  end
' "$workflow" "$T" || exit 1

commit_step="$T/Commit_state_update.sh"
ff_step="$T/Fast-forward_to_the_branch_tip.sh"

remote_file() { # remote_file <repo-dir> <path>
  git -C "$1" fetch --quiet origin main
  git -C "$1" show origin/main:"$2" 2>/dev/null || echo MISSING
}

new_repo() { # new_repo <dir>
  local d=$1
  rm -rf "$d"
  git init -q -b main --bare "$d/remote.git"
  git init -q -b main "$d/seed"
  (
    cd "$d/seed" || exit 1
    git config user.email a@b.c; git config user.name t
    mkdir -p data JS
    printf 'base-state'    > data/reg.json
    printf 'base-download' > data/downloads.json
    printf 'base-manifest' > JS/manifest.json
    printf 'code'          > code.py
    git add -A && git commit -qm base
    git remote add origin "$d/remote.git"
    git push -q origin HEAD:main
  )
}

clone_at() { # clone_at <dir> <target> [stale-sha]
  local d=$1 t=$2 stale=${3:-}
  git clone -q "$d/remote.git" "$d/$t"
  (
    cd "$d/$t" || exit 1
    git config user.email a@b.c; git config user.name t
    [ -n "$stale" ] && git checkout -q --detach "$stale"
  )
}

run_commit_step() { # run_commit_step <repo-dir>
  ( cd "$1" && bash "$commit_step" )
}

run_ff_step() { # run_ff_step <repo-dir>
  ( cd "$1" && bash "$ff_step" )
}

note "== step 1: fast-forward recovers a stale checkout =="
new_repo "$T/a"
ABASE=$(git -C "$T/a/seed" rev-parse HEAD)
clone_at "$T/a" r1 "$ABASE"
(
  cd "$T/a/r1" || exit 1
  printf 'newer-code' > code.py
  git add -A && git commit -qm "feat: newer code" && git push -q origin HEAD:main
)
ATIP=$(git ls-remote "$T/a/remote.git" refs/heads/main | cut -f1)
clone_at "$T/a" r2 "$ABASE"
run_ff_step "$T/a/r2" >/dev/null 2>&1 && rc=0 || rc=$?
check "fast-forward exits 0" "0" "$rc"
check "worktree is at the branch tip" "$ATIP" "$(git -C "$T/a/r2" rev-parse HEAD)"
check "newer code is present" "newer-code" "$(cat "$T/a/r2/code.py")"

note "== step 2: stale base + another run already pushed state =="
new_repo "$T/b"
BBASE=$(git -C "$T/b/seed" rev-parse HEAD)
clone_at "$T/b" run1 "$BBASE"
(
  cd "$T/b/run1" || exit 1
  printf 'RUN1-state' > data/reg.json
  printf 'RUN1-manif' > JS/manifest.json
  printf 'run1-only'  > data/only_in_run1.json
  git add -A && git commit -qm "state run1" && git push -q origin HEAD:main
)
clone_at "$T/b" run2 "$BBASE"
(
  cd "$T/b/run2" || exit 1
  printf 'RUN2-state' > data/reg.json
  printf 'RUN2-manif' > JS/manifest.json
)
run_commit_step "$T/b/run2" >/dev/null 2>&1 && rc=0 || rc=$?
check "commit step exits 0 (was: conflict x3 then exit 1)" "0" "$rc"
check "push landed" \
  "$(git -C "$T/b/run2" rev-parse HEAD)" \
  "$(git ls-remote "$T/b/remote.git" refs/heads/main | cut -f1)"
check "this run's snapshot wins" "RUN2-state" "$(remote_file "$T/b/run2" data/reg.json)"
check "manifest from this run wins" "RUN2-manif" "$(remote_file "$T/b/run2" JS/manifest.json)"
check "file only the other run created survives" "run1-only" \
  "$(remote_file "$T/b/run2" data/only_in_run1.json)"
check "file this run never wrote is untouched" "base-download" \
  "$(remote_file "$T/b/run2" data/downloads.json)"
check "no temp directory leaked into the commit" "0" \
  "$(git -C "$T/b/run2" ls-tree -r --name-only origin/main | grep -c 'tmp\.' )"

note "== step 3: nothing changed locally =="
new_repo "$T/c"
clone_at "$T/c" r2
out=$(run_commit_step "$T/c/r2" 2>&1) && rc=0 || rc=$?
check "exits 0" "0" "$rc"
check "reports no changes" "1" "$(printf '%s' "$out" | grep -c 'No state changes to commit')"
check "creates no commit" "1" "$(git -C "$T/c/r2" rev-list --count origin/main)"

note "== step 4: local edit identical to the remote tip =="
new_repo "$T/d"
DBASE=$(git -C "$T/d/seed" rev-parse HEAD)
clone_at "$T/d" r1 "$DBASE"
(
  cd "$T/d/r1" || exit 1
  printf 'SAME-value' > data/reg.json
  git add -A && git commit -qm s1 && git push -q origin HEAD:main
)
clone_at "$T/d" r2 "$DBASE"
(
  cd "$T/d/r2" || exit 1
  printf 'SAME-value' > data/reg.json
)
out=$(run_commit_step "$T/d/r2" 2>&1) && rc=0 || rc=$?
check "exits 0" "0" "$rc"
check "recognises the state already matches" "1" \
  "$(printf '%s' "$out" | grep -c 'already matches')"
check "no redundant commit" "2" "$(git -C "$T/d/r2" rev-list --count origin/main)"

note "== step 5: remote moves while this run is pushing =="
new_repo "$T/e"
EBASE=$(git -C "$T/e/seed" rev-parse HEAD)
clone_at "$T/e" r1 "$EBASE"
(
  cd "$T/e/r1" || exit 1
  printf 's1' > data/reg.json
  git add -A && git commit -qm s1 && git push -q origin HEAD:main
)
clone_at "$T/e" r2 "$EBASE"
(
  cd "$T/e/r2" || exit 1
  printf 's2' > data/reg.json
  printf 's3' > "$T/e/r1/data/downloads.json"
  git -C "$T/e/r1" add -A && git -C "$T/e/r1" commit -qm s3 \
    && git -C "$T/e/r1" push -q origin HEAD:main
)
run_commit_step "$T/e/r2" >/dev/null 2>&1 && rc=0 || rc=$?
check "exits 0 despite the late push" "0" "$rc"
check "this run's state wins" "s2" "$(remote_file "$T/e/r2" data/reg.json)"
check "the concurrent write is preserved" "s3" \
  "$(remote_file "$T/e/r2" data/downloads.json)"

note ""
if [ "$fail" -eq 0 ]; then note "ALL PASS"; else note "SOME CHECKS FAILED"; fi
exit "$fail"
