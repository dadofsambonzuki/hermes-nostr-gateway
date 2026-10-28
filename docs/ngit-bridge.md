# The ngit bridge

Issues on a channel's repository become work on that channel's board; its pull requests are
announced in the channel. One mapping, used in both directions.

## Configuration

The channel already resolves to a checkout, a project and a board (`concord.projects`, see the
README). The bridge reads that same map: `--channel` names the conversation, the entry supplies
the rest. Because a cron job has nothing but the conversation string, the entry it matches must
be keyed by the conversation's channel id (hex) or by the whole conversation
(`channel: concord:<community>:<channel>`); a friendly name cannot be turned back into an id
without the client. `--repo` and `--board` override a single run and never replace the mapping —
a channel with no entry is an error, because a bridge that guessed a checkout would file work in
the wrong tree.

```yaml
ngit:
  assignee: default        # the profile a task created from an issue is dispatched to
  reviews: true            # a PR also gets a review task (never a merge)
  review_assignee: default # the profile that review task is dispatched to
  # review_model: ...      # run the review on another model, via the task's own override
  # review_provider: ...   # that model's provider (only read alongside review_model)
  # relays:                # where 1618/1631 events are read; default: the repository
  #   - wss://relay.example # announcement's own relay hints, then relays.read
  lookback_hours: 168      # the first poll's floor; 0 reads everything the relay will give
  issues: true             # import kind-1621 issues
  prs: true                # announce kind-1618 PRs and kind-1631 merges
  priority: 0              # task priority for anything the bridge creates
  max_new: 20              # new tasks per poll; the rest wait for the next one
  max_prs: 10              # PR messages per poll; the rest wait for the next one
  watch: true              # the daemon wakes a pass on a mapped repo's event (see below);
                           # default on once `assignee` is set, `false` is cron-only
  watch_debounce_s: 30     # coalesce a channel's events this long before the pass
```

Trusted authors are the control pane's (`control.trusted_npubs` unioned with
`control.planes.concord.trusted_npubs`). With no trusted npubs configured every issue is held,
which is the safe default: a person is asked, not guessed.

The repository address is read from the checkout itself (`ngit repo --json`, the kind-30617
coordinate). Nothing about a particular box is compiled in.

## Running it

Cron, per channel, on the host that holds the checkout and the gateway:

```cron
*/10 * * * *  cd /path/to/gateway && hng ngit sync --channel concord:<community>:<channel> --json >> /var/log/hng-ngit.log 2>&1
```

Exit code 0 means every message reached the channel **or is queued and waiting for the daemon**
(the report says which); 1 means something was refused or could not be read, and the report
says what; 2 means the configuration is wrong (no assignee, no mapping, no board) and the
bridge did not touch anything. `--dry-run` reports what would happen and writes nothing — the
first run against a live channel should be one.

The better long-term shape is a live subscription off the daemon's read path, once the daemon is
stable; `hng ngit sync` is the smallest thing that is useful today and does not block that. The
daemon now offers that subscription as a *wake* — see "Waking the pass" below. Cron stays,
because a subscription can replace the wait, not the decision.

## Waking the pass

Cron is the floor: every ten minutes the pass runs, whatever happened. The daemon can also wake
a pass as soon as something changes, so a new issue or a revised PR shows up in seconds instead
of up to ten minutes.

The wait moves; the decision does not. A relay event says something was *proposed* — a revision
is another event, an issue can be resolved or reopened later, and a merge is a kind-1631 that has
to be matched against the branch. Only the repository says what is true, and only
`ngit issue list --json` and the checkout's refs say it. So the watcher subscribes, coalesces,
and runs `hng ngit sync --channel <conversation>` as a child process: the same command cron runs,
with the same store, cursor and idempotency keys. It never opens the board or the outbox and never
moves a cursor, so there is still exactly one writer to each.

What it subscribes to:

