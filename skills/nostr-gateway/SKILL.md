---
name: nostr-gateway
description: Use when driving this Nostr gateway: answering health questions, reading drops, routes and ngit bridge passes.
---

# Driving the Nostr gateway

`hng` is the interface: the daemon's socket and the CLI run the same core, so a question
answered here is the answer the daemon would give. `--config <path>` is a global flag, so it
goes before the verb (`hng --config <path> doctor`) when the file is not `config.yaml` or
`$HNG_CONFIG`. Start with `hng doctor --json`; it is the preflight for everything below and
every failing check names its own fix.

## Two engines, two kinds of failure

The daemon (`hng serve`) decides **per message**: policy gives one event exactly one reach,
the row is stored once, and the adapter is handed one frame. The ngit bridge
(`hng ngit sync`) decides **per pass**: it reads the repository, reconciles the board and
the `ngit_*` state tables, queues channel notices and moves each repository's cursor.
Attribute a failure to the engine that owns it. A message that never arrived or was
refused is the daemon's — ask `health`, `drops`, `recent`; a refusal is final for that
event and is not retried, so the fix is configuration or the sender resending. Work that
never appeared on the board, or a PR notice that never reached the channel, is the
bridge's — ask `ngit sync --dry-run`; a pass re-reads the repository every time, so most
pass failures clear on the next pass, and only its `failed`/`errors` counts are real.
The daemon's watcher only wakes a pass early; it never decides anything.

## Ask a question before acting

| question | command | `--json` carries |
| --- | --- | --- |
| Is anything wrong right now? | `hng health --json` | `verdict`, `degraded`, `reasons`, per-plane `planes`, `counts`, `relays` (verdict per relay), `notes`. The command exits non-zero when degraded, so a cron can read the exit code alone. |
| Is the daemon up, and are relays hearing anything? | `hng status --json` | `identity`, `checkout` (`branch`, `commit`, `behind`, `ahead`, `ok`), `daemon` (bool), `event_times`, `pending`, `blocked`, `drops`, `wraps`, `menu` (the catalogue the Hermes plugin wrote, and whether its last write failed), `relays[].verdict`/`detail`, `ngit_watch`. |
| What did policy refuse, and why? | `hng drops --json` | `drops[]` (plane, sender, kind, rule, why, reason, received_at, age_s, expired), `total`, `live`, `expired`, `ttl_s`. Add `--plane`, `--reason`, `--since-hours`. |
| What actually arrived? | `hng recent [N]` | Text rows only (no `--json`): time, direction, sender, reach, first 90 characters. |
| Does each channel map to a real checkout, project and board? | `hng projects --check --json` | Per entry: the mapping fields plus `checks[]` of `{name, ok, detail}` for `config`, `checkout`, `project`, `board`; exit 1 on any gap. Plain `hng projects --json` lists the map only. |
| Which profile, model and key would a conversation's turn use? | `hng route <conversation> --json`, or `hng route --check` for the whole config | The winning rule, `profile`, `model`/`provider`, `env_file`, and `ok`/`error` (`route_*`, `profile_*`). Exit 1 on an unresolved route. |
| What would a bridge pass do, without writing anything? | `hng ngit sync --channel concord:<community-id>:<channel-id> --dry-run --json` | `channel`, `repo`, `repo_address`, `board`, `assignee`, `issues{d seen,created,ready,held,existing,comments,deleted,deferred,would_create}`, `prs{seen,signalled,merged,closed,existing,pending,tasks,review_errors,deferred,would_post,revisions,reopened,approved,changes,applied,completed,unblocked,blocked,would_close}`, `posted`, `failed`, `errors`, `notes`. |
| Which cards are parked on a verdict no pass will read again? | `hng ngit reconcile --channel concord:<community-id>:<channel-id> --dry-run --json` | `channel`, `repo`, `repo_address`, `board`, `dry_run`, `cards[]{card,pr,from,to,assignee,verdict,verdict_event,why}`, `errors`, `notes`. Drop `--dry-run` to apply them; a second run moves nothing. |
| What does this identity advertise to clients? | `hng profile --json`, or `hng profile publish` to publish what differs | `advertise{enabled, targets, profile, catalogue, relays}` and `published[]` (`kind`, `event_id`, `content_hash`, `published_at`, `refused`, `retrying`). `publish` reports the event id per kind and the per-relay read-back (`retained`, `absent`, `superseded`). |
| Does the live Nostr plugin match this checkout's adapter? | `hng adapter status --json`; `hng adapter install` writes it | `repo`/`live` (both paths), `repo_hash`/`live_hash`, both mtimes, `equal`, `unit`, `unit_started`, `stale_import`, `problems`, `detail`; exit 1 when the copies differ or one cannot be read. |
| One preflight of all of the above | `hng doctor --json` | `ok` and `checks[]` of `{name, ok, detail, fix}`; exit 1 when any check fails. |

