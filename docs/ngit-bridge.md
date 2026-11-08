# The ngit bridge

Issues on a channel's repository become work on that channel's board; its pull requests are
announced in the channel. One mapping, used in both directions.

## Configuration

The channel already resolves to a checkout, a project and a board (`concord.projects`, see the
README). The bridge reads that same map: `--channel` names the conversation, the entry supplies
the rest. A systemd timer pass has nothing but the channel id — the unit instance — so the entry it
matches must be keyed by the channel id (hex). A crontab pass may instead name the whole
conversation, and an entry may be keyed by that (`channel: concord:<community>:<channel>`). A
friendly name cannot be turned back into an id without the client, so it cannot schedule a pass.
An id-keyed entry must also declare `community:`: a notice is addressed by the whole conversation,
which the id alone does not spell, so the pass refuses at the preflight rather than failing every
notice at delivery. `--repo` and `--board` override a single run and never replace the mapping — a
channel with no entry is an error, because a bridge that guessed a checkout would file work in the
wrong tree.

```yaml
ngit:
  assignee: default        # the profile a task created from an issue is dispatched to
  reviews: true            # a PR also gets a review task (never a merge)
  review_assignee: review  # profile that review task is dispatched to (a bot role)
  # relays:                # where 1618/1631 events are read; default: the repository
  #   - wss://relay.example # announcement's own relay hints, then relays.read
  lookback_hours: 168      # the first poll's floor; 0 reads everything the relay will give
  issues: true             # import kind-1621 issues
  prs: true                # announce kind-1618 PRs and kind-1631 merges
  priority: 0              # task priority for anything the bridge creates
  max_new: 20              # new tasks per poll; the rest wait for the next one
  max_prs: 10              # PR messages per poll; the rest wait for the next one
  labels: true             # publish each PR's review state as a NIP-34 label (see below)
  watch: true              # the daemon wakes a pass on a mapped repo's event (see below);
                           # default on once `assignee` is set, `false` is the timer alone
  watch_debounce_s: 30     # coalesce a channel's events this long before the pass
```

Trusted authors are the control pane's (`control.trusted_npubs` unioned with
`control.planes.concord.trusted_npubs`). With no trusted npubs configured every issue is held,
which is the safe default: a person is asked, not guessed.

The repository address is read from the checkout itself (`ngit repo --json`, the kind-30617
coordinate). Nothing about a particular box is compiled in.

## Running it

A systemd timer per mapped channel, on the host that holds the checkout and the gateway. The two
units ship as one template pair:

- `deploy/hermes-nostr-gateway-ngit@.service` — `Type=oneshot`, one pass, the unit instance is
  the channel id, and `LoadCredential=nostr_secret:/etc/hermes-nostr-gateway/identity.json` hands
  it the same identity the daemon reads. That is why the pass runs from a timer rather than from
  crontab: a user crontab has no `$CREDENTIALS_DIRECTORY`, so an `identity.secret_credential`
  would fail every scheduled pass while the daemon looked healthy. One credential form, no second
  copy of the key.
- `deploy/hermes-nostr-gateway-ngit@.timer` — every ten minutes, `Persistent=true` so a tick
  missed while the host was down runs at the next opportunity, and `RandomizedDelaySec=30` so
  instances due on the same minute do not start together. A pass that hangs is killed by the
  service's `TimeoutStartSec`, not left running into the next tick.

Install the pair once, then enable one instance per mapped channel id:

```sh
sudo install -m 0644 deploy/hermes-nostr-gateway-ngit@.service \
    deploy/hermes-nostr-gateway-ngit@.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now hermes-nostr-gateway-ngit@<channel-id>.timer
sudo systemctl enable --now hermes-nostr-gateway-ngit@<other-channel-id>.timer
```

The instance name is the `concord.projects` entry's `channel:` as written when it is keyed by id
(hex). `systemctl list-timers 'hermes-nostr-gateway-ngit@*'` shows each schedule and its last and
next run; `hng doctor` counts an enabled instance as that channel's pass and names the unit, once
the entry declares `community:` — without it the pass cannot address the channel from the id
alone, and the doctor says so instead of going green.

Crontab still works, and still counts in `hng doctor`, for a host that prefers it:

```cron
*/10 * * * *  cd /path/to/gateway && PATH=<dir holding hng>:<dir holding ngit>:$PATH hng --config <config> ngit sync --channel concord:<community>:<channel> --json
```