- kinds 1621 (issue), 1622 (comment), 1618 (PR), 1619 (PR revision) and 1631 (applied);
- a `#a` filter naming only the mapped repositories — the `concord.projects` entries that declare
  a board, resolved through their checkout's own `ngit repo --json` address — never a kind on
  its own and never the whole relay.

1619 is in that list on purpose: a revised PR is a new 1619 event, and a watcher that ignored it
would leave the review task's brief stale until the next cron pass.

Where it listens is the pass's own rule: `ngit.relays` when set, otherwise the repositories'
announcement relay hints and then `relays.read`, capped at eight. Listening somewhere the pass
does not read would wake it for events it will not see; listening only on `relays.read` would miss
the grasp servers a NIP-34 repository actually publishes to.

Debounce and single-flight:

- a channel's events are coalesced for `watch_debounce_s` (30) after the last one, with a ceiling
  of four intervals so a steady trickle still gets a pass rather than none;
- one pass per channel at a time; an event that arrives while a pass is running earns one more
  pass after it finishes, never a concurrent one;
- the same event from three relays, or replayed by a resubscribe, is one wake: event ids are
  remembered in a bounded set, and a wake is a flag rather than a queue.

Failure is the floor's job. A pass that cannot run (no `ngit`, a locked board, a moved checkout)
is logged and reported in `hng status`, and the next cron pass is unaffected. A daemon restart
loses every pending wake and nothing else: whatever the watcher missed is exactly what the next
ten-minute pass reads.

```yaml
ngit:
  watch: true             # default: on once ngit.assignee is set; false is exactly cron-only
  watch_debounce_s: 30    # coalesce a channel's events this long before running the pass
```

`hng status` prints the watcher — channel repositories, relays, passes woken, wakes still counting
down — and `hng status --json` carries the same under `ngit_watch`. `hng health` notes it when a
watcher was asked for but is not subscribed.

Two operational notes:

- **The sandbox.** The pass is a child of the daemon and inherits the unit's restrictions. With
  `ProtectHome=read-only` the boards directory must be writable (`ReadWritePaths`, as the shipped
  unit does): otherwise every woken pass fails to write its task while the cron pass — which runs
  outside the unit — keeps working, which is a confusing half-failure to diagnose.
- **Overlap with cron.** A cron pass and a woken pass can interleave for one channel. Tasks and
  notices carry idempotency keys, so the ordinary outcome is still one task and one notice; the
  board's check-then-insert is not atomic, though, so a duplicate is possible in that window. It
  is the same window a slow cron pass already has, and the alternative — a lock shared with cron —
  would mean changing the pass, which this deliberately does not do.

## Inbound: issues as work

- A kind-1621 issue becomes a task with the issue's subject as its title and its body, author
  npub and nevent in the description. The idempotency key is `ngit:issue:<event id>`, so a
  re-poll cannot create a second task for the same issue.
- The issue id is a **dedupe key, not an identity**. A human may file a second task for the same
  issue; the bridge only refuses to duplicate its own.
- A kind-1622 comment becomes a task comment, in `created_at` order, once (tracked per comment
  id). A comment added later is appended; nothing is rewritten.
- A trusted author's issue is created `ready` and the channel gets one notice,
  `issue <nevent> -> <task id>`.
- Any other author's issue is created `blocked` and the channel gets exactly one question naming
  the issue, the author, the nevent, the task and what a `yes` would do. The question is queued
  and the link stays `held` until the daemon takes it, so it is asked once when the channel is
  reachable and retried when it was not — and the idempotency key stops a retry becoming a
  second question.
- `max_new` bounds how many tasks one poll creates; the rest are counted as deferred and picked
  up by the next poll. A backlog or a spam run cannot fill the board and the channel at once.

## Answering a held issue

```bash
hng ngit answer <task-id|nevent> --yes --by <npub>
hng ngit answer <task-id|nevent> --no --by <npub> --reason "..."
```

