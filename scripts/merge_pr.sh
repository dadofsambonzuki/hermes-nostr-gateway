#!/usr/bin/env bash
# Apply a PR and gate it, in one command.
#
#   scripts/merge_pr.sh <pr-id|nevent>            apply onto master, gate, push
#   scripts/merge_pr.sh <pr-id|nevent> --check    apply and gate only, never push
#
# Exit: 0 applied (or would), 1 conflict, 2 gate failure, 3 usage/state error,
#       4 a card workspace (not the operator's), 5 the live checkout moved.
#
# Why by PR id and not by branch: this repository's remote is an ngit remote, and a
# fetch prunes `pr/*` refs back to `master`. `git merge origin/pr/...` therefore fails
# with "not something we can merge" however cleanly the PR would apply; the patches
# ride on the PR event, so `ngit pr apply` is the only reliable route.
#
# Why a script at all: doing this by hand is a dozen tool round-trips per PR — a
# status lookup, a worktree, a merge, two gate runs, a push — each one re-sending the
# agent's whole context. One command, a few lines of output, same result.
#
# Why it cannot move the clone: the daemon serves this checkout, so a script that leaves it
# on a proposal branch makes that proposal live. Everything below runs in a throwaway
# worktree, the clone's HEAD is snapshotted before the first git command and verified on
# exit (exit 5 if it moved), and scripts/live_checkout_guard.sh is the helper for any script
# that thinks it needs a branch.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-$REPO/.venv}
GUARD="$REPO/scripts/live_checkout_guard.sh"
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"

pr=""; check=0
for arg in "$@"; do
  case "$arg" in
    --check|--dry) check=1 ;;
    -h|--help) awk 'NR > 1 && /^set -u/ {exit} NR > 1 {print}' "$0"; exit 0 ;;
    *) pr="$arg" ;;
  esac
done
[ -n "$pr" ] || { echo "usage: scripts/merge_pr.sh <pr-id|nevent> [--check]"; exit 3; }

# A card workspace may not merge. This is the same guard that refuses a worker's push to
# master (scripts/worker_push_guard.sh), checked here so the documented route is not a way
# around it — and so it fails before the fetch rather than after the gate.
"$REPO/scripts/worker_push_guard.sh" operator-only || exit 4

cd "$REPO" || exit 3
git fetch -q origin 2>/dev/null || { echo "FAIL fetch"; exit 3; }

# Snapshot the clone's HEAD, then verify on every exit path — including the failures — that
# this script never switched it. The work happens in a worktree; the daemon reads this tree.
before=$("$GUARD" snapshot 2>/dev/null) || before=""
wt=""
cleanup() {
  if [ -n "$wt" ]; then
    git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1
  fi
  return 0
}
finish() {
  local code=$?
  cleanup
  if [ -n "$before" ] && ! "$GUARD" unchanged "$before" >/dev/null 2>&1; then
    printf 'FAIL the live checkout moved: was %s, now %s\n' "$before" \
      "$("$GUARD" snapshot 2>/dev/null)" >&2
    printf '     a script that needs a branch takes a worktree, never this clone:\n' >&2
    printf '     %s worktree <path> [ref]\n' "$GUARD" >&2
    code=5
  fi
  exit "$code"
}
trap finish EXIT

# A clone parked on a proposal branch still lets this merge land on origin/master, and that is
# the state the daemon must not stay in: say so once, without refusing a legitimate merge.
if ! "$GUARD" check >/dev/null 2>&1; then
  printf 'note: %s\n' "$("$GUARD" status 2>/dev/null)"
  printf '      this clone is not serving origin/master; return it afterwards:\n'
  printf '      git checkout master && git pull\n'
fi

status=$(timeout 60 ngit pr view "$pr" 2>/dev/null | awk '/^Status:/ {print $2}')
case "$status" in
  applied) echo "OK   already applied: $pr"; exit 0 ;;
  open|"") : ;;
  *) echo "FAIL $pr is ${status:-unknown}, not open"; exit 3 ;;
esac

wt=$(mktemp -d "${TMPDIR:-/tmp}/merge-pr.XXXXXX")
git worktree add --detach -q "$wt" origin/master || { echo "FAIL worktree"; exit 3; }

if ! (cd "$wt" && timeout 180 ngit pr apply "$pr" >/dev/null 2>&1); then
  echo "CONFLICT $pr against origin/master"
  git -C "$wt" diff --name-only --diff-filter=U | head -5
  exit 1
fi

(cd "$wt" && "$VENV/bin/ruff" check src tests scripts benchmarks >/dev/null 2>&1) \
  || { echo "FAIL ruff"; exit 2; }
tests=$(cd "$wt" && PYTHONPATH="$wt/src" "$VENV/bin/python" -m pytest -q 2>&1 | tail -1)
case "$tests" in
  *" failed"*|*" error"*) echo "FAIL $tests"; exit 2 ;;
esac

if [ "$check" = 1 ]; then
  echo "OK   would apply $pr | $tests"
  exit 0
fi

git -C "$wt" push -q origin HEAD:master || { echo "FAIL push"; exit 2; }
git -C "$REPO" fetch -q origin 2>/dev/null
echo "OK   applied $pr | $tests | master $(git -C "$REPO" rev-parse --short origin/master)"
