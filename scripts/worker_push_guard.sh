#!/bin/sh
# worker-push-guard v1 -- a card workspace may not push master.
#
# git runs this as `.git/hooks/pre-push`, and every worktree of a clone shares that file, so
# one install covers each card the dispatcher creates afterwards -- nothing has to run inside
# the card for it to be guarded.
#
#   scripts/worker_push_guard.sh install [repo]     write the hook (default: this checkout)
#   scripts/worker_push_guard.sh uninstall [repo]   remove it, if it is ours
#   scripts/worker_push_guard.sh status [repo]      installed? is this shell a card workspace?
#   scripts/worker_push_guard.sh operator-only      exit 1 when this shell is a card workspace
#   scripts/worker_push_guard.sh check < refspecs   the decision, without pushing anything
#   scripts/worker_push_guard.sh                    the hook: refspecs on stdin, git passes two args
#
# A worker can still delete the hook or push with `--no-verify`. This does not make the push
# impossible; it makes an accidental merge into a deliberate one. The durable answer is
# identity (a merge signed by a key no worker holds), not this file.
#
# Why the whole clone and not just a card: hooks live in the shared git directory, so the
# guard has to be installed once per clone -- and then it must tell a card from the operator
# by itself. It uses the dispatcher's markers and the `.worktrees/` convention, never the
# branch that happens to be checked out.
#
# The rule and its enforcement point: docs/working-on-this-repo.md, "A card workspace
# cannot merge".

set -u