A crontab line cannot carry a systemd credential, though: with `identity.secret_credential` set,
the timer is the only scheduler that works, so that host should not keep both.

### Cutover, in the order that never leaves the bridge unscheduled

1. Check the credential the daemon already uses is there and root-owned:
   `sudo ls -l /etc/hermes-nostr-gateway/identity.json`.
2. Install the pair and enable one instance per mapped channel id (above). Both schedulers now
   run; the config still says `secret_file`, so nothing changes yet, and `hng doctor` is still
   green.
3. Remove the ngit crontab lines (`crontab -e`). The timers are the floor now.
4. In `config.yaml`, replace `identity.secret_file: …` with `identity.secret_credential:
   nostr_secret`, then `sudo systemctl restart hermes-nostr-gateway`. A crontab pass would fail
   from here on; a timer pass does not.
5. Prove the credential once by hand:
   `sudo systemctl start hermes-nostr-gateway-ngit@<channel-id>.service` and read
   `journalctl -u hermes-nostr-gateway-ngit@<channel-id>.service` — the pass reports what it
   did, and `identity` is the check that would have failed under cron.
6. Confirm an unattended tick does the same: `systemctl list-timers
   'hermes-nostr-gateway-ngit@*'` shows last and next run; the journal for that instance shows
   the same report without anyone starting it.
7. `hng doctor` is green, naming `systemd timer hermes-nostr-gateway-ngit@<channel-id>.timer`
   for every mapped channel.

Steps 2–7 are the operator's: the units ship here, but nothing installs, enables, starts or
rewrites the host's config on its own.

Exit code 0 means every message reached the channel **or is queued and waiting for the daemon**
(the report says which); 1 means something was refused or could not be read, and the report
says what; 2 means the run was refused before any pass work — bad configuration (no assignee,
no mapping, no board) or an unusable `ngit` — and the bridge did not touch anything.
`--dry-run` reports what would happen and writes nothing — the first run against a live
channel should be one.

Configuration is checked first: a missing `ngit.assignee`, an unmapped channel or a board that
is not there is the operator's to fix and does not depend on what is installed, so it is
refused before the host is read at all. Then, because the bridge shells out to `ngit`, that
binary is a pinned dependency: **3.0.4** or newer, on `PATH`. A missing or older one fails at
the preflight of `hng ngit sync` with the binary name, the floor and an install hint, not in a log
hours later. `hng doctor` checks the same floor, plus the mapping, the project and board bindings,
a scheduled pass for every mapped channel (an enabled systemd timer instance or a crontab line)
and the watcher's own state; every failing check names its fix.

The better long-term shape is a live subscription off the daemon's read path, once the daemon is
stable; `hng ngit sync` is the smallest thing that is useful today and does not block that. The
daemon now offers that subscription as a *wake* — see "Waking the pass" below. Cron stays,
because a subscription can replace the wait, not the decision.

## Waking the pass

The timer is the floor: every ten minutes the pass runs, whatever happened. The daemon can also wake
a pass as soon as something changes, so a new issue or a revised PR shows up in seconds instead
of up to ten minutes.

The wait moves; the decision does not. A relay event says something was *proposed* — a revision
is another event, an issue can be resolved or reopened later, and a merge is a kind-1631 that has
to be matched against the branch. Only the repository says what is true, and only
`ngit issue list --json` and the checkout's refs say it. So the watcher subscribes, coalesces,
and runs `hng ngit sync --channel <conversation>` as a child process: the same command the timer
runs, with the same store, cursor and idempotency keys. It never writes the board, the outbox or
the store and never moves a cursor, so there is still exactly one writer to each; the one thing
it reads is the link table, for the issue and PR ids a comment can name.

What it subscribes to:

- kinds 1621 (issue), 1618 (PR), 1619 (PR revision) and 1631 (applied), under
- a `#a` filter naming only the mapped repositories — the `concord.projects` entries that declare
  a board, resolved through their checkout's own `ngit repo --json` address — never a kind on
  its own and never the whole relay;
- and **separately**, kind 1111 comments under a `#K` filter — the root's kind, an issue or a
  PR. A comment cannot ride in the first filter: `ngit issue comment` and `ngit pr comment`
  publish NIP-22, whose event carries root `E`/`K`/`P`, parent `e`/`k`/`p` and **no `a` tag at
  all**, so a `#a` subscription can never match one. Comments are attributed to a repository
  locally, by that root id: the ids the pass has already linked (`ngit_links`) are read when
  the plan is built, and an issue or PR event that arrives while listening teaches its own id.
  A comment whose root is not known here wakes nothing and waits for the scheduled pass.

