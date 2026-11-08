# Working in this checkout

This tree is **deploy-only**. The daemon on this host runs from it (`hermes-nostr-gateway.service`,
`WorkingDirectory=` this path), so whatever branch `HEAD` sits on is what is serving. A checkout
left on a proposal branch is a silent outage that every health check survives: `health` is green
for a gateway running code nobody merged.

## The rules

- **Leave `HEAD` on `master`.** Card work happens in a worktree (`scripts/merge_pr.sh` and the
  board's dispatcher each create their own), never by switching this clone. `hng doctor` fails if
  `HEAD` is not a commit on `origin/master`, and `hng status` prints the branch when it is not.
- **No direct commits — anywhere.** Every change is a branch, pushed as `pr/<name>` to the ngit
  remote, then applied with `scripts/merge_pr.sh <pr-id>`. Merging is an operator action after a
  review verdict; a worker opens the PR and stops.
- **Gate the merged tree.** `ruff check src tests scripts benchmarks` and `pytest -q` run on what
  the patch produces on top of current `master`, not on the PR branch: two PRs that each pass alone
  can still disagree once combined.
- **PRs are not branches.** The remote is an ngit remote and a fetch prunes `pr/*` refs, so
  `git merge origin/pr/...` fails however cleanly the PR applies. `ngit pr apply` is the route;
  `scripts/merge_pr.sh` wraps it.
- **Secrets never enter this tree.** The identity and the reviewer key live in
  `/etc/hermes-nostr-gateway/`; `config.yaml` here is gitignored and operator-owned.

## Where the pieces are

| Path | What |
|---|---|
| `src/hermes_nostr_gateway/` | planes, bridges, store, CLI (`hng`) |
| `integrations/hermes-plugin/` | the Hermes adapter, installed by copy into `$HERMES_HOME/plugins/nostr/` |
| `skills/nostr-gateway/` | the shipped skill, installed by `hng skill install` |
| `docs/decisions.md` | why, in the order it was decided — read before re-litigating |
| `docs/working-on-this-repo.md` | the merge route and the cost discipline |
| `.worktrees/` | card workspaces; never the main clone |

## When work blocks

A card that cannot proceed enters `blocked` with the question it waits on, and the question is
asked in its channel once. A blocked card is not a failure and not a pause to be retried — it is
the board asking a person something. Answer it with the card, not by re-dispatching the work.
