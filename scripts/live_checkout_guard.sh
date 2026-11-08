#!/bin/sh
# live-checkout-guard v1 -- the operator's clone stays on the branch it was on.
#
# The daemon runs from this checkout (editable install, `.venv/bin/hng serve`), so whatever
# branch HEAD is on there is the code the box serves. A script that needs a proposal checked
# out must take a worktree; it must never switch the clone itself.
#
#   scripts/live_checkout_guard.sh status [repo]            one line: branch @ sha (n behind master)
#   scripts/live_checkout_guard.sh check [repo]             exit 1 unless HEAD is master and has
#                                                           nothing origin/master lacks
#   scripts/live_checkout_guard.sh snapshot [repo]          a token naming HEAD, for `unchanged`
#   scripts/live_checkout_guard.sh unchanged <token> [repo] exit 1 when HEAD is no longer <token>
#   scripts/live_checkout_guard.sh worktree <path> [ref] [repo]
#                                                           add a worktree at <path> from <ref>
#                                                           (default origin/master), leaving
#                                                           the clone's own HEAD alone
#
# Exit: 0 fine, 1 the check failed (the message says what is wrong), 3 usage/not a checkout.
#
# The same rule, in the same words: `hng doctor`'s `checkout` check and
# docs/working-on-this-repo.md, "The live checkout stays on master".

set -u

usage() {
  cat <<'EOF'
usage: live_checkout_guard.sh <command> [repo]

  status [repo]              branch @ sha (n behind master)
  check [repo]               exit 1 unless HEAD is master with nothing origin/master lacks
  snapshot [repo]            a token naming HEAD, for `unchanged`
  unchanged <token> [repo]   exit 1 when HEAD moved since <token>
  worktree <path> [ref] [repo]  add a worktree at <path> (default origin/master)

A script that needs a branch takes a worktree; the operator's clone stays on master.
Rule: docs/working-on-this-repo.md, "The live checkout stays on master".
EOF
}

REPO=""
branch=""
sha=""
default_branch="master"

# Read HEAD and the branch origin calls its default. Returns non-zero when `repo` is not a
# checkout with a commit in it.
read_head() {
  REPO=${1:-.}
  if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    printf 'not a git checkout: %s\n' "$REPO" >&2
    return 3
  fi
  branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null) || branch=""
  sha=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null) || sha=""
  if [ -z "$sha" ]; then
    printf 'no commit checked out in %s\n' "$REPO" >&2
    return 1
  fi
  if [ "$branch" = "HEAD" ]; then
    branch="(detached)"
  fi
  remote_head=$(git -C "$REPO" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null) \
    || remote_head=""
  if [ -n "$remote_head" ]; then
    default_branch=${remote_head#origin/}
  fi
  return 0
}

behind_count() {
  git -C "$REPO" rev-list --count HEAD.."origin/$default_branch" 2>/dev/null || true
}

ahead_count() {
  git -C "$REPO" rev-list --count "origin/$default_branch"..HEAD 2>/dev/null || true
}

# The one line `hng status` prints. Returns 1 when the distance cannot be measured (there is
# no origin/<default> to measure against), which is also a failed check.
print_state() {
  behind=$(behind_count)
  if [ -z "$behind" ]; then
    printf 'checkout: unknown (no origin/%s in %s: git fetch origin %s)\n' \
      "$default_branch" "$REPO" "$default_branch"
    return 1
  fi
  ahead=$(ahead_count)
  extra=""
  if [ -n "$ahead" ] && [ "$ahead" != "0" ]; then
    extra=", $ahead ahead"
  fi
  printf 'checkout: %s @ %s (%s behind %s%s)\n' \
    "$branch" "$sha" "$behind" "$default_branch" "$extra"
}

status_line() {
  read_head "${1:-.}" || return $?
  print_state
}

check_live() {
  read_head "${1:-.}" || return $?
  print_state || return 1
  # The verdict is about the branch and about local commits, never about a dirty tree: a
  # proposal branch that passes its own gates is clean, which is the failure this catches.
  if [ "$branch" != "$default_branch" ]; then
    printf 'REFUSED: the live checkout is on %s, not %s.\n' "$branch" "$default_branch" >&2
    printf '         The daemon serves this tree, so the box runs code nobody merged.\n' >&2
    printf '         fix: cd %s && git checkout %s && git pull\n' \
      "$REPO" "$default_branch" >&2
    return 1
  fi
  ahead=$(ahead_count)
  if [ -n "$ahead" ] && [ "$ahead" != "0" ]; then
    printf 'REFUSED: %s has %s commit(s) origin/%s does not.\n' \
      "$default_branch" "$ahead" "$default_branch" >&2
    printf '         Move them to a proposal branch: the box must serve what origin has.\n' >&2
    return 1
  fi
  return 0
}

snapshot() {
  read_head "${1:-.}" || return $?
  printf '%s@%s\n' "$branch" "$(git -C "$REPO" rev-parse HEAD)"
}

unchanged() {
  want=${1:-}
  if [ -z "$want" ]; then
    printf 'usage: live_checkout_guard.sh unchanged <token> [repo]\n' >&2
    return 3
  fi
  read_head "${2:-.}" || return $?
  have="$branch@$(git -C "$REPO" rev-parse HEAD)"
  if [ "$have" != "$want" ]; then
    printf 'the live checkout moved: was %s, now %s (%s)\n' "$want" "$have" "$REPO" >&2
    printf 'a script that needs a branch takes a worktree: worktree <path> [ref]\n' >&2
    return 1
  fi
  return 0
}

add_worktree() {
  path=${1:-}
  ref=${2:-origin/master}
  repo=${3:-.}
  if [ -z "$path" ]; then
    printf 'usage: live_checkout_guard.sh worktree <path> [ref] [repo]\n' >&2
    return 3
  fi
  if ! git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    printf 'not a git checkout: %s\n' "$repo" >&2
    return 3
  fi
  # Detached on purpose: no local branch is created, so nothing made here can later be
  # mistaken for the operator's checkout.
  git -C "$repo" worktree add --detach -q "$path" "$ref" || return 1
  printf 'worktree: %s at %s (the clone itself is untouched)\n' "$path" "$ref"
}

case "${1:-}" in
  status) shift; status_line "${1:-.}" ;;
  check) shift; check_live "${1:-.}" ;;
  snapshot) shift; snapshot "${1:-.}" ;;
  unchanged) shift; unchanged "${1:-}" "${2:-.}" ;;
  worktree) shift; add_worktree "${1:-}" "${2:-origin/master}" "${3:-.}" ;;
  -h | --help) usage ;;
  *) usage >&2; exit 3 ;;
esac
exit $?