1619 is in that list on purpose: a revised PR is a new 1619 event, and a watcher that ignored it
would leave the review task's brief stale until the next scheduled pass.

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
is logged and reported in `hng status`, and the next scheduled pass is unaffected. A daemon restart
loses every pending wake and nothing else: whatever the watcher missed is exactly what the next
ten-minute pass reads.

```yaml
ngit:
  watch: true             # default: on once ngit.assignee is set; false is the scheduled
                           # pass alone
  watch_debounce_s: 30    # coalesce a channel's events this long before running the pass
```

`hng status` prints the watcher — channel repositories, relays, passes woken, wakes still counting
down — and `hng status --json` carries the same under `ngit_watch`. `hng health` notes it when a
watcher was asked for but is not subscribed.

Two operational notes:

- **The sandbox.** A pass woken by the daemon is a child of the daemon and inherits the unit's
  restrictions. With `ProtectHome=read-only` the boards directory must be writable
  (`ReadWritePaths`, as the shipped unit does): otherwise every woken pass fails to write its task
  while a timer pass — which is its own unit and has no such restriction — keeps working, which is
  a confusing half-failure to diagnose.
- **Overlap between the timer and a wake.** A timer pass and a woken pass can interleave for one
  channel. Tasks and notices carry idempotency keys, so the ordinary outcome is still one task and
  one notice; the board's check-then-insert is not atomic, though, so a duplicate is possible in
  that window. It is the same window a slow pass already has, and the alternative — a lock shared
  with the scheduler — would mean changing the pass, which this deliberately does not do.

## Inbound: issues as work

- A kind-1621 issue becomes a task with the issue's subject as its title and its body, author
  npub and nevent in the description. The idempotency key is `ngit:issue:<event id>`, so a
  re-poll cannot create a second task for the same issue.
- The issue id is a **dedupe key, not an identity**. A human may file a second task for the same
  issue; the bridge only refuses to duplicate its own.
- A NIP-22 kind-1111 comment becomes a task comment, in `created_at` order, once (tracked per
  comment id). A comment added later is appended; nothing is rewritten. The kind is 1111
  because that is what `ngit issue comment` and `ngit pr comment` publish on 3.0.5 — no ngit
  release ever wrote 1622.
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

**The card is the queue, the PR is the record.** One PR root — the kind-1618 that opened it —
maps to exactly one task for the life of the PR. A re-poll, a retry, a second proposal for the
same branch and a revision all land on that one card; none of them makes a second.

- A kind-1618 event posts one message — the PR's subject, its author npub, the branch and the
  nevent, "ready for review" — and creates one **review task** on the board, keyed
  `ngit:pr:<root event id>`. Deduped by PR root (`ngit_links`), so the same PR is never
  announced twice, and the idempotency key means a re-poll cannot make a second task either.
  A re-push that a client published as a *second* 1618 is folded back onto the root (same
  author, same source branch) and read as a revision: the oldest proposal is the root, because
  that is the event the PR opened with.
- A **revision** is an event, not an observation: kind 1619 (`E` names the PR, `c` the new
  tip), or a republished proposal carrying the same `E`/`P`/`c` tags. It reopens the PR's one
  card — `review`/`done` -> `ready` — with one comment naming the new tip and the event that
  moved it, and with the review brief rewritten for that tip (a fix round left the fix brief
  behind). A card already `ready` or `running` only gets the comment and the brief (the
  queue, or the worker reading it, now knows which tip is current); a `blocked` card is
  waiting for a person unless the bridge itself parked it (`Verdict:`/`Revision:` in the
  reason), in which case the revision reopens it; an `archived` one is left alone.
  A branch that moved without an event changes nothing: the tip the bridge knows is the tip
  an event published.
- **Reopening is not a fresh start in every respect.** `consecutive_failures` is reset — the
  attempt is new — but `model_override`/`provider_override` are untouched: a person pinned
  that card, and a revision is not a channel-config change. The profile's own model still
  decides every card that was never pinned.
- A revision is filtered before it can reopen anything. A PR the bridge never announced has
  no card to reopen; an `applied` PR is final — a late update event cannot revive it, and the
  merge commit is written on the row as soon as the merge is seen, so a notice still queued
  does not leave a window; and a revision older than the tip the row already knows is a relay
  replay, not news.
