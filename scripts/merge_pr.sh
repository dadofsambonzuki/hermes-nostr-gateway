#!/usr/bin/env bash
# Apply a PR and gate it, in one command.
#
#   scripts/merge_pr.sh <pr-id|nevent>            apply onto master, gate, push
#   scripts/merge_pr.sh <pr-id|nevent> --check    apply and gate only, never push
#
# Exit: 0 applied (or would), 1 conflict, 2 gate failure, 3 usage/state error,
#       4 a card workspace (not the operator's).
#
# Why by PR id and not by branch: this repository's remote is an ngit remote, and a
# fetch prunes `pr/*` refs back to `master`. `git merge origin/pr/...` therefore fails
# with "not something we can merge" however cleanly the PR would apply; the patches
# ride on the PR event, so `ngit pr apply` is the only reliable route.
#
# Why a script at all: doing this by hand is a dozen tool round-trips per PR — a
# status lookup, a worktree, a merge, two gate runs, a push — each one re-sending the
# agent's whole context. One command, a few lines of output, same result.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-$REPO/.venv}
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"

pr=""; check=0
for arg in "$@"; do
  case "$arg" in
    --check|--dry) check=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
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

status=$(timeout 60 ngit pr view "$pr" 2>/dev/null | awk '/^Status:/ {print $2}')
case "$status" in
  applied) echo "OK   already applied: $pr"; exit 0 ;;
  open|"") : ;;
  *) echo "FAIL $pr is ${status:-unknown}, not open"; exit 3 ;;
esac

wt=$(mktemp -d "${TMPDIR:-/tmp}/merge-pr.XXXXXX")
git worktree add --detach -q "$wt" origin/master || { echo "FAIL worktree"; exit 3; }
cleanup() { git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1; }
trap cleanup EXIT

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
