# Dependency proposals (`hng deps`)

The scheduled pipeline that keeps the repositories' dependency manifests clear of known
vulnerabilities. It began as a standalone shell script with its own unit and its config in
the unit's environment; it now runs from the gateway's own CLI and reads the gateway's
`config.yaml`. The unit stays separate — that part was never the problem.

## Why it is not in the daemon

A run clones or fetches, scans with `osv-scanner`, rewrites a lockfile, then runs the
repository's own gates: `npm ci`, typecheck, lint, tests, build and a start-the-server smoke
test. That is minutes to an hour of npm, cargo and docker. In the daemon's poll loop it would
be a turn that holds the relay past every timeout; as a oneshot it can take as long as it
likes while the gateway keeps serving. `deploy/nostr-deps.service` is that oneshot,
`deploy/nostr-deps.timer` its daily schedule.

## Commands

```sh
hng deps run                 # every repository in deps.repositories
hng deps run --repo <name>   # one repository, for a re-run or a first look
hng deps run --dry-run       # say what would be proposed; push nothing, record nothing
hng deps dry-run             # the same command spelled as its own subcommand
```

A dry run still fetches, scans, applies the proposed bump and runs the gates, because that
is what decides whether a proposal is worth making — but it publishes nothing, triggers no
CI run, records no state, and puts the checkout back where it found it. It is the safe way
to see what the next scheduled run would do.

## Configuration

Everything site-specific lives in the `deps:` block of `config.yaml` (see
`config.example.yaml` for the shape and placeholders):

| Key | Meaning |
|---|---|
| `repositories` | `name`, `owner` (the npub or NIP-05 that published the repository) and `branch` a proposal is cut from |
| `min_severity` | the CVSS floor; `7` is high and above |
| `workspace` | where the bot's clones live (`/opt/deps-bot`); kept across runs, never inside a checkout |
| `state_dir` | last proposal's lockfile hash per repository, so a pending bump is not repeated; and the kept gate log of the last failure, `build-<name>.log` |
| `log` | the run log, appended by the oneshot and readable by `journalctl` too |
| `signer` | the Deps Bot npub that signs the proposal push and the scan trigger |
| `coordinator` | whose name a manual CI run is requested from |
| `workflow` | the CI workflow triggered for a fresh signed scan |
| `open_prs` | `false` scans, bumps and gates but publishes nothing |
| `board` | the kanban board a published proposal's review task is filed on; empty means no card |
| `review_assignee` | the profile that task is dispatched to (a review role, as in `ngit.review_assignee`) |

A misspelled key is a config error, not a silent default: a unit that quietly stops
proposing is worse than one that fails.

## What a run does per repository

1. Clone into `workspace/<name>` if it is not there; otherwise fetch and reset `work/deps`
   to `origin/<branch>`.
2. Ask for this week's signed scan (`ngit ci trigger`) so a scan result exists even in a
   week with nothing to bump.
3. Count the findings at or above the CVSS floor with `osv-scanner scan`.
4. Let `osv-scanner fix --strategy in-place` propose the minimum upgrade. No diff, no
   proposal: the manifest is left untouched.
5. Run the repository's own gates against the bump. A bump that fails a gate is discarded,
   never proposed; the failing gate's output is kept at `state_dir/build-<name>.log`, so the
   `(see …)` path the journal prints still opens after the run has returned.
6. Compare the lockfile's hash with `state_dir/last-<name>`: the same proposal still waiting
   for review is not repeated.
7. Commit as the Deps Bot and push `pr/deps-<date>` with a title and a description naming
   what the bump clears. The `nostr://` remote publishes it as a pull request.
8. File the review task (when `deps.board` is set): `Review PR <short>: dependency bumps`,
   keyed `deps:pr:<event id>`, with the PR's id and nevent, the repository, the target
   branch and the scan's `before -> after` counts in the body. The card is guarded: `done`
   requires the PR to be `applied`, so it cannot be closed before the merge.

## The proposal's card, and the merge that finishes it

A published proposal is work, not only a branch, so a run files it on the board exactly as
the ngit bridge files a PR's review card (`docs/ngit-bridge.md`, "Review tasks"): one card
per PR root, deduplicated on the event id, so a second run of the scan adds nothing. The
body carries the brief — read the diff, reproduce the scan, run the gates, publish a
verdict, never merge — and the reviewer parks the card with `kanban_block` rather than
completing it.

The next run reconciles the last proposal before it decides anything new, and it reads the
repository rather than its own memory:

- **merged** — a `Merge #<short>: <subject>` commit is on the target branch. That is the
  commit a kind-1631's `merge-commit-id` names (measured against a live one); the queue's
  own status lags it, because a proposal applied as patches still reads `open`. The card is
  completed with a comment naming that commit.
- **open** — `ngit pr list` still lists the branch: the card is the one already filed, and a
  re-push of the same proposal (a different bump on the same day) adds one comment naming the
  new tip instead of a second card.
- **gone without a merge** — no branch in the queue and no merge commit on the target: the
  proposal was withdrawn, so the card is blocked for a person once, naming what was checked.

`state_dir/proposal-<name>.json` records the branch, the PR's identity and the counts. It is
written before the PR is looked up, so a relay that has not caught up costs a run the lookup,
not the counts: the next run adopts the open proposal and files the card.

`git push` needs `nostr.signer` set per repository (the account store holds more than one
identity and the helper will not guess); the run sets it from `deps.signer` immediately
before the push.

## Installing the unit

The unit and timer are shipped in `deploy/`. `hng` is the gateway checkout's console script,
so the unit runs from that checkout and picks up whichever revision is deployed there:

```sh
cd /home/ubuntu/Projects/hermes-nostr-gateway
sudo install -m 0644 deploy/nostr-deps.service deploy/nostr-deps.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nostr-deps.timer

# prove it once through the unit, then read the journal
sudo systemctl start nostr-deps.service
journalctl -u nostr-deps.service -n 60 --no-pager
```

The unit's `ReadWritePaths` names the board directory (`~/.hermes/kanban`) as well as the
workspace and the state: the sandbox leaves `ProtectHome=read-only` in force, so a board
missing from that list is readable and never writable, and every run would fail to file its
card with `attempt to write a readonly database`. Reinstall the unit whenever the board
moves with `HERMES_HOME`.

The old `/usr/local/bin/nostr-deps-weekly` script is retired to a timestamped `.bak`; the
workspace and its clones do not move.

## Testing

`tests/test_deps.py` covers the `deps:` block (the full shape, the defaults and each way it
can be wrong), the report helpers, and the pipeline end to end against a miniature
repository with stub tools: a dry run that writes nothing, the "same proposal" guard,
`open_prs: false`, and a failing gate that leaves its output at the path the message names.
The board is a real SQLite database written with the kernel's own columns: the tests pin one
card per proposal, a re-run that adds nothing, the merge that completes the card with the
commit named, a withdrawn proposal that parks it once, and the adoption of an open proposal
when a run finds no record. `tests/test_deploy_units.py` pins the unit's shape: a oneshot
that calls `hng deps run`, never the daemon, that may write to the board directory.
