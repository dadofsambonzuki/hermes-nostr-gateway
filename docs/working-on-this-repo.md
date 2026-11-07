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

## A card workspace cannot merge

A kanban worker runs as the same user as the operator, so it holds push rights to `master`.
"Never merge" in a card body is not a control: four PRs were merged by card workers one
evening while the operator believed merges were waiting on review. The clone now carries a
`pre-push` guard that refuses a push to `refs/heads/master` or `refs/heads/main` from a card
workspace, and says why:

```text
REFUSED: a card workspace cannot push refs/heads/master.
  the checkout is a card worktree: …/.worktrees/t_0123abcd
  A merge is the operator's. Push the branch and let a review land it:
      git push origin HEAD:refs/heads/pr/<slug>
```

Hooks live in the clone's shared git directory, so every worktree uses the one file: a card
the dispatcher creates after the install is guarded without running anything inside the card.

```sh
scripts/worker_push_guard.sh install     # once per clone
scripts/worker_push_guard.sh status      # installed? is this shell a card workspace?
```

A shell is a **card workspace** when the dispatcher marked it (`HERMES_KANBAN_TASK` or
`HERMES_KANBAN_WORKSPACE`), or when the checkout is under `.worktrees/`. Those two are also
where the guard ends: a worktree made by hand from an unmarked shell is an operator's, and
`git push --no-verify` or deleting `.git/hooks/pre-push` skips the hook. Each of those turns
an accidental merge into a deliberate one, which is all a hook can honestly promise — it is
not a security boundary. The durable answer is identity: a merge signs with a key no worker
profile holds, and the remote accepts `master` only from that key (branch protection is not
available on a GRASP).

`scripts/merge_pr.sh` refuses in a card workspace too (exit 4), so the documented merge route
is not a way around the guard.

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
