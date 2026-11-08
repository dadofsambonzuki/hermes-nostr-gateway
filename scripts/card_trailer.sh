#!/bin/sh
# card-trailer v1 -- every commit says which card wrote it.
#
# git runs this as `.git/hooks/commit-msg`, and every worktree of a clone shares that file, so
# one install covers each card the dispatcher creates afterwards -- nothing has to run inside
# the card for its commits to be attributed.
#
#   scripts/card_trailer.sh install [repo]      write the hook (default: this checkout)
#   scripts/card_trailer.sh uninstall [repo]    remove it, if it is ours
#   scripts/card_trailer.sh status [repo]       installed? and what would this shell write?
#   scripts/card_trailer.sh card                the value this shell would write
#   scripts/card_trailer.sh <message-file>      the hook: git passes the message file
#
# A card workspace writes `Card: t_xxxxxxxx` -- the dispatcher's task id, or the id in the
# worktree's own `.worktrees/t_xxxxxxxx-...` path. The operator's checkout writes
# `Card: operator`, which is how operator work says so instead of saying nothing. A message
# that already carries a Card trailer is left exactly as it is, so an operator working for a
# card writes the id themselves and this never overwrites it.
#
# Why the trailer and not the merge: `scripts/merge_pr.sh` could write an attribution when it
# merged, but it would be guessing for a PR whose commits never said -- and rewriting the
# commits to add one changes the history the PR was reviewed at. The trailer is the worker's
# fact, made once, and the merge route only checks it (exit 6 when a commit carries none).
#
# A worker can still `--no-verify` or delete the hook. That does not make an untrailered
# commit impossible; it makes it a deliberate act that the merge route then refuses.
#
# The rule and its enforcement point: docs/working-on-this-repo.md, "Every commit says which
# card wrote it".

set -u