- The task is titled `Review PR <first 8 of the root event id>: <subject>` and its body names
  the PR's event id and nevent, the repository address, the author, the source branch
  (`branch-name`) and the target branch — NIP-34 carries no target tag, so it is the
  checkout's default branch, read from git. The brief: check the proposal out
  (`ngit pr checkout <nevent>`), read the diff, run the repository's gates (`ruff check`,
  `pytest`), report a verdict with reasons. **It never merges**: merging is the operator's,
  or a follow-up task.
- The notice names the task (`review task <id>`) instead of a second message announcing it.
  One PR, one channel event.
- `ngit.review_assignee` (default `default`) is the profile the task is dispatched to and
  `ngit.priority` its priority. A bot host points it at the review role's own profile, which
  carries the identity that signs the verdict (see "Bot identities and profiles"). The bridge
  writes no model on the task: the board's default,
  then that profile's own model, decides, so a review costs whatever the profile costs and
  there is no second place to keep a model in step. A task that needs another model is moved
  on its own (`hermes kanban set-model <id> <model> --provider <p>`), which is the operator's
  call, not the bridge's.
- A kind-1631 event for a PR this bridge announced posts one follow-up, `merged as <commit>`
  (NIP-34's `merge-commit` tag, or `merge-commit-id` — the spelling ngit 3.0.4 actually
  publishes, one oid for a merge commit and every commit the push added for a fast-forward —
  or the first `applied-as-commits` value when the proposal was applied as patches), and
  **completes every card the PR owns** — the bridge's review task, a re-review filed by hand
  under the brief's title, and any duplicate history left behind — with a result naming the
  merge commit and a comment saying what landed. A merge that arrives for a card already
  completed is a no-op: nothing is written twice. A 1631 that names **no commit** is not
  evidence and completes nothing: the card stays open and the pass says why.