Read-only questions are safe to run at any time: `status`, `health`, `drops`, `recent`,
`projects`, `route`, `profile`, `doctor`, `adapter status`, `ngit sync --dry-run` and
`ngit reconcile --dry-run` write nothing. `hng profile publish` publishes the advertisement,
`hng adapter install` replaces the live plugin copy (after backing it up), `ngit sync` without
`--dry-run` writes tasks, comments, notices and cursors — it is what the scheduled pass runs —
and `ngit reconcile` without `--dry-run` moves the cards parked on a verdict (never a card
blocked on a person's question).

## Failure vocabulary

Every refusal is a word, and the word decides what to do.

- **blocked reasons on a message frame** (policy): `rate_limited` (a sender or rule bucket
  is exhausted), `echo` (a bot peer repeated our own recent text), `bot_reply_cap` (the
  bot-peer hourly cap), `not_allowlisted` (the rule or default is `drop`), and
  `digest_quota` (a digest overflowed and the tail was cut). A policy refusal is `final` on
  the frame — its row is never replayed — carries no content and is never retried; the row
  stays as evidence.
- **blocked reasons from routing** (the turn was not dispatched): `route_unresolved`,
  `route_invalid`, `route_key_missing`, `profile_unknown`, `profile_invalid`. These are not
  `final`: the row stays pending and the frame is re-offered, so fix the `routing`/`profiles`
  config and the stored message is delivered without anyone re-sending it.
- **refused vs dropped**: a *refused* message is `reach: drop` or carries a
  `blocked_reason`; it is what `hng drops` lists. *Dropped* with `digest_quota` means the
  digest quota cut it, not a rule. Both are final; a message never becomes deliverable by
  waiting.
- **pending vs delivered**: `pending` is a stored inbound row no adapter has taken yet
  (replay will deliver it) and, in the bridge, a PR notice that is queued but not yet
  confirmed. `sent`/`signalled` mean the daemon took it. A pass exits 0 when every notice is
  delivered *or queued*; only `failed` and `errors` are delivery failures.
- **held vs ready**: a bridge-created task is `ready` when its issue's author is trusted
  (`control.trusted_npubs` unioned with the concord plane's list) and `blocked` (held) when
  not — it stays on the board until a human answers with
  `hng ngit answer <ref> --yes|--no --by <who>`. `authorised`/`declined` are the recorded
  answers; `deleted` means a human removed the task and the bridge will not recreate it
  without `--recreate`.

## The bridge's rules

- **One pass owns store, board and cursor.** Never run two passes for one channel at once
  and never edit the `ngit_*` tables to "fix" a pass. The repository is the source of
  truth; the pass re-reads it and reconciles.
- **A relay wake never decides.** The watcher subscribes, coalesces and runs the same
  `hng ngit sync` the timer runs. An event only says *something was proposed*; a revision, a
  resolution and a merge all arrive as later events, so only the pass may change state.
- **Notices are queued, then recorded.** A PR notice is written `pending` before it is
  sent and marked delivered only when the daemon took it, so a pass cut off mid-send is
  retried with the same idempotency key instead of posting twice.
- **Review tasks are keyed `ngit:pr:<event id>`.** The key is what makes a re-poll or a
  wake idempotent: the board refuses a second task with the same key. Do not rename it.

## When a card blocks, say so

- **A block is an event, not a state.** When a card enters `blocked`, post one message in its
  channel: the card id, the reason, and the question it waits on. Once, on the transition — a
  repeated poll must not repeat the notice, and a silent block reads exactly like work that never
  started, which is how a board stalls with every health check green.
- **A verdict finishes the card.** Approve or changes-requested is published on the PR (a
  kind-1111 comment plus a label) and the bridge applies it to the PR's card on its next pass:
  `approved` completes the card with the verdict as its result (the merge is still the
  operator's), and `changes-requested` returns it to the implementer as `ready` with the
  findings. Neither leaves the card parked, so the board and the PR say the same thing.
- **A merged PR's card finishes itself, and the refusal says why.** `ngit pr apply` replays the
  patches as new commits, so the bridge does not trust ancestry or a status event alone: it
  compares patch ids against the base branch too. A refusal that names what it compared (`no
  applied status names it, and no commit on master carries its patch`) means the checkout has
  not fetched the proposal — run `git fetch origin` in it — not that the merge is missing.
- **Blocked is not a failure.** It is the board asking a question; answer it with the card, not by
  re-dispatching the work and hoping.

## Changing this repository

- **The live checkout stays on `master`.** The daemon serves this clone (editable install), so a
  clone left on a proposal branch is a box running code nobody merged, and `health` stays green
  through it. `hng doctor`'s `checkout` check fails on a proposal or detached HEAD and prints the
  fix; `hng status` prints `checkout: <branch> @ <sha> (<n> behind master)` as one line. A script
  that needs a branch takes a worktree — `scripts/live_checkout_guard.sh worktree <path> [ref]` —
  never `git checkout` in the clone.
- **A card workspace cannot merge, and that is enforced.** A `pre-push` hook installed by
  `scripts/worker_push_guard.sh` refuses `master`/`main` from a card worktree, and
  `scripts/merge_pr.sh` exits 4 there. If a push is refused, push the branch and say so on the
  card — `--no-verify` is the deliberate act the guard exists to make visible.
- **Merge a PR with `scripts/merge_pr.sh <pr-id>`**, not with `git merge`: an ngit fetch prunes
  `pr/*` refs to `master`, so the branch is not there to merge. The script applies the PR to a
  fresh worktree, runs the gates on the merged tree and pushes, in one command. It refuses a PR
  whose commits carry no card trailer — none at all, or one that names something that is not a
  card (exit 6) — and one whose card is not finished (exit 7: `review` or `done`; the PR's own
  card may also be the `blocked` a reviewer parks it as).
  `hng ngit merge-check <pr> --repo <checkout>` is the same card answer on its own.
- **Every commit says which card wrote it.** A `commit-msg` hook installed by
  `scripts/card_trailer.sh install` writes `Card: t_xxxxxxxx` from a card workspace and
  `Card: operator` from the operator's checkout. Write the id yourself when the work belongs to
  another card — the hook leaves an existing trailer alone — and never `--no-verify` it away.
- **Gate the merged tree.** PRs touching `README.md`, `gateway.py` or `cli.py` merge clean alone
  and can still disagree together.
- **One command per sub-procedure, output clipped at the source.** A status check costs the same
  as an answer; run four of them and you have paid for four answers.
- **Delegate sweeps to a worker and keep the decisions.** A disposable context can run the merge
  queue; the thread should hold the judgement calls.
- **Reviews are cheap — keep them.** Measured here: every review combined was ~1.4% of the tokens
  the main conversation spent on itself.

## The ngit CLI it drives

The bridge shells out to `ngit`; these are the parts it depends on, not a copy of the CLI's
manual — run `ngit skill install` for the rest.

- A **PR is created by pushing a branch whose name starts with `pr/`**. Re-pushing the same
  commit answers *"proposal has no commits after latest tip of inferred PR"* — that means the
  PR already exists, not that the push failed.
- **`ngit pr checkout <id-or-nevent>`** gets a PR whose branch is not on the remote: the
  patches ride on the PR event itself. `git fetch` will not find it.
- **`ngit issue comment` takes issues only** and refuses a PR (`ngit pr comment` is the PR
  form). Both publish NIP-22 **kind 1111** on 3.0.5 — never 1622, which no ngit release wrote.
  Do not fake one by hand and assume it lands: a hand-published comment is accepted by the
  relay and still does not appear in `ngit pr view --comments`.
- **A proposal a rebase orphaned is closed with a status event, not a deletion, and the event
  names what replaced it.** Merging a rebased branch leaves the PR's original commits outside
  the target, so it reads `open` with its content already in `master`; a kind-5 deletion is
  refused (*authored by another pubkey*). Close it with **`hng ngit closed <id> --reason
  "<why>"`**, which publishes a kind 1632 carrying the repository `a` coordinate — `ngit pr
  close` writes the same kind without that tag, so a repository reader never sees the close —
  and read it back with `ngit pr view`. The `--reason` names the successor PR id and tip:
  `Superseded by 08baeaaf (tip 3bbb86b): a re-push published a second proposal`. **Never
  publish an empty close**: `closed` with no content reads as a withdrawal and gives a reviewer
  no proposal to follow. `scripts/close_pr.sh <duplicate> --successor <id>` builds that reason
  from ngit's record and refuses to publish without one (`ngit pr close --reason`; pass the
  same reason to `hng ngit closed` when the `a` tag matters). A hand-published close passes
  `--reason` itself. Never leave a dead proposal for a reviewer to re-read.
- A **merged** proposal needs its own kind-1631: `ngit pr apply` re-signs the commits, so the
  proposal can never infer `applied`. `scripts/merge_pr.sh` publishes it after the push, and
  `hng ngit applied <id> --commit <full sha>` does it by hand (`--dry-run` shows the event).
- Read **`ngit pr view <id> --json`** for state, and judge a push by it — a self-hosted GRASP
  can fail its ref update while the ngit relay took the change.

## Traps, as rules

- **A merged adapter is not a deployed adapter.** Hermes does not import this repository's
  `integrations/hermes-plugin/adapter.py`; it loads a copy from
  `<HERMES_HOME>/plugins/nostr/adapter.py`. So `master` can carry the fix while the running
  gateway still holds the module it imported at its last restart, and every health check stays
  green. `hng adapter status` prints both SHA-256 hashes and warns when the process started
  before the copy was written (a stale import); `hng doctor`'s `adapter` check fails when they
  differ. The repair is `hng adapter install` — which backs the live copy up and never restarts a
  unit — followed by the restart **you** perform (`sudo systemctl restart hermes-gateway`).
- Key every `concord.projects` entry by the channel **id** (hex), or by the whole
  `concord:<community-id>:<channel-id>` string. A friendly name resolves inbound messages
  fine and looks correct, but a scheduled pass that names the channel by id cannot match it,
  so every notice fails at 03:00 while manual polls pass. A timer instance is the id alone, so
  the id keying is what lets `hermes-nostr-gateway-ngit@<id>.timer` resolve an entry that
  declares `community:` — and it must declare it, because a notice is addressed by the whole
  conversation and the id alone does not spell the community.
- A scheduled pass needs the **binary's directory on `PATH`**: the directory holding `hng`
  (a virtualenv's `bin`) and the one holding `ngit`. The unit carries both in
  `Environment=PATH`; a crontab line has to set the same thing itself, because cron's default
  `PATH` has neither and the failure only appears in the log.
- A client must **expand the configured socket path** (`~` and `$VARS`) before connecting,
  exactly as the daemon does. A literal `~` in `socket:` makes a running daemon look down.
- A worktree task needs its **branch free**: the branch must not already be checked out in
  another worktree and the target path must not exist. Prune stale worktrees
  (`git worktree list`, `git worktree prune`) before dispatching rather than forcing.
- Pin the bridge's dependency: it shells out to `ngit` and needs a version at or above the
  floor `doctor` reports. Put that version on the pass's `PATH`, not just in your shell.
- Trusted authors are `<npub>` values in `control.trusted_npubs` (and its per-plane
  lists), never names, and relays are `wss://relay.example`-style URLs in `relays.read` /
  `relays.publish` or in an announcement's own hints. Replace every placeholder
  (`wss://relay.example`, `<npub>`, `concord:<community-id>:<channel-id>`) with the
  deployment's own values; none is compiled in.
