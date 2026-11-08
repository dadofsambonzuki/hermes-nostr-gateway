# The queue and the record

**The card is the queue; the PR is the record.**

A card answers one question: *what should happen next with this change*. A PR answers a
different one: *what has happened to it*. The card is allowed to be late, and it is never the
evidence — the diff, the revisions, the verdict and the merge commit are all on the PR.

The model is written down because one fact was once held three ways at once — a card status, a
PR label and the PR's own status event — each with its own writer. A card read `done` on a PR
that was still open, a revised PR grew a second card, and a PR carried two labels that
contradicted each other. One fact, one writer; everything else derived.

## One owner per fact

| fact | lives in | written by |
|---|---|---|
| what should happen next | the card's `status` | the kernel, the worker, the reviewer, the bridge |
| what has happened to the change | the PR's status events: `open`, `applied`, `closed` | the repository's relays |
| what a reader of the PR sees | one label | the bridge's label pass, derived |

Nothing is copied upwards. A label is computed from the newest verdict comment on the PR root
and the tip that verdict covers — never from the card, because a reader who never opens the
board has to be able to check it. Nothing reads a label back either: a card status is never set
from a label, and a label is never set from a card status. And a card is not a record of the
change: archiving it does not change a PR, and a merged PR does not become provisional because
a card moved.

## What each status means

| status | means | how a card gets there |
|---|---|---|
| `triage` | filed, not yet a specification | a person, or `kanban_create(triage=true)` |
| `todo` | not claimable: an open parent card is gating it | the kernel, when a card is created under a parent |
| `scheduled` | waiting on a time or an external event, not a person | `kanban_schedule` |
| `ready` | in the queue; the next free worker may claim it | the kernel: created without an open parent, unblocked, reopened, or a parent finished |
| `running` | a worker holds the claim | the dispatcher |
| `blocked` | a person has to act; `block_kind` says what for | `kanban_block` with `needs_input`, `capability` or `transient` |
| `review` | the worker's work is done; a reviewer decides | `kanban_request_review` |
| `done` | landed, or reviewed and approved; the merge is still a person's | a merge, or a reviewer's approving verdict, for a card that reviews a PR; the worker, for a card that has none |
| `archived` | outside the board — not a column, not a status to work from | archive |

A dependency is not a block. The kernel routes `block_kind=dependency` to `todo`, gated by the
parent card, and returns it to `ready` by itself when the parent is done. `blocked` means a
person, which is the only meaning the word is allowed here — with two exceptions that are
events, not questions: a PR closed without a merge blocks its card (kind `dependency`, the
closing event named in the comment), because dropping or re-filing the work is a person's
decision too; and a review's hand-off parks the card (`Verdict:`/`Revision:` in the reason)
only until the bridge acts on the verdict or the fix, which it does on its next pass.

## The six rules

### One card per PR root

The kind-1618 event that opened the PR is its key (`ngit:pr:<root>`), and one PR root has one
card for the life of the PR. A re-poll, a retry after a failed send, a re-push a client
published as a second proposal, and a revision all land on that card.

- **A second proposal for the same branch is a revision.** The oldest proposal is the root
  (same author, same source branch); a later 1618 for that branch, or one that names the root
  explicitly, is folded back onto it, and so is a 1619.
- **A revision reopens the card; it never creates one.** `review`/`done` -> `ready`, with one
  comment naming the new tip and the event that moved it, and the review brief rewritten for
  that tip. A card already `ready` or `running` only gets the comment; a `blocked` card is
  waiting for a person unless the bridge itself parked it (`Verdict:`/`Revision:` in the
  reason), in which case the revision reopens it; an `applied` PR is final, so a late
  revision cannot revive it.

**Enforced in** `_fold_repeats` and `_revisions` in `src/hermes_nostr_gateway/ngit.py`, the
`ngit:pr:<root>` idempotency key on the card, and `Board.reopen` in
`src/hermes_nostr_gateway/kanban.py`. `tests/test_ngit.py` pins one card across two passes, a
republished proposal and a revision of a `done` card — and, since the queue grew a duplicate
review card for a re-push, the two real kind-1618 roots it published (same `branch-name`, two
tips, the second one based on the first).

**The branch it compares is the one on the wire.** `branch-name` is the author's branch as
typed, cut by ngit to 60 characters. `ngit pr view --json` prints
`pr/<branch>-(<8 hex>)`, adding the short PR id so two roots of one branch stay apart on
screen; that suffix is the client's display string and never reaches the bridge, so it is not
stripped. Two roots of one re-push carry the **same** `branch-name` and fold; a `(<pr-id>)`
mismatch is not a failure mode, and a normalisation for it would guard a string this code
never sees.