MARKER="worker-push-guard"
SELF=$0
case "$SELF" in
  */*) SELF=$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF") ;;
  *) SELF=$(pwd)/$SELF ;;
esac

usage() {
  cat <<'EOF'
usage: worker_push_guard.sh <command> [repo]

  install [repo]        write the hook into <repo>/.git/hooks/pre-push
  uninstall [repo]      remove it, if it is ours
  status [repo]         installed? is this shell a card workspace?
  operator-only         exit 1 when this shell is a card workspace
  check < refspecs      the decision, without pushing anything
  (no command)          behave as the pre-push hook

A card workspace is a shell the dispatcher marked (HERMES_KANBAN_TASK,
HERMES_KANBAN_WORKSPACE) or a checkout under .worktrees/. The rule is in
docs/working-on-this-repo.md, "A card workspace cannot merge".
EOF
}

# Print why this shell is a card workspace, or nothing (exit 1) when it is not one.
card_workspace_reason() {
  if [ -n "${HERMES_KANBAN_TASK:-}" ]; then
    printf 'the dispatcher handed this shell a card: HERMES_KANBAN_TASK=%s\n' "$HERMES_KANBAN_TASK"
    return 0
  fi
  if [ -n "${HERMES_KANBAN_WORKSPACE:-}" ]; then
    printf 'the dispatcher handed this shell a card: HERMES_KANBAN_WORKSPACE=%s\n' \
      "$HERMES_KANBAN_WORKSPACE"
    return 0
  fi
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  case "$top" in
    */.worktrees/*)
      printf 'the checkout is a card worktree: %s\n' "$top"
      return 0
      ;;
  esac
  return 1
}

# The first refspec on stdin that would update master/main, or nothing.
refused_target() {
  while read -r _local_ref _local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
      refs/heads/master | refs/heads/main)
        printf '%s' "$remote_ref"
        return 0
        ;;
    esac
  done
  return 0
}

refusal() {
  # $1 = the destination ref, $2 = why this shell is a card
  printf 'REFUSED: a card workspace cannot push %s.\n' "$1"
  printf '\n  %s\n\n' "$2"
  printf "  A merge is the operator's. Push the branch and let a review land it:\n\n"
  printf '      git push origin HEAD:refs/heads/pr/<slug>\n\n'
  printf '  Or ask the operator to run this from their checkout, after review:\n\n'
  printf '      scripts/merge_pr.sh <pr-id|nevent>\n\n'
  printf '  Rule and enforcement point: docs/working-on-this-repo.md, "A card workspace\n'
  printf '  cannot merge". Do not work around this inside the card -- say so on the card.\n'
}

hook() {
  target=$(refused_target)
  [ -n "$target" ] || return 0
  reason=$(card_workspace_reason) || return 0
  refusal "$target" "$reason" >&2
  return 1
}

operator_only() {
  reason=$(card_workspace_reason) || return 0
  printf 'REFUSED: this is a card workspace, so it cannot merge a PR.\n' >&2
  printf '\n  %s\n\n' "$reason" >&2
  printf '  Merges run from the operator checkout, after review:\n\n' >&2
  printf '      scripts/merge_pr.sh <pr-id|nevent>\n\n' >&2
  printf '  Rule: docs/working-on-this-repo.md, "A card workspace cannot merge".\n' >&2
  return 1
}

hooks_dir() {
  # $1 = repo; prints the shared hooks directory for the checkout (same for every worktree),
  # as an absolute path so the caller's own cwd cannot matter.
  ( cd "${1:-.}" 2>/dev/null || exit 1
    hooks=$(git rev-parse --git-path hooks 2>/dev/null) || exit 1
    case "$hooks" in
      /*) ;;
      *) hooks=$(pwd)/$hooks ;;
    esac
    printf '%s\n' "$hooks" )
}

install_guard() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  [ -n "$hooks" ] || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  mkdir -p "$hooks" || return 3
  dest=$hooks/pre-push
  if [ -e "$dest" ] && ! grep -q "$MARKER" "$dest" 2>/dev/null; then
    printf 'refusing to replace %s: it exists and is not the worker push guard\n' "$dest" >&2
    printf 'chain this guard from that hook by hand, or install into another checkout\n' >&2
    return 3
  fi
  if [ "$SELF" -ef "$dest" ]; then
    printf 'installed: %s\n' "$dest"
    return 0
  fi
  cp "$SELF" "$dest" && chmod 755 "$dest" || return 3
  printf 'installed: %s\n' "$dest"
  printf 'every worktree of this clone uses it, including cards this dispatcher makes later\n'
}

uninstall_guard() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  dest=$hooks/pre-push
  if [ ! -e "$dest" ]; then
    printf 'not installed: %s\n' "$dest"
    return 0
  fi
  if ! grep -q "$MARKER" "$dest" 2>/dev/null; then
    printf 'refusing to remove %s: it is not the worker push guard\n' "$dest" >&2
    return 3
  fi
  rm -f "$dest" || return 3
  printf 'removed: %s\n' "$dest"
}

status_guard() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  dest=$hooks/pre-push
  state=1
  if [ -f "$dest" ] && grep -q "$MARKER" "$dest" 2>/dev/null; then
    if [ "$SELF" -ef "$dest" ] || cmp -s "$SELF" "$dest"; then
      printf 'guard: installed at %s (current)\n' "$dest"
    else
      printf 'guard: installed at %s (STALE: %s differs; re-run install)\n' "$dest" "$SELF"
    fi
    state=0
  elif [ -e "$dest" ]; then
    printf 'guard: NOT installed -- %s holds a different hook\n' "$dest"
  else
    printf 'guard: NOT installed -- no %s\n' "$dest"
  fi
  if reason=$(card_workspace_reason); then
    printf 'shell: card workspace (%s)\n' "$reason"
  else
    printf 'shell: operator (no card marker, and this checkout is not under .worktrees/)\n'
  fi
  return "$state"
}

case "${1:-}" in
  install) shift; install_guard "${1:-.}" ;;
  uninstall) shift; uninstall_guard "${1:-.}" ;;
  status) shift; status_guard "${1:-.}" ;;
  operator-only) operator_only ;;
  -h | --help) usage ;;
  check | hook) hook ;;
  # git runs a pre-push hook with two arguments of its own (remote name and URL), so two or
  # more arguments is the hook invocation; anything else is a mistyped verb.
  *) if [ "$#" -ge 2 ]; then hook; else usage >&2; exit 3; fi ;;
esac
exit $?