- Only a trusted npub (the control pane's, for the concord plane) can answer: filing an issue
  does not authorise it.
- A `yes` promotes `blocked -> ready` and writes an audit comment naming the npub and the time;
  the link state becomes `authorised`. Answering again changes nothing.
- A `no` leaves the task blocked, records the reason as a comment, and marks the link
  `declined` so the issue is never asked about again.

## Outbound: PRs signalled for review

- A kind-1618 event posts one message — the PR's subject, its author npub, the branch and the
  nevent, "ready for review" — and creates one **review task** on the board, keyed
  `ngit:pr:<event id>`. Deduped by PR id (`ngit_links`), so the same PR is never announced
  twice, and the idempotency key means a re-poll cannot make a second task either.
- The task is titled `Review PR <first 8 of the event id>: <subject>` and its body names the
  PR's event id and nevent, the repository address, the author, the source branch
  (`branch-name`) and the target branch — NIP-34 carries no target tag, so it is the
  checkout's default branch, read from git. The brief: check the proposal out
  (`ngit pr checkout <nevent>`), read the diff, run the repository's gates (`ruff check`,
  `pytest`), report a verdict with reasons. **It never merges**: merging is the operator's,
  or a follow-up task.
- The notice names the task (`review task <id>`) instead of a second message announcing it.
  One PR, one channel event.
- `ngit.review_assignee` (default `default`) is the profile the task is dispatched to and
  `ngit.priority` its priority. `ngit.review_model` (and optionally `ngit.review_provider`)
  is written to the task's own `model_override` / `provider_override` — the columns
  `hermes kanban set-model` writes and the dispatcher already honours — so review can run on
  a stronger model than routine work without a second profile. A `review_model` /
  `review_provider` on the channel's `concord.projects` entry beats it for that channel
  alone; unset everywhere means the assignee profile's model.
- A kind-1631 event for a PR this bridge announced posts one follow-up, `merged as <commit>`
  (the `merge-commit` tag, or the first `applied-as-commits` value when a merge commit was
  not named), and completes the review task with a comment naming the merge. A merge that
  arrives for a task already completed is a no-op: nothing is written twice.
- A PR first seen *already* applied gets one message, the merged line — not a fake "ready for
  review" followed by a merge, and not a replay of history. There is nothing to review, so no
  task is made.
- The two halves fail independently. A task that cannot be made (a locked board, a missing
  database) still gets the PR its notice, with `no review task: <why>` as the last line; the
  cursor does not move past it, so a later poll makes the task and posts nothing — the
  channel is not told twice. A message that cannot be delivered leaves the link `pending`
  and is retried, task and all, on the next poll.
- A merge for a PR the bridge never announced posts nothing: the bridge narrates what it
  watched, it does not dump the past into a channel.
- The author is named in every message, including PRs this gateway opened itself.
- `max_prs` bounds both halves of a poll: how many new notices it decides on, and how many
  queued ones it hands to the daemon. The first run, or a catch-up after a long outage, must
  not dump a month of merges into a channel; the remainder is counted as deferred and picked
  up on later polls.

## Delivery: decide, queue, then record

A poll decides what the channel should hear, writes each notice into `ngit_outbox`, and only
then hands it to the daemon. The record moves when the daemon says it took the message, not
before:

- **A busy daemon is a delay.** A running turn holds the daemon past the 20s socket timeout.
  That is `NotifyBusy`, not an error: the notice stays queued, the pass reports it as `pending`
  and exits 0, and the next pass delivers it. A refusal — no channel, no keys — is an error,
  because the next pass would hear the same thing; the notice still stays queued, so fixing the
  cause delivers it.
- **One timeout per pass.** The drain stops at the first notice the daemon did not take: it has
  already ignored one for the whole timeout, so the rest wait rather than each costing another.
- **A retry cannot double-post.** Every queued notice has its own idempotency key — the queue
  row's id, sent as the `send` op's `id`. The daemon records the key against the message it
  published, so a notice that landed but lost its acknowledgement is answered with that message
  rather than posted again. The same mechanism covers the issue notice and the question, which
  is why `held` (not `asked`) is the state until the question is delivered.