**So a second card for one branch means the pass that made it had no fold.** Check the
checkout before the comparison: `hng status` prints `checkout: <branch> @ <sha> (<n> behind
master)`, and a bridge serving a stale tree makes one card per root while every gate stays
green (`hng doctor` passes a checkout that is behind by design). Pull, restart the daemon,
then read the board again — the code is right, the deployment was not.

**In this tree:** yes — all three are on `master`.

**Check:** `hng ngit sync --channel <id> --dry-run --json` counts a new tip under
`prs.revisions` and the reopen it would cause under `prs.would_reopen`; `prs.reopened` moves
only on a real pass. And
`sqlite3 <board>/kanban.db "select idempotency_key from tasks where idempotency_key like 'ngit:pr:%'"`
has one row per PR root.

### `done` requires applied

A card that reviews a PR is finished by the merge — or by the reviewer's approving verdict, which
is the review's end and not the work's landing. A worker's word is neither. While the PR is open
the board refuses `done` and names what it saw *and what it compared* (`PR <short> is open at
<tip> -- no applied status names it, and no commit on master carries its patch (…)`); a PR closed
without a merge keeps refusing and names the closing event.

**`applied` is decided from the record, then from the checkout** (`applied.py`). `ngit pr apply`
replays a proposal's patches as new commits, so the tip the PR event names is never an ancestor
of `master` however cleanly the change landed — and a kind-1631 exists only when a merge route
published one. So the pass asks the checkout whether every patch the PR adds is already on the
base branch (`git patch-id`), or the PR as one patch (a squash). A PR found that way is applied:
its cards are completed with `applied as <commit> (its patch is already on master …)` as the
result, and the comment says no status event claims it. A close with nothing on the branch is
still a person's decision.

The agent's `kanban_complete` tool and `hermes kanban complete` both write
`tasks.status = 'done'`, so one guard covers both without either knowing the bridge exists. A
merge releases the guard, and the bridge completes the card with the merge commit in its
result.

**Enforced in** `src/hermes_nostr_gateway/ngit_guard.py` — one row per guarded card in
`ngit_pr_guards`, carrying the evidence the pass used, and one `BEFORE UPDATE OF status` trigger
on the board's `tasks` table whose refusal is the sentence a person reads. The evidence check is
`src/hermes_nostr_gateway/applied.py`; `tests/test_ngit.py` drives both completion paths
(`test_the_cli_refuses_an_open_pr_card`,
`test_an_open_pr_card_refuses_completion_at_the_board`,
`test_a_pr_applied_as_patches_completes_its_card_without_a_status_event`) and
`tests/test_applied.py` drives the checkout's answers.

**In this tree:** yes — `ngit_guard.py` and its trigger are on `master` (PR `14bd6ea3`), and the
checkout half is on `master` with this change.

**Check:** `sqlite3 <board>/kanban.db "select name from sqlite_master where type='trigger'"`
lists `ngit_pr_done_*` for the open PRs' cards,
`sqlite3 <board>/kanban.db "select pr_id, state, evidence from ngit_pr_guards"` shows what each
refusal is based on, and `test -f src/hermes_nostr_gateway/ngit_guard.py` answers true.

#### The guard is monotonic

`applied`, `closed` and `approved` are final: the pass never walks any of them back to `open`.
Only the absence of a *newer* event could suggest a PR reopened, and the PR feed is incremental
— each pass fetches from a stored cursor, so silence about a PR means "nothing arrived in this
window", not "this PR is open". A status event that reaches the relays after the pass whose
window covered its timestamp sits behind the cursor for good, and re-asserting `open` on that
silence rewrites a truthful row into a refusal no later pass can undo. The guard reads each
card's existing state before refreshing it and leaves a terminal one alone
(`TERMINAL_GUARD_STATES` in `ngit.py`, used by `Sync._guards`); the **link** is another half,
because a link whose state is `merged` or that carries `note["merge"]` is skipped outright,
and so is a card already `done` or `archived` — a finished card has nothing left to guard.

`approved` is in the set because an approving verdict releases the guard: the card is finished
by the verdict, the merge stays a person's. A no-news pass that rewrote that row to `open`
would re-make the trigger and refuse the completion the verdict earned — the one thing the
review loop exists to prevent.

This does not hide a missed event: a card whose close was never read stays open and still
refuses completion. Curing that needs a lookback wider than the cursor — see
`docs/ngit-bridge.md`, "A missed status event does not heal".

**Enforced in** `Sync._guards` (`src/hermes_nostr_gateway/ngit.py`).
**Tested by** `test_a_pass_with_no_news_does_not_reopen_a_released_guard`,
`test_a_pass_with_no_news_does_not_reopen_a_closed_guard`,
`test_a_pass_with_no_news_does_not_reopen_an_approved_guard`, and the control
`test_a_pass_with_no_news_still_holds_an_open_guard` in `tests/test_ngit.py`.

