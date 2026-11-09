#!/usr/bin/env bash
# Apply a PR and gate it, in one command.
#
#   scripts/merge_pr.sh <pr-id|nevent>            apply onto master, gate, push, publish the record
#   scripts/merge_pr.sh <pr-id|nevent> --check    apply and gate only, never push
#
# Exit: 0 applied (or would), 1 conflict, 2 gate failure, 3 usage/state error,
#       4 a card workspace (not the operator's), 5 the live checkout moved,
#       6 a commit does not say which card wrote it, 7 the card is not in review or done,
#       8 merged, but the applied record (kind-1631) did not publish,
#       9 no verdict from a different npub.
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
# Why it publishes a kind-1631 after the push: `ngit pr apply` replays the proposal's
# patches as NEW commits, so the proposal's commits never become ancestors of master and
# ngit never infers `applied`. Without the status the content is in and the PR still reads
# `open`, and a board card guarded by `done` requires `applied` refuses to finish. The
# commit in the record is the one the push left (`git rev-parse HEAD` in the worktree), and
# the event carries the repository `a` coordinate because that is what a repository feed
# filters on — see `hng ngit applied`.
#
# Why it cannot move the clone: the daemon serves this checkout, so a script that leaves it
# on a proposal branch makes that proposal live. Everything below runs in a throwaway
# worktree, the clone's HEAD is snapshotted before the first git command and verified on
# exit (exit 5 if it moved), and scripts/live_checkout_guard.sh is the helper for any script
# that thinks it needs a branch.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-$REPO/.venv}
PYTHON=${PYTHON:-$VENV/bin/python}
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

# The tip the merge will actually apply, read from the remote now rather than from the
# bridge's last pass: a push after the approval is exactly the revision the merge must not
# land on the old word. A fork branch the origin does not carry gives no tip, and the check
# falls back to the link's recorded one.
branch=$(timeout 60 ngit pr view "$pr" 2>/dev/null | awk '/^Branch:/ {print $2}')
tip=""
if [ -n "$branch" ]; then
  tip=$(git -C "$REPO" ls-remote origin "refs/heads/$branch" 2>/dev/null | awk '{print $1}')
fi

# Authorship: a merge needs a verdict from a npub other than the PR's author. Stopping a card
# from pushing master answers "who may merge"; this answers "did anyone else look at it". The
# verdict is the NIP-22 kind-1111 comment on the PR root that the bridge publishes from the
# review card and the label pass reads -- one event, two readers -- so a self-authored PR
# merges only with a second party's signature. It reads the same `ngit pr view` the status
# above does, and refuses before anything is applied. Exit 9 is its own code -- 5 is the live
# checkout moving, 7 the card, 8 the applied record -- so a caller can tell which refusal it
# got. (5, then 6 and 7, then 8 were each taken by master while this refusal was in review.)
verdict_check=$(PYTHONPATH="${PYTHONPATH:-$REPO/src}" "$PYTHON" \
  -m hermes_nostr_gateway.ngit_verdict "$pr" 2>&1)
case $? in
  0) ;;
  9) echo "$verdict_check"; exit 9 ;;
  # Anything else is the check failing to run, not a verdict: a missing interpreter or an
  # import error must not read as "ask a reviewer".
  *) echo "$verdict_check"; exit 3 ;;
esac

wt=$(mktemp -d "${TMPDIR:-/tmp}/merge-pr.XXXXXX")
git worktree add --detach -q "$wt" origin/master || { echo "FAIL worktree"; exit 3; }

if ! (cd "$wt" && timeout 180 ngit pr apply "$pr" >/dev/null 2>&1); then
  echo "CONFLICT $pr against origin/master"
  git -C "$wt" diff --name-only --diff-filter=U | head -5
  exit 1
fi

# Authorship, on the commits this PR adds. Every one of them has to say which card wrote it,
# because the trailer is the only place `git log` can answer that without the board. It is
# checked here and never written here: a merge-time attribution would be a guess for a PR whose
# commits never said, and rewriting them would change the history the review covered. The
# trailer itself is the worker's, written by the commit-msg hook in scripts/card_trailer.sh.
commits=$(git -C "$wt" rev-list origin/master..HEAD)
if [ -z "$commits" ]; then
  echo "FAIL $pr applies no commits onto origin/master"
  echo "     it is already in master (a rebased proposal): close it with a kind-1632 status event"
  # A re-push can publish a second proposal for the same branch while this one is being
  # merged; the close then has to name the one that survived. Ask the checkout what else is
  # open on this branch and print the exact command, so the reason is never empty.
  successor=$(PYTHONPATH="${PYTHONPATH:-$REPO/src}" "$PYTHON" -m hermes_nostr_gateway.ngit_close \
    successors "$pr" --repo "$REPO" 2>/dev/null) || successor=""
  if [ -n "$successor" ]; then
    printf '%s\n' "$successor" | sed 's/^/     /'
  else
    echo "     no other open proposal shares this branch; name the successor in the reason anyway:"
    echo "     ngit pr close $pr --reason 'Superseded by <id> (tip <sha>): <why>'"
  fi
  exit 3
