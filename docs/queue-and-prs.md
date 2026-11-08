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
| `done` | landed | a merge, for a card that reviews a PR; the worker, for a card that has none |
| `archived` | outside the board — not a column, not a status to work from | archive |

A dependency is not a block. The kernel routes `block_kind=dependency` to `todo`, gated by the
parent card, and returns it to `ready` by itself when the parent is done. `blocked` means a
person, which is the only meaning the word is allowed here — with one exception: a PR closed
without a merge blocks its card (kind `dependency`, the closing event named in the comment),
because dropping or re-filing the work is a person's decision too.

## The four rules

### One card per PR root

The kind-1618 event that opened the PR is its key (`ngit:pr:<root>`), and one PR root has one
card for the life of the PR. A re-poll, a retry after a failed send, a re-push a client
published as a second proposal, and a revision all land on that card.

- **A second proposal for the same branch is a revision.** The oldest proposal is the root
  (same author, same source branch); a later 1618 for that branch, or one that names the root
  explicitly, is folded back onto it, and so is a 1619.
- **A revision reopens the card; it never creates one.** `review`/`done` -> `ready`, with one
  comment naming the new tip and the event that moved it. A card already `ready` or `running`
  only gets the comment; a `blocked` card is waiting for a person and is not released by a
  revision; an `applied` PR is final, so a late revision cannot revive it.

**Enforced in** `_fold_repeats` and `_revisions` in `src/hermes_nostr_gateway/ngit.py`, the
`ngit:pr:<root>` idempotency key on the card, and `Board.reopen` in
`src/hermes_nostr_gateway/kanban.py`. `tests/test_ngit.py` pins one card across two passes, a
republished proposal and a revision of a `done` card.

**In this tree:** yes — all three are on `master`.

**Check:** `hng ngit sync --channel <id> --dry-run --json` counts a new tip under
`prs.revisions` and the reopen it would cause under `prs.would_reopen`; `prs.reopened` moves
only on a real pass. And
`sqlite3 <board>/kanban.db "select idempotency_key from tasks where idempotency_key like 'ngit:pr:%'"`
has one row per PR root.

### `done` requires applied

A card that reviews a PR is finished by the merge — not by the worker, and not by the
reviewer's approval. A worker's word is not a merge. While the PR is open the board refuses
`done` and names what it saw (`PR <short> is open at <tip>`); a PR closed without a merge keeps
refusing and names the closing event.

The agent's `kanban_complete` tool and `hermes kanban complete` both write
`tasks.status = 'done'`, so one guard covers both without either knowing the bridge exists. A
merge releases the guard, and the bridge completes the card with the merge commit in its
result.

**Enforced in** `src/hermes_nostr_gateway/ngit_guard.py` — one row per guarded card in
`ngit_pr_guards`, and one `BEFORE UPDATE OF status` trigger on the board's `tasks` table whose
refusal is the sentence a person reads. `tests/test_ngit.py` drives both completion paths
(`test_the_cli_refuses_an_open_pr_card`,
`test_an_open_pr_card_refuses_completion_at_the_board`).

**In this tree:** no — `ngit_guard.py` and its tests land with PR `14bd6ea3`, so until it merges
`done` on an open PR card is convention.

**Check:** `sqlite3 <board>/kanban.db "select name from sqlite_master where type='trigger'"`
lists `ngit_pr_done_*` for the open PRs' cards, and
`test -f src/hermes_nostr_gateway/ngit_guard.py` answers true.

### Three labels, one writer

A PR carries one of `review-required`, `changes-requested`, `approved`. There is no
`in-review` — an open PR with a review card already is in review — and no `stale`: a tip newer
than the verdict is `review-required`, which is the same fact in the one word a reader has to
look at. The label is derived from the newest kind-1111 verdict on the PR root and the tip it
covers; the pass publishes the one that belongs there and retracts every other live label,
including one an earlier writer left. That correction is what makes it the only writer: no
cleanup follows by hand.

**Enforced in** `src/hermes_nostr_gateway/ngit_labels.py`, the only module that signs a
kind-1985 label, called from the pass in `ngit.py`; on by default, and `ngit.labels: false`
turns it off. `tests/test_ngit_labels.py` pins "a verdict replaces the previous one, never adds
to it" and "every live label outside the desired set is unmanaged".

**In this tree:** no — the three-word vocabulary lands with PR `870a47fc`; the `ngit_labels.py`
here still carries `in-review`.

**Check:** with `870a47fc` applied, `git grep -n '"in-review"' -- src/` finds nothing — the
prose still mentions the old word, the code does not — and `ngit pr list` shows one label per PR
once the pass has run.

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

**In this tree:** no — the hook is absent, and `merge_pr.sh` here has no card-workspace refusal;
both halves land with PR `b81a2d50`.

**Check:** `scripts/worker_push_guard.sh status` reports whether the hook is installed and
whether this shell is a card workspace; until `b81a2d50` merges the script is absent and the
check cannot run.

## Transitions

```text
triage -> todo|ready          a person, or the specifier profile
todo -> ready                 every parent card is done (the kernel's gate)
ready -> running              the dispatcher claims it
running -> review             the worker's last act on the card: kanban_request_review
review -> ready -> running    a new revision, or a changes-requested verdict
review -> blocked             the PR was closed without a merge; a person decides
review -> done                a merge, and only a merge
done -> ready                 a revision on a PR that never merged (the repair path)
any -> archived               out of the board
```

`review -> running` has two triggers, and neither is silent. A **new revision** reopens the
card by itself, because the tip the verdict covered is no longer the tip on the PR. A
**changes-requested verdict** is a person's: `kanban_request_changes` sends a card in the
review lane back to its implementer, and for the bridge's review card a person re-queues the
work after reading the verdict.

`review -> done` for a card with **no** PR link is the ordinary `kanban_complete`; the guard
binds only cards that review a PR.

## Which of this is enforced, and which is convention

The **In this tree** lines above are written against `master` at `e1d843d`, where only the first
rule is enforced end to end. The `done` guard and the three-word labeler are code in review —
PRs `14bd6ea3` and `870a47fc` — and the merge route exists without a card-workspace refusal
while the pre-push hook is absent: both halves land with PR `b81a2d50`. The model does not
change when they land — the rule, the writer and the transition are as written above, and only
the check starts answering. That is why the checks are the authority and this paragraph is not:
run them against the tree you are about to trust.

Still convention, with nothing enforcing it:

- **A worker ends at `review`.** For a linked PR card the guard refuses `done` once `14bd6ea3`
  lands; for a work card nothing does — nothing stops a worker completing its own card while the
  PR it published is open.
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
- The card is not `done` while the PR is open — with `14bd6ea3` applied, the guard's refusal is
  the proof.
- A revision after the verdict reopens the same card; it does not create a second one.
- Nothing was pushed to `master` from a card workspace; the merge was the operator's.