### A merge requires the card finished

The other direction of the same rule. `done` requires applied, and an applied PR requires a card
that is not still being worked on: the merge route reads the PR's card and refuses when its
status says the work is unfinished. `review` is a reviewer's turn and `done` is finished, so both
mean nobody is still working on it; `running`, `ready`, `todo`, `scheduled`, `triage` and
`archived` do not. The refusal names the card and the status the board actually holds, because a
merge that says only "refused" is the generic error this rule exists to replace.

`blocked` means two things and the two are told apart by which card it is. The PR's **own** card
may be blocked: the review brief's last instruction is to park a reviewed PR's card with
`kanban_block` (a reviewer cannot complete it, because `done` requires applied), and the person
such a card waits for is the person who merges — refusing it would refuse every PR that went
through the review lane. A **work** card named by a trailer may not be blocked: there a block
says the work is not finished, and a merge does not overrule it.

Which card is read is the bridge's answer, not the merge's: the `ngit_links` row for the PR root
names the PR's card, and every `Card:` trailer in the commits names the card the work was done
under, so both are checked. When the bridge has not linked the PR yet, the board comes from the
checkout's `concord.projects` entry. `Card: operator` says the work is nobody's card; it does not
release a card the bridge linked, so operator work cannot land a worker's PR early.

**Enforced in** `src/hermes_nostr_gateway/ngit_merge.py` (the read) and `scripts/merge_pr.sh`
exit 7 (the route). `tests/test_merge_route.py` runs the real script against a fake `ngit` for a
running card, a blocked card, a card the PR's commits name but the link does not, and the
`Card: operator` case.

**In this tree:** yes — `ngit_merge.py`, the exit-7 check and its tests are this change.

**Check:** `hng ngit merge-check <pr> --repo <checkout>` exits 7 and prints the card, or 0 and
prints the statuses it read.

### Every commit carries its card

A card's commits say which card they belong to, so `git log` answers "which card wrote this
line" without the board — and because an ngit PR's description is built from its commits'
messages, the PR says it too. The trailer is `Card: t_xxxxxxxx`, written by a `commit-msg` hook
installed once per clone (`scripts/card_trailer.sh install`); the operator's checkout writes
`Card: operator`, which is the explicit statement that the work is nobody's card rather than a
silent absence. A message that already carries a trailer is left alone, so an operator working
for a card writes the id themselves.

The merge route refuses a PR any of whose commits carries no trailer, or one that names something
that is not a card (exit 6), and names them. It never writes one: the attribution is the worker's
fact, and a merge-time guess would attribute lines to a card nobody named, while rewriting the
commits would change the history the review covered.

**Enforced in** `scripts/card_trailer.sh` (the hook) and `scripts/merge_pr.sh` exit 6 (the
route). `tests/test_merge_route.py` commits in a card worktree for real and checks the trailer,
then refuses a PR that carries none.

**In this tree:** yes — the hook and the exit-6 check are this change.

**Check:** `scripts/card_trailer.sh status` says whether the hook is installed and what this
shell would write; `git log --format='%(trailers:key=Card,valueonly)'` on a merged branch names
the card.

### Three labels, one writer

A PR carries one of `review-required`, `changes-requested`, `approved`. There is no
`in-review` — an open PR with a review card already is in review — and no `stale`: a tip newer
than the verdict is `review-required`, which is the same fact in the one word a reader has to
look at. The label is derived from the newest kind-1111 verdict on the PR root and the tip it
covers; the pass publishes the one that belongs there and retracts every other live label,
including one an earlier writer left and a second event carrying the same word (a duplicate
publish; selecting leftovers by value alone cannot see it). That correction is what makes it
the only writer: no cleanup follows by hand.

**Enforced in** `src/hermes_nostr_gateway/ngit_labels.py`, the only module that signs a
kind-1985 label, called from the pass in `ngit.py`; on by default, and `ngit.labels: false`
turns it off. `tests/test_ngit_labels.py` pins "a verdict replaces the previous one, never adds
to it" and "two live events with the desired value leave one"; the pass's tests pin that the
row names the survivor and that a row whose event is gone republishes the label.

**In this tree:** yes — the three-word vocabulary is on `master` (PR `870a47fc`).

**Check:** `git grep -n '"in-review"' -- src/` finds nothing — the prose still mentions the old
word, the code does not — and `ngit pr list` shows one label per PR once the pass has run.

### A worker cannot merge

A card workspace may push its own branch; `master` is the operator's. A pre-push hook refuses
a push to `refs/heads/master` or `refs/heads/main` from a dispatcher-marked shell or a checkout
under `.worktrees/`, and the documented merge route refuses the same contexts before it fetches
anything.