- **A merge supersedes an unsent review line.** If the PR merged before its review message went
  out, the queued line is replaced: the channel is not told a PR is ready for review after it
  has merged, and nothing that landed is hidden.
- **The cursor follows the queue, not the daemon.** It advances past an event whose notice is
  queued — the queue is durable — and stops short of one that was only deferred. A poll that
  reads nothing new still delivers what the queue holds: delivery does not depend on the relay
  window.
- **A reset starts from now.** If `ngit_cursors` loses a repo's cursor it was deleted by hand,
  and reading from `lookback_hours` would replay a week of PRs into the channel in bursts. The
  bridge starts from `now` (minus the 5-minute overlap) instead. A repo it has never read keeps
  the configured floor.
- `max_prs` bounds how many PRs one poll announces (message *and* task). The first run, or a
  catch-up after a long outage, must not dump a month of merges into a channel; the
  remainder is counted as deferred and picked up on later polls. The cursor only advances
  when a poll left nothing deferred, undelivered or without its task, so a backlog cannot be
  stepped over.

Messages go through the daemon's existing `send` op, so they are published as the gateway's
identity with the same signing, NIP-42 and recording as any other reply. There is no second
transport.

## State and recovery

In the gateway store (`store.py`):

- `ngit_links` — `(repo, kind, source event id) -> task id, board, state, note`. States:
  `ready`, `held`, `asked`, `authorised`, `declined`, `deleted` for issues; `pending`,
  `signalled`, `merged` for PRs. `pending` means the notice is not confirmed; `asked` is written
  only once the question reached the channel.
  `signalled`, `merged` for PRs. A PR's row carries the review task's id, which is what a
  merge completes; its `note` keeps the PR's nevent, subject, author and branches so a later
  poll can act on a merge whose event it no longer carries.
- `ngit_comments` — which comment has been copied onto which task.
- `ngit_cursors` — the last event time each repo's PR poll reached, with a 5-minute overlap so
  clock skew cannot skip an event; the link table, not the cursor, prevents a duplicate.
- `ngit_outbox` — one row per notice (`issue`, `question`, `ready`, `merged`), `queued` until the
  daemon took it and `sent` after, with an attempt count and the last error.
- `send_keys` — the daemon's receipts: which idempotency key produced which message.

Recovery:

- **A task deleted by hand** keeps its `deleted` link. The next sync reports it and does not
  recreate it. `--recreate` creates a new task and updates the link.
- **The daemon was busy or down** when a notice was due: the notice is queued, the report says
  how many are waiting, and the next pass delivers them in order. Nothing was lost and nothing
  was asked twice.
- **A `pending` PR with no notice in the queue** — a pass that died between writing the link and
  queueing the notice — is noticed on the next pass: the notice is made again from the link's own
  note (subject, author, branch, nevent) and the report says so. Without that, the cursor having
  moved past the PR would leave it stuck.
- **A relay read fails**: PRs are reported as an error and issues still import; the queue is
  still drained, so a notice already decided on is not held back by the relay.
- **Catch-up after downtime**: the cursor is where the last successful poll ended, so the next
  poll reads the gap; events already linked are not reported again, and notices already queued
  are delivered from the queue.

## Not in this bridge

- Publishing status events from the board back to Nostr (a task completing does not close or
  apply the issue). The mapping is one-way for status on purpose: the board's lifecycle is the
  board's, and a status event is a public statement someone should choose.
- Merging. A review task ends with a verdict and a comment on the task; the merge is the
  operator's (`ngit pr merge`), and the bridge only watches for the status event it produces.
- Retrofitting review tasks for PRs announced before this bridge made them. A PR already
  `signalled` with no task stays that way: the channel was told what happened at the time, and
  a change of behaviour is not a reason to rewrite it.
- PR revisions (kind 1619), drafts (1633) and labelling: the channel gets one signal per PR and
  one per merge.
- Any per-issue visibility rule. A private repository keeps everything derived from it private;
  the bridge carries no second set of rules.
