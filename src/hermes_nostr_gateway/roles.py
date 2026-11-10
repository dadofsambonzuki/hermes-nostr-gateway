"""The roles this gateway speaks as, and which one a request is addressed to.

A role is an identity plus what a request addressed to it means. The gateway's own key is
one; the ngit review bot and the Deps Bot are the others, derived from the keys already in
config so a new bot needs no second registry -- naming its key is the whole enrolment.

Two things every plane shares:

* `addressed` resolves a comment, message or note to the role it names -- a NIP-27 `p` tag,
  or the `nostr:npub...` reference a client leaves behind in the words when it drops the
  tag;
* the authorisation gate is the control pane's (`control.trusted_npubs`), not the plane's:
  a stranger can already file an issue, and naming a bot must not be a second way to spend
  a worker turn.

What differs between planes is only the *answer*. A repository comment becomes a reply
task, and the review role's request on a PR re-queues that PR's review instead; a channel
message is answered in the channel by the role's profile. Keeping the resolution and the
gate here is what stops each new plane from growing its own, slightly different, rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .control import as_hex

#: The gateway's own identity: the default profile, and every question not addressed to a bot.
GATEWAY = "gateway"
#: The ngit bridge's review role: a PR request re-queues the review, it does not reply.
REVIEW = "review"
#: The dependency pipeline's signer. It has no profile: its answer is a run, not a message.
DEPS = "deps"

#: What a request addressed to the review role does on a PR.
REVIEW_ACTION = "review"


@dataclass(frozen=True)
class Role:
    """One identity, and what addressing it means.

    `key` is what a comment actually carries -- a NIP-27 `p` tag is hex -- and `npub` is the
    same key in the form a person recognizes, which is also the form a client that dropped
    the tag leaves in the body. `profile` is the Hermes profile the answer is dispatched to,
    empty when the role's answer is not a message (the Deps Bot's is a run). `action` is the
    non-message answer a plane can carry out, and `secret_file` is the role's own signing
    key when it has one where the gateway can read it.
    """

    name: str
    key: str
    npub: str = ""
    profile: str = ""
    action: str = ""
    secret_file: str = ""
    #: One line a reply task carries when the role cannot answer for itself: what a person
    #: or the default worker would run instead of writing a message.
    how: str = ""


def roles_from_config(cfg: Mapping[str, Any], *, self_npub: str,
                      reviewers: Sequence[str] = ()) -> tuple[Role, ...]:
    """Every role this installation defines, gateway first.

    Nothing new is configured: the review role comes from `ngit.review_secret_file` (through
    the same `reviewers` derivation the verdict gate uses, so there is one answer to "whose
    word counts"), and the Deps Bot from `deps.signer`. A key that does not parse is skipped
    rather than guessed at -- a role nobody can address is a config problem, not a crash.
    """
    roles: list[Role] = []
    gateway = as_hex(self_npub)
    if gateway:
        roles.append(Role(name=GATEWAY, key=gateway, npub=_npub(gateway)))
    ngit = _block(cfg, "ngit")
    profile = str(ngit.get("review_assignee") or "").strip()
    review_secret = str(ngit.get("review_secret_file") or "").strip()
    for who in reviewers:
        key = as_hex(str(who or ""))
        if not key or any(role.key == key for role in roles):
            continue
        roles.append(Role(name=REVIEW, key=key, npub=_npub(key), profile=profile,
                          action=REVIEW_ACTION, secret_file=review_secret))
    deps = _block(cfg, "deps")
    signer = as_hex(str(deps.get("signer") or ""))
    if signer and not any(role.key == signer for role in roles):
        roles.append(Role(
            name=DEPS, key=signer, npub=_npub(signer),
            how="Run `hng deps run --repo <repository>` from the gateway checkout (never "
                "from a service account by hand) and comment back with what it proposed."))
    return tuple(roles)


def addressed(event: Mapping[str, Any], roles: Sequence[Role]) -> Optional[Role]:
    """The role a comment names, or None when it names none of ours.

    A `p` tag is the NIP-27 spelling and what gitworkshop writes for an `@npub`. The text
    fallback exists because a client that dropped the tag still leaves the `nostr:npub...`
    reference in the body, and answering a person matters more than insisting on their
    client's correctness. Role order decides a comment that names two of ours: the gateway
    is first, because the general profile answering is the safe default.
    """
    keys = {str(tag[1]).lower() for tag in event.get("tags") or ()
            if isinstance(tag, (list, tuple)) and len(tag) >= 2 and str(tag[0]) == "p"}
    body = str(event.get("content") or "").lower()
    for role in roles:
        if role.key and role.key in keys:
            return role
        if role.npub and role.npub in body:
            return role
        if role.key and role.key in body:
            return role
    return None


def named(event: Mapping[str, Any], roles: Sequence[Role]) -> tuple[Role, ...]:
    """Every role a comment names, in role order. For a caller that has to report, not act."""
    found: list[Role] = []
    keys = {str(tag[1]).lower() for tag in event.get("tags") or ()
            if isinstance(tag, (list, tuple)) and len(tag) >= 2 and str(tag[0]) == "p"}
    body = str(event.get("content") or "").lower()
    for role in roles:
        if ((role.key and role.key in keys)
                or (role.npub and role.npub in body)
                or (role.key and role.key in body)):
            found.append(role)
    return tuple(found)


def _npub(key: str) -> str:
    """A key in the form a person and a `nostr:` reference use."""
    try:
        import nostr_sdk as sdk

        return sdk.PublicKey.parse(key).to_bech32()
    except Exception:  # noqa: BLE001 - display only; the hex key is what matches
        return ""


def _block(cfg: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """One config block, or an empty mapping: a missing block is not a broken one."""
    value = cfg.get(name)
    return value if isinstance(value, Mapping) else {}


def describe(roles: Iterable[Role]) -> list[str]:
    """One line per role, for an operator reading `hng status` or a report."""
    out = []
    for role in roles:
        what = role.action or ("reply" if role.profile else (role.how and "run" or "reply"))
        out.append(f"{role.name:8} {role.npub or role.key}  {what}"
                   + (f" -> {role.profile}" if role.profile else ""))
    return out
