# Working on this repository

Notes for whoever is driving the checkout — an agent or a person. The gateway's own behaviour is
in the [README](../README.md); this is about changing it without burning a context window.

## Merging a PR

```sh
scripts/merge_pr.sh <pr-id|nevent>            # apply onto master, gate, push
scripts/merge_pr.sh <pr-id|nevent> --check    # apply and gate only
```

**PRs are not branches here.** The remote is an ngit remote: a fetch prunes `pr/*` refs back to
`master`, so `git merge origin/pr/<something>` answers *"not something we can merge"* however
cleanly the PR would apply. The patches ride on the PR event itself, so `ngit pr apply` is the
route. A proposal whose branch was rebased reads `open` with its content already in master; close
it with a maintainer kind-1632 status event, not a kind-5 deletion (the relay refuses a deletion
of someone else's event).

The script exists because the hand-rolled loop — status lookup, worktree, merge, ruff, pytest,
push — is a dozen tool round-trips, and each one re-sends the caller's entire context.

## Gates

`ruff check src tests scripts benchmarks` and `pytest -q`. Both must pass on the *merged tree*,
not on the PR branch: PRs that touch the same file (README, `gateway.py`, `cli.py`) merge clean
individually and can still disagree once combined.

## Cost discipline

Measured on this repository's own work: a long agent thread spent 133M input tokens on the main
conversation against 1.9M on every code review combined. The reviews were not the cost; the
context resend was.

- **One call per sub-procedure, output clipped at source** — `| tail -3`, or grep for the failure
  line, never the whole suite tail.
- **Use `scripts/merge_pr.sh`** rather than the manual loop.
- **Delegate sweeps** — merge queues, gate runs, PR archaeology — to a worker whose context is
  disposable; it returns a paragraph, the thread keeps the decisions.
- **Start a new session per workstream.** The kanban board is the handoff, so nothing is lost and
  the context baseline resets.
- **Reviews are cheap.** Keep them; a review that costs 2% of the bill is the best value here.