This is not a security boundary and this document will not pretend it is: a worker can delete
the hook or pass `--no-verify`. What the hook buys is that an accidental merge becomes a
deliberate one. The durable half is identity — a merge signed with a key no worker profile
holds — and that is host configuration, not this repository.

**Enforced in** `scripts/worker_push_guard.sh` (the `pre-push` hook; installed once per clone
into the hooks directory every worktree shares) and `scripts/merge_pr.sh` (exit 4 in a card
workspace).

**In this tree:** yes — the hook and `merge_pr.sh`'s exit-4 refusal are on `master` (PR
`b81a2d50`).

**Check:** `scripts/worker_push_guard.sh status` reports whether the hook is installed and
whether this shell is a card workspace.

## Transitions

```text
triage -> todo|ready          a person, or the specifier profile
todo -> ready                 every parent card is done (the kernel's gate)
ready -> running              the dispatcher claims it
running -> review             the worker's last act on the card: kanban_request_review
review -> ready -> running    a new revision, or a changes-requested verdict
review -> blocked             the PR was closed without a merge; a person decides
review -> done                a merge, or a reviewer's approving verdict
done -> ready                 a revision on a PR that never merged (the repair path)
blocked -> ready              a fix that was pushed, a revision on a verdict-parked card,
                              or a changes-requested verdict
blocked -> done               the approving verdict on a review card the reviewer parked
any -> archived               out of the board
```

`review -> running` has two triggers, and neither is silent. A **new revision** reopens the
card by itself, because the tip the verdict covered is no longer the tip on the PR. A
**verdict** ends the review: the bridge reads it from the PR root and applies it — `approved`
completes the card with the verdict as its result (the merge stays the operator's), and
`changes-requested` puts it back on the implementer's desk as `ready` with the findings, which
is the transition `kanban_request_changes` writes for a card the kernel handed to a reviewer.

A **blocked** card is released by an event only when the bridge parked it: `Verdict:` or
`Revision:` as the first word of the reason says the next event is what it waits for, and a
person's question matches neither.

**Behind the cursor, the repair is a command.** A verdict (or revision) that arrived while the
bridge was down, or before the rule above existed, is never read again by a pass — the poll only
carries what is newer than its cursor. `hng ngit reconcile --channel <id>` reads those PR roots
again for the cards parked on a `Verdict:` reason and applies the same transition; `--dry-run`
prints the cards and their destinations first, and a second run moves nothing.

`review -> done` for a card with **no** PR link is the ordinary `kanban_complete`; the guard
binds only cards that review a PR.

## Which of this is enforced, and which is convention

All six rules are now code on `master` or in this change: one card per PR root, `done` requires
applied, a merge requires the card finished, every commit carries its card, three labels written
by one pass, and a worker cannot merge. The **In this tree** lines above name each one's check,
and they are the part of this document that is allowed to be wrong when the code moves: run them
against the tree you are about to trust.

What the rules still cannot do:

- **A hook is not a boundary.** `--no-verify`, deleting `.git/hooks/*`, or committing from a
  hand-made worktree with the markers cleared all skip the hooks that write the trailer and
  refuse a push to `master`. Each of those turns an accident into a deliberate act; the durable
  answer is identity (a merge signed by a key no worker profile holds), which is host
  configuration.
- **The merge route is a script in this checkout.** It refuses in a card workspace and checks
  the card, but a shell that edits the script can do what it likes. Its job is to make the
  documented route the safe one, not to be the only route.

Still convention, with nothing enforcing it:

- **A worker ends at `review`.** For a linked PR card the guard refuses `done`; for a work card
  nothing does — nothing stops a worker completing its own card while the PR it published is
  open.
- **A work card is not the PR's card.** The bridge's review card is the one the board holds to
  the merge; nothing adopts the card the implementation ran on. Its `done` says the work is
  finished, not that the change landed.
- **A verdict is published, not just recorded.** The label pass reads kind-1111 on the PR
  root; a verdict written only into the card's comments publishes nothing. The review brief
  asks for both.
- **Branches land as `pr/<slug>`.** Naming, and the operator's habit; nothing checks it.

## What a reviewer can hold

- The card is in `review`, and the verdict is a comment naming the PR and the tip it covers.
- The PR carries exactly one label, and it is the verdict's, when `ngit.labels` is on.
- The card is not `done` while the PR is open — the guard's refusal is the proof.
- The PR cannot be merged while its card is `running`: `hng ngit merge-check <pr>
  --repo <checkout>` exits 7 and names it.
- Every commit the PR adds carries a `Card:` trailer, so the merge route will not refuse it
  for saying nothing.
- A revision after the verdict reopens the same card; it does not create a second one.
- Nothing was pushed to `master` from a card workspace; the merge was the operator's.