- **`done` requires `applied`.** A PR card whose PR is still open cannot be completed by a
  worker — not the agent's `kanban_complete` tool, not `hermes kanban complete`, not
  `--force`. The bridge installs a guard on the board (`ngit_guard`): one row per PR card in
  `ngit_pr_guards` and one `BEFORE UPDATE OF status` trigger whose message is the refusal a
  person reads (`PR <short> is open at <tip> -- a merge completes a PR card, and a reviewer's
  approving verdict finishes a review card`). A merge releases the guard (the commit is the
  proof) and the completion succeeds; an approving verdict releases it too (the verdict is
  the review's proof); a close keeps it refusing and names the closing event. A card with no
  PR link — ops, docs, a fix card — is untouched.
- **A verdict finishes its card.** The reviewer records the verdict as a comment and **parks
  the card** with `kanban_block`, the first line of the reason being `Verdict: approved` or
  `Verdict: changes-requested` and the tip it covers. That park is a hand-off, not a
  question: the bridge reads the newest verdict from the PR root and applies it to the card
  the verdict covers — `approved` **completes** it with the verdict as the result (the merge
  is still a person's), and `changes-requested` puts it back on the implementer's desk as
  `ready` with the fix brief and the findings. The same pass publishes the label, so the
  board and the PR cannot disagree about which verdict they answered. A card blocked on a
  real question is never finished or re-queued by a verdict: only one the bridge parked (a
  `Verdict:`/`Revision:` reason) or a card in the `review` lane is touched.
- **The fix round ends the same way.** The fix brief tells the implementer to push the fix
  and park the card with `Revision: <tip>`. The push usually lands first, so the park is the
  news: the bridge reopens the card for review with the review brief for the tip the PR
  carries. `Revision:` is the only marker that does that, and a card blocked on a person's
  question is left where it is.
- A kind-1632 **closed** status is not a merge and never completes a card. The PR's cards are
  blocked with `block_kind=dependency`, a comment naming the closing event, and a guard that
  refuses completion — a person decides whether the work is re-filed, re-proposed or dropped.
  A queued "ready for review" line for a closed PR is dropped rather than posted.
- A card whose own premise is `after PR <short> merges` (or a `blocked on … PR <short>` line
  that names the merge) is **released** when the merge lands, with the commit named in the
  comment: the fact it waited on is now true. A blocked card that merely mentions the PR is
  left alone — a mention is not a premise, and any human condition in the released card's body
  still stands.
- A PR first seen *already* applied gets one message, the merged line — not a fake "ready for
  review" followed by a merge, and not a replay of history. There is nothing to review, so no
  task is made. A PR first seen already **closed** gets one closed line, and no task.
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

## The label on the PR

`ngit pr list` has a LABELS column, and it is where a reader looks before opening anything. The
bridge keeps it current, so "waiting on a review", "approved" and "changes asked for" are on
the PR rather than only in a task comment. Three words, one namespace, and one label per PR:

| label | when |
| --- | --- |
| `review-required` | no verdict yet, or a revision since the last one — what a new PR gets |
| `changes-requested` | the newest verdict asks for changes |
| `approved` | the newest verdict approves it; merging is still the operator's |

`in-review` is deliberately gone: an open PR with a review card already *is* in review, and a
second word for "a reviewer is looking at it" says nothing a reader cannot see on the board.
`stale` is gone the same way — a revision after a verdict means `review-required`, which says
the same thing in the one word a reader has to look at.

**The verdict is read from the PR root, not from the board.** A reviewer publishes its verdict
as a NIP-22 kind-1111 comment on the PR (`ngit pr comment ...`; root `E` = the PR) and reports
the same verdict on the task, naming the comment's event id. The newest verdict comment wins.
The card is the queue and the PR is the record: a reader of the label can fetch the comment it
was derived from, and a verdict recorded only on the card leaves the label where it was rather
than publishing a state the PR itself does not carry.

**The same read finishes the card.** The verdict a label is derived from is the review's end,
so the pass applies it to the PR's card in the same step: `approved` completes the card with
the verdict as its result (the merge stays the operator's, and the `approved` label is what
the merge route reads), and `changes-requested` returns the card to the implementer as `ready`
with the fix brief and the findings. One rule decides both — a verdict covers the tip it was
given for — so a revision that moves the tip leaves the label at `review-required` and the
card untouched until a reviewer reads the new tip. Only a card the bridge parked, or one in
the `review` lane, is acted on: a card blocked on a person's question is never finished or
re-queued by a verdict, however green it is.

**One writer.** The label pass — the sync that walks `ngit_links` — is the only thing that
writes a label. It reads each open PR's verdict comments and the label events the PR currently
carries off the same relay set it publishes to, decides the one label that belongs there,
publishes it and retracts every other label event the PR carries — including one an earlier
writer left (`in-review`, `stale`, a second word from a version with more of them) and one this
bridge's link row never recorded. That is what makes a leftover a one-pass correction rather
than a manual cleanup. The link row only remembers what this pass published, so a poll with no
news publishes nothing. A merge leaves the last verdict in place: it is the verdict the merge
was decided on.

**A verdict that cannot be read does not fall back.** If the newest comment states a verdict in
words the bridge does not recognise, the label is left untouched and the pass records the event
id and the relay: publishing an older approval over a verdict nobody parsed is the wrong
direction. The same holds when the relay read itself fails, or a PR the pass already labelled
has no verdict it can read any more — the row keeps what readers see and the next poll tries
again.

**Relays can disagree.** The read is per relay, so two relays holding different newest verdicts
(one of them missed an event) is visible: the newest `created_at` wins, the pass records which
relay's verdict it used, and a note names the relays that saw something else.

Two things about the format were measured against ngit 3.0.5 and decide how it is published:

- The namespace is `#t` (NIP-32), because that is the label a `ngit pr list` reader shows; a
  namespace of the bridge's own does not appear in the column at all. One kind-1985 event
  carries `L=#t`, `l=<label>,#t`, the repository's `a` and the PR's `e`.
- A later label does **not** supersede an earlier one: ngit merges `#t` labels, so publishing
  `approved` over `review-required` shows both. The bridge therefore retracts the label it is
  replacing with a NIP-09 kind-5, sent to every relay it published the label to. Without that
  the column accumulates a history and a superseded `approved` still reads as a green light.

A PR with no verdict and no review card carries **no label at all** (reviews turned off, or a
card that could not be made): nothing says a review is coming, and a label would be the bridge
inventing one. Any label left on such a PR is retracted.

A reader with a cached checkout may still show the previous label until ngit refreshes its
`.git/nostr-cache.lmdb`: the events on the relays are the state, and the cache is a copy of
them. `ngit.labels: false` turns the whole thing off — a deployment that does not want the
extra events on its repository's relays publishes none.

Retrofitting is deliberately not done: the first poll after upgrading labels the PRs it
carries, and an old PR still open is labelled when a revision puts it back in the window.
Publishing a label onto every PR the bridge ever announced would be a burst of events nobody
asked for, and a merge is left as it is — its link is closed, and the label a merged PR
carries is whatever it was decided on.

## Bot identities and profiles

A bot role is a job with its own voice: it gets its own key and, when it runs agent work, its
own Hermes profile. The default profile stays the box's general-purpose agent and is never a
bot's signature.

- **deps** — dependency proposals. The role's identity signs the `pr/deps-*` branches and the
  CI triggers; the scheduled unit that scans, builds and pushes holds it, so the role needs no
  profile of its own. The pipeline is `hng deps run`, configured from the gateway's `deps:`
  block and kept as its own oneshot (see `docs/deps.md`) — never a pass in the daemon's poll
  loop.
- **review** — PR verdicts. It has both: an identity to sign with and a profile the bridge
  dispatches its tasks to (`ngit.review_assignee: review`). The profile is what makes a review
  cheap or careful — its model is set once, and there is no per-task or per-config model to
  keep in step with it.

Rules that keep the pairing honest:

- **The key belongs to the role, not the host.** Keep it outside every checkout at mode `0600`
  and hand it to a command by path (`--nsec-file <path>`), never as an argument value, never
  into a log, never into a tracked file. A role that borrows the default profile's key is not
  a role: its work becomes unattributable.
- **The verdict goes on the PR.** A review publishes its verdict as a NIP-34 comment signed as
  its own identity, then reports the same verdict on the task, naming the event:

  ```bash
  ngit pr comment <id|nevent> --body "<verdict>" --nsec-file <path to the review key>
  ```

  The command reports a signed event, not a relay's acceptance: read the thread back **from a
  relay** before calling the verdict published. Read it by the kind the command actually
  published: `ngit pr comment` publishes a NIP-22 `kind: 1111` whose uppercase root tags
  (`E`/`K`/`P`) name the PR, and whose lowercase parent tags (`e`/`k`/`p`) name it too for a
  top-level comment — `--reply-to` points the parent tags at the answered comment, so it is the
  root `E` that always names the PR. A `kind: 1622` filter returns nothing and reads exactly
  like a comment no relay kept: no ngit release ever wrote 1622 (comment publishing arrived in
  2.3.0 and was NIP-22 from the start), and the 1622s in this repository's history were
  hand-published by the bots. Query the author and the kind, then match the root tag to the PR
  (`nak req -k 1111 -a <bot npub> <repository relay>`); a comment that only exists in the local
  view is not on the PR, and a relay that does not know the identity yet is the usual reason.

- **The profile must exist before the config names it.** `review_assignee: review` on a host
  with no `review` profile files a task that nothing will pick up. Create the profile first,
  then point the bridge at it.
- **A new identity is an operator step at the relay.** A GRASP may gate writes by author
  (`NGIT_REPOSITORY_WHITELIST`), and whether a comment needs the identity listed depends on the
  deployment — check by publishing and reading the answer back, never by trusting the publish
  command's success line. Adding the bot's npub to that list is a deployment change: the bridge
  does not make it, and a bot must not add itself. Until the operator does, say which relay
  refused and why, and report the verdict on the task with the refusal quoted.

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
  A PR has **one row per root event id**, and its task id stays with that root for the life
  of the PR — a revision updates the row rather than adding one. The `note` keeps the PR's
  nevent, subject, author, branches, the tip an event last named (`tip`, `tip_at`) and the
  merge commit once a status event names one, so a later poll can act on news whose event it
  no longer carries.
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
- Merging. A review task ends with a verdict; the merge is the operator's (`ngit pr merge`), and
  the bridge only watches for the status event it produces.
- Retrofitting review tasks for PRs announced before this bridge made them. A PR already
  `signalled` with no task stays that way: the channel was told what happened at the time, and
  a change of behaviour is not a reason to rewrite it.
- PR revisions are handled as events only (kind 1619, or a republished proposal carrying its
  tags): a branch that moved without publishing one changes nothing. Drafts (1633) are not
  read, and the channel gets one signal per PR and one per merge — a revision reopens the
  card, it does not post.
- Any per-issue visibility rule. A private repository keeps everything derived from it private;
  the bridge carries no second set of rules.
