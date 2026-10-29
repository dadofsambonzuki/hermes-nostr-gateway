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
| Is the daemon up, and are relays hearing anything? | `hng status --json` | `identity`, `daemon` (bool), `event_times`, `pending`, `blocked`, `drops`, `wraps`, `relays[].verdict`/`detail`, `ngit_watch`. |
| What did policy refuse, and why? | `hng drops --json` | `drops[]` (plane, sender, kind, rule, why, reason, received_at, age_s, expired), `total`, `live`, `expired`, `ttl_s`. Add `--plane`, `--reason`, `--since-hours`. |
| What actually arrived? | `hng recent [N]` | Text rows only (no `--json`): time, direction, sender, reach, first 90 characters. |
| Does each channel map to a real checkout, project and board? | `hng projects --check --json` | Per entry: the mapping fields plus `checks[]` of `{name, ok, detail}` for `config`, `checkout`, `project`, `board`; exit 1 on any gap. Plain `hng projects --json` lists the map only. |
| Which profile, model and key would a conversation's turn use? | `hng route <conversation> --json`, or `hng route --check` for the whole config | The winning rule, `profile`, `model`/`provider`, `env_file`, and `ok`/`error` (`route_*`, `profile_*`). Exit 1 on an unresolved route. |
| What would a bridge pass do, without writing anything? | `hng ngit sync --channel concord:<community-id>:<channel-id> --dry-run --json` | `channel`, `repo`, `repo_address`, `board`, `assignee`, `issues{d seen,created,ready,held,existing,comments,deleted,deferred,would_create}`, `prs{seen,signalled,merged,existing,pending,tasks,review_errors,deferred,would_post}`, `posted`, `failed`, `errors`, `notes`. |
| One preflight of all of the above | `hng doctor --json` | `ok` and `checks[]` of `{name, ok, detail, fix}`; exit 1 when any check fails. |

Read-only questions are safe to run at any time: `status`, `health`, `drops`, `recent`,
`projects`, `route`, `doctor` and `ngit sync --dry-run` write nothing. `ngit sync` without
`--dry-run` writes tasks, comments, notices and cursors, and is what cron runs.

## Failure vocabulary

Every refusal is a word, and the word decides what to do.

- **blocked reasons on a message frame** (policy): `rate_limited` (a sender or rule bucket
  is exhausted), `echo` (a bot peer repeated our own recent text), `bot_reply_cap` (the
  bot-peer hourly cap), `not_allowlisted` (the rule or default is `drop`), and
  `digest_quota` (a digest overflowed and the tail was cut). A blocked frame carries no
  content and is never retried; the row stays as evidence.
- **blocked reasons from routing** (the turn was not dispatched): `route_unresolved`,
  `route_invalid`, `route_key_missing`, `profile_unknown`, `profile_invalid`. Fix the
  `routing`/`profiles` config, replay, and the stored message is delivered.
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
  `hng ngit sync` cron runs. An event only says *something was proposed*; a revision, a
  resolution and a merge all arrive as later events, so only the pass may change state.
- **Notices are queued, then recorded.** A PR notice is written `pending` before it is
  sent and marked delivered only when the daemon took it, so a pass cut off mid-send is
  retried with the same idempotency key instead of posting twice.
- **Review tasks are keyed `ngit:pr:<event id>`.** The key is what makes a re-poll or a
  wake idempotent: the board refuses a second task with the same key. Do not rename it.

## The ngit CLI it drives

The bridge shells out to `ngit`; these are the parts it depends on, not a copy of the CLI's
manual — run `ngit skill install` for the rest.

- A **PR is created by pushing a branch whose name starts with `pr/`**. Re-pushing the same
  commit answers *"proposal has no commits after latest tip of inferred PR"* — that means the
  PR already exists, not that the push failed.
- **`ngit pr checkout <id-or-nevent>`** gets a PR whose branch is not on the remote: the
  patches ride on the PR event itself. `git fetch` will not find it.
- **`ngit issue comment` takes issues only** and refuses a PR. Do not fake a kind-1622 by hand
  and assume it lands: a hand-published `e`/`a`/`p` comment is accepted by the relay and still
  does not appear in `ngit pr view --comments`.
- Read **`ngit pr view <id> --json`** for state, and judge a push by it — a self-hosted GRASP
  can fail its ref update while the ngit relay took the change.

## Traps, as rules

- Key every `concord.projects` entry by the channel **id** (hex), or by the whole
  `concord:<community-id>:<channel-id>` string. A friendly name resolves inbound messages
  fine and looks correct, but a cron pass that names the channel by id cannot match it, so
  every notice fails at 03:00 while manual polls pass.
- A cron line needs the **binary's directory on `PATH`**: the directory holding `hng`
  (a virtualenv's `bin`) and the one holding `ngit`. Cron's default `PATH` has neither, and
  the failure only appears in the log.
- A client must **expand the configured socket path** (`~` and `$VARS`) before connecting,
  exactly as the daemon does. A literal `~` in `socket:` makes a running daemon look down.
- A worktree task needs its **branch free**: the branch must not already be checked out in
  another worktree and the target path must not exist. Prune stale worktrees
  (`git worktree list`, `git worktree prune`) before dispatching rather than forcing.
- Pin the bridge's dependency: it shells out to `ngit` and needs a version at or above the
  floor `doctor` reports. Put that version on `PATH` for cron, not just for your shell.
- Trusted authors are `<npub>` values in `control.trusted_npubs` (and its per-plane
  lists), never names, and relays are `wss://relay.example`-style URLs in `relays.read` /
  `relays.publish` or in an announcement's own hints. Replace every placeholder
  (`wss://relay.example`, `<npub>`, `concord:<community-id>:<channel-id>`) with the
  deployment's own values; none is compiled in.