fi
missing=()
unnamed=()
cards=" "
for sha in $commits; do
  values=$(git -C "$wt" show -s --format='%(trailers:key=Card,valueonly)' "$sha" \
    | sed '/^[[:space:]]*$/d')
  if [ -z "$values" ]; then
    missing+=("$(git -C "$wt" show -s --format='%h %s' "$sha")")
    continue
  fi
  while IFS= read -r value; do
    [ -n "$value" ] || continue
    case "$value" in
      operator|t_[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
      *) unnamed+=("'$value' (on $(git -C "$wt" show -s --format='%h' "$sha"))"); continue ;;
    esac
    case "$cards" in
      *" $value "*) ;;
      *) cards="$cards$value " ;;
    esac
  done <<< "$values"
done

if [ ${#unnamed[@]} -gt 0 ]; then
  printf 'REFUSED: a Card trailer names something that is not a card.\n' >&2
  for item in "${unnamed[@]}"; do printf '  %s\n' "$item" >&2; done
  printf '  A trailer is `Card: t_xxxxxxxx` (a board card) or `Card: operator`.\n' >&2
  printf '  Rule: docs/working-on-this-repo.md, "Every commit says which card wrote it".\n' >&2
  exit 6
fi

if [ ${#missing[@]} -gt 0 ]; then
  printf 'REFUSED: these commits carry no `Card:` trailer, so nothing says which wrote them:\n' >&2
  for item in "${missing[@]}"; do printf '  %s\n' "$item" >&2; done
  printf '\n  Install the hook once per clone, then push the branch again:\n\n' >&2
  printf '      scripts/card_trailer.sh install\n' >&2
  printf '      git push origin HEAD:refs/heads/pr/<slug>\n\n' >&2
  printf '  Or write the trailer yourself -- git commit --amend --trailer "Card: operator"\n' >&2
  printf '  fixes the tip; a whole branch takes a rebase with the same --trailer.\n' >&2
  printf '  Rule: docs/working-on-this-repo.md, "Every commit says which card wrote it".\n' >&2
  exit 6
fi

# And the board's half: the card has to be in `review` or `done`. The helper reads the bridge's
# `ngit_links` row for the PR root (the PR's own card) and every card a trailer names, so an
# operator's `Card: operator` does not release a worker's running card. Exit 7 is the refusal;
# 3 means the box cannot answer (no config, no board), which is this script's own state error.
set -- "$pr" --repo "$REPO"
if [ -n "$tip" ]; then set -- "$@" --tip "$tip"; fi
for value in $cards; do
  [ "$value" = operator ] && continue
  set -- "$@" --card "$value"
done
answer=$(PYTHONPATH="${PYTHONPATH:-$REPO/src}" "$PYTHON" -m hermes_nostr_gateway.ngit_merge \
  "$@" 2>&1)
code=$?
case "$code" in
  0) ;;
  7) printf '%s\n' "$answer" >&2; exit 7 ;;
  *) printf '%s\n' "${answer:-FAIL the card check did not run (exit $code)}"; exit 3 ;;
esac

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

# The push landed; the record is what makes the PR read `applied` and releases a card's
# guard. The commit is the worktree's HEAD — the one master now points at — and it has to
# be published from the live checkout, whose `ngit repo --json` names the address and the
# relays this repository's readers use. `HNG_CONFIG` and `HNG_NSEC_FILE` are for an operator
# whose config or signer is not this box's default; the record is signed by the key that
# publishes it, so a merge made with another maintainer key publishes with that key too.
commit=$(git -C "$wt" rev-parse HEAD)
hng=("$VENV/bin/hng")
[ -n "${HNG_CONFIG:-}" ] && hng+=(--config "$HNG_CONFIG")
nsec=()
[ -n "${HNG_NSEC_FILE:-}" ] && nsec+=(--nsec-file "$HNG_NSEC_FILE")
record_cmd=("${hng[@]}" ngit applied "$pr" --commit "$commit" --repo "$REPO" "${nsec[@]}")

if ! record=$("${record_cmd[@]}" 2>&1); then
  echo "FAIL applied $pr: content is in master ($commit), the applied record is not published"
  printf '%s\n' "$record" | sed 's/^/     /' | head -3
  echo "     until it is, the PR reads open and a guarded card refuses completion. Retry:"
  printf '     '; printf '%q ' "${record_cmd[@]}"; echo
  exit 8
fi
echo "OK   applied $pr | $tests | master $(git -C "$REPO" rev-parse --short origin/master)"
echo "     $record"

