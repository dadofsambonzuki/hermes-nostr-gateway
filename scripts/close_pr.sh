#!/usr/bin/env bash
# Close a duplicate proposal, naming the successor that replaced it.
#
#   scripts/close_pr.sh <duplicate-pr> --successor <pr-id|nevent> [--reason "why"] [--tip <sha>]
#
# `ngit pr close` accepts an empty reason, and an empty kind-1632 reads as a withdrawal: a
# reviewer gets no pointer to the proposal that survived. This route refuses to publish one.
# It reads the successor's tip from ngit, builds `Superseded by <id> (tip <sha>): <why>`,
# runs `ngit pr close --reason`, and reads the status back before reporting success.
#
# Exit: 0 closed, 2 refused (no successor, a proposal that is not open, no tip to name),
#       3 usage or ngit could not be read.
set -uo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
VENV=${VENV:-$REPO/.venv}
PYTHON=${PYTHON:-$VENV/bin/python}

# The everyday call is `close_pr.sh <duplicate> --successor <id>`; `successors` is the
# read-only lookup the merge route uses.
case "${1:-}" in
  successors|close) ;;
  *) set -- close "$@" ;;
esac

exec env PYTHONPATH="${PYTHONPATH:-$REPO/src}" "$PYTHON" -m hermes_nostr_gateway.ngit_close "$@"