MARKER="card-trailer"
SELF=$0
case "$SELF" in
  */*) SELF=$(cd "$(dirname "$SELF")" && pwd)/$(basename "$SELF") ;;
  *) SELF=$(pwd)/$SELF ;;
esac

#: shell case cannot express a repetition, so an eight-hex tail is spelled out.
TASK_CASE='t_[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'

usage() {
  cat <<'EOF'
usage: card_trailer.sh <command> [repo]

  install [repo]        write the hook into <repo>/.git/hooks/commit-msg
  uninstall [repo]      remove it, if it is ours
  status [repo]         installed? and what would this shell write?
  card                  the Card value this shell would write
  <message-file>        behave as the commit-msg hook

A card workspace is a shell the dispatcher marked (HERMES_KANBAN_TASK,
HERMES_KANBAN_WORKSPACE) or a checkout under .worktrees/. It writes
`Card: t_xxxxxxxx`; the operator's checkout writes `Card: operator`.
The rule is in docs/working-on-this-repo.md, "Every commit says which card wrote it".
EOF
}

# The task id at the front of a worktree's directory name, or nothing.
id_from_path() {
  path=${1%/}
  [ -n "$path" ] || return 1
  base=$(basename "$path")
  case "$base" in
    t_*) ;;
    *) return 1 ;;
  esac
  id=$(printf '%s' "$base" | cut -c1-10)
  case "$id" in
    $TASK_CASE) printf '%s' "$id"; return 0 ;;
  esac
  return 1
}

# Sets CARD to what this shell should write, or CARD_WHY and returns 1 when it cannot tell.
card_value() {
  CARD=""
  CARD_WHY=""
  task=${HERMES_KANBAN_TASK:-}
  if [ -n "$task" ]; then
    case "$task" in
      $TASK_CASE) CARD=$task; return 0 ;;
      *) CARD_WHY="HERMES_KANBAN_TASK is set to '$task', which is not a card id (t_ + 8 hex)"
         return 1 ;;
    esac
  fi
  workspace=${HERMES_KANBAN_WORKSPACE:-}
  if [ -n "$workspace" ]; then
    if CARD=$(id_from_path "$workspace"); then return 0; fi
    CARD_WHY="HERMES_KANBAN_WORKSPACE is set to '$workspace', which names no t_xxxxxxxx card"
    return 1
  fi
  top=$(git rev-parse --show-toplevel 2>/dev/null) || top=""
  case "$top" in
    */.worktrees/*)
      if CARD=$(id_from_path "$top"); then return 0; fi
      CARD_WHY="the checkout is a card worktree at '$top', which names no t_xxxxxxxx card"
      return 1
      ;;
  esac
  CARD="operator"
  return 0
}

# Whether the message already has a Card trailer in its trailer block. `--parse` reads only the
# trailer block, so a sentence that happens to start with "Card:" mid-body is not one.
has_card() {
  git interpret-trailers --parse "$1" 2>/dev/null | grep -qi '^Card:'
}

hook() {
  file=${1:-}
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    printf 'card trailer: expected a commit message file (git passes one)\n' >&2
    return 3
  fi
  if has_card "$file"; then
    return 0
  fi
  if ! card_value; then
    printf 'REFUSED: this is a card workspace but the commit cannot say which card it is.\n' >&2
    printf '\n  %s\n\n' "$CARD_WHY" >&2
    printf '  Set the dispatcher marker, or write the trailer yourself:\n\n' >&2
    printf '      git commit --trailer "Card: t_xxxxxxxx"\n\n' >&2
    printf '  A commit with no card is refused at merge (scripts/merge_pr.sh, exit 6):\n' >&2
    printf '  docs/working-on-this-repo.md, "Every commit says which card wrote it".\n' >&2
    return 1
  fi
  git interpret-trailers --in-place --trailer "Card: $CARD" "$file" || return 1
  return 0
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

install_hook() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  [ -n "$hooks" ] || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  mkdir -p "$hooks" || return 3
  dest=$hooks/commit-msg
  if [ -e "$dest" ] && ! grep -q "$MARKER" "$dest" 2>/dev/null; then
    printf 'refusing to replace %s: it exists and is not the card trailer\n' "$dest" >&2
    printf 'chain this hook from that one by hand, or install into another checkout\n' >&2
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

uninstall_hook() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  dest=$hooks/commit-msg
  if [ ! -e "$dest" ]; then
    printf 'not installed: %s\n' "$dest"
    return 0
  fi
  if ! grep -q "$MARKER" "$dest" 2>/dev/null; then
    printf 'refusing to remove %s: it is not the card trailer\n' "$dest" >&2
    return 3
  fi
  rm -f "$dest" || return 3
  printf 'removed: %s\n' "$dest"
}

status_hook() {
  repo=${1:-.}
  hooks=$(hooks_dir "$repo") || { printf 'not a git checkout: %s\n' "$repo" >&2; return 3; }
  dest=$hooks/commit-msg
  state=1
  if [ -f "$dest" ] && grep -q "$MARKER" "$dest" 2>/dev/null; then
    if [ "$SELF" -ef "$dest" ] || cmp -s "$SELF" "$dest"; then
      printf 'hook: installed at %s (current)\n' "$dest"
    else
      printf 'hook: installed at %s (STALE: %s differs; re-run install)\n' "$dest" "$SELF"
    fi
    state=0
  elif [ -e "$dest" ]; then
    printf 'hook: NOT installed -- %s holds a different hook\n' "$dest"
  else
    printf 'hook: NOT installed -- no %s\n' "$dest"
  fi
  if card_value; then
    printf 'card: %s\n' "$CARD"
  else
    printf 'card: cannot be derived -- %s\n' "$CARD_WHY"
  fi
  return "$state"
}

case "${1:-}" in
  install) shift; install_hook "${1:-.}" ;;
  uninstall) shift; uninstall_hook "${1:-.}" ;;
  status) shift; status_hook "${1:-.}" ;;
  card) card_value && printf '%s\n' "$CARD" || { printf '%s\n' "$CARD_WHY" >&2; exit 1; } ;;
  -h | --help) usage ;;
  "")
    printf 'card_trailer.sh: expected a command or a commit message file\n' >&2
    usage >&2
    exit 3
    ;;
  *) hook "$1" ;;
esac
exit $?
