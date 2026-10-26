"""Bot peers and the rules that stop two agents answering each other for ever.

The regression test at the bottom is the incident itself: one agent and one bot stored
3,370 rows of the same sentence before anyone noticed. Every other test here is a piece
of why that can no longer happen — and, just as important, evidence that a human peer
is untouched by any of it.
"""

from __future__ import annotations

import asyncio
import json
import time

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.bots import BotRegistry, bot_flag, profile_name
from hermes_nostr_gateway.crypto import build_rumor, unsigned_event
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.policy import Policy, normalise_text
from hermes_nostr_gateway.store import Inbound, Store

ME = "f" * 64
BOT = "a" * 64
HUMAN = "b" * 64
RELAY = "wss://bots.test"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def kind0(pubkey: str, *, bot: bool | None = None, name: str = "peer") -> dict:
    meta: dict = {"name": name}
    if bot is not None:
        meta["bot"] = bot
    return {
        "id": "0" * 63 + "1",
        "pubkey": pubkey,
        "kind": 0,
        "created_at": 1,
        "tags": [],
        "content": json.dumps(meta),
    }


# -- the flag ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("TRUE", True), ("yes", True),
    ("false", False), ("0", False), (1, True), (0, False), (None, None), ({}, None),
])
def test_bot_flag_reads_the_claim_whatever_the_client_wrote(value, expected):
    event = kind0(BOT, bot=value)
    if value == {}:
        event["content"] = json.dumps({"bot": {}})
    assert bot_flag(event) is expected


def test_bot_flag_is_none_when_the_profile_is_unreadable_or_not_a_profile():
    assert bot_flag({"kind": 0, "content": "{not json"}) is None
    assert bot_flag({"kind": 1, "content": json.dumps({"bot": True})}) is None
    assert bot_flag({"kind": 0, "content": json.dumps(["bot"])}) is None
    assert bot_flag({"kind": 0, "content": json.dumps({"name": "peer-bot"})}) is None


def test_profile_name_prefers_display_name():
    event = {"kind": 0, "content": json.dumps({"display_name": "peer-bot", "name": "peer-bot"})}
    assert profile_name(event) == "peer-bot"
    assert profile_name({"kind": 0, "content": "not json"}) is None


def test_registry_persists_the_flag_so_a_restart_does_not_refetch(tmp_path):
    store = Store(tmp_path / "gateway.db", identity=ME)
    first = BotRegistry(store)
    assert first.flag(BOT) is None, "unknown is not the same as human"
    first.ingest(kind0(BOT, bot=True))
    first.ingest(kind0(HUMAN, bot=False))
    assert first.is_bot(BOT) and not first.is_bot(HUMAN)

    restarted = BotRegistry(store, fetcher=lambda pk: pytest.fail("refetched a known peer"))
    assert restarted.is_bot(BOT)
    assert restarted.flag(HUMAN) is False
    assert restarted.status()["known"] == 2
    assert [row["pubkey"] for row in restarted.status()["flagged"]] == [BOT]


def test_a_profile_that_says_nothing_about_bot_is_a_human_and_is_not_refetched(tmp_path):
    store = Store(tmp_path / "gateway.db", identity=ME)
    registry = BotRegistry(store)
    registry.ingest(kind0(HUMAN))  # a real profile, no `bot` field at all
    assert registry.flag(HUMAN) is False
    assert store.profile_count() == 1


async def test_observe_fetches_kind0_once_and_only_until_it_is_known(tmp_path):
    store = Store(tmp_path / "gateway.db", identity=ME)
    calls: list[str] = []

    def fetcher(pubkey: str) -> dict:
        calls.append(pubkey)
        return kind0(pubkey, bot=True, name="peer-bot")

    registry = BotRegistry(store, fetcher=fetcher)
    assert registry.flag(BOT) is None  # schedules the fetch
    registry.observe(BOT)               # already in flight: must not fetch twice
    registry.observe(BOT)
    await asyncio.sleep(0.05)
    assert registry.is_bot(BOT)
    assert calls == [BOT]
    registry.observe(BOT)               # known: nothing to do
    await asyncio.sleep(0.02)
    assert calls == [BOT]


async def test_only_a_couple_of_profile_fetches_run_at_once(tmp_path):
    """A mention wave must not become a fleet of `nak` children."""
    store = Store(tmp_path / "gateway.db", identity=ME)
    calls: list[str] = []

    def fetcher(pubkey: str) -> dict | None:
        calls.append(pubkey)
        return None  # no profile on the relay

    registry = BotRegistry(store, fetcher=fetcher, max_in_flight=2)
    registry.flag("1" * 64)
    registry.flag("2" * 64)
    registry.flag("3" * 64)
    await asyncio.sleep(0.05)
    assert len(calls) == 2
    assert registry.flag("3" * 64) is None  # still unknown, judged as a human


async def test_a_peer_skipped_by_the_in_flight_cap_is_retried_later(tmp_path):
    store = Store(tmp_path / "gateway.db", identity=ME)
    calls: list[str] = []

    def fetcher(pubkey: str) -> dict | None:
        calls.append(pubkey)
        return None

    registry = BotRegistry(store, fetcher=fetcher, max_in_flight=1)
    registry.flag("1" * 64)
    registry.flag("2" * 64)          # no slot: stays unknown, not cached as human
    await asyncio.sleep(0.05)
    assert calls == ["1" * 64]
    registry.flag("2" * 64)          # slot is free again, so it retries
    await asyncio.sleep(0.05)
    assert calls == ["1" * 64, "2" * 64]


async def test_a_stale_flag_keeps_its_value_while_it_refreshes(tmp_path):
    store = Store(tmp_path / "gateway.db", identity=ME)
    clock = Clock()
    calls: list[str] = []

    def fetcher(pubkey: str) -> dict:
        calls.append(pubkey)
        return kind0(pubkey, bot=True)

    registry = BotRegistry(store, fetcher=fetcher, ttl_s=60, clock=clock)
    registry.ingest(kind0(BOT, bot=True))
    clock.t += 61
    # Stale, but still a bot: a refresh must never turn a loop back on mid-flight.
    assert registry.is_bot(BOT)
    await asyncio.sleep(0.05)
    assert calls == [BOT]
    assert registry.is_bot(BOT)


# -- the rules -----------------------------------------------------------

BOT_CONFIG = {
    "groups": {"bots": [BOT], "humans": [HUMAN]},
    "policies": [
        {"name": "bot-dms", "match": {"group": "bots", "kinds": [14, 1059], "dm": True},
         "reach": "immediate", "authority": "none"},
        {"name": "human-dms", "match": {"group": "humans", "kinds": [14, 1059], "dm": True},
         "reach": "immediate", "authority": "instructions"},
    ],
    "defaults": {"reach": "digest(15m)", "authority": "none"},
    "bots": {"max_replies_per_hour": 3, "echo_window": "10m", "digest_interval": "30m"},
}


def policy_for(clock: Clock, *, sent: list[tuple[float, str]] | None = None) -> Policy:
    """A policy whose bot lookup is exact and whose echo evidence respects the window."""
    evidence = list(sent or [])
    return Policy(
        BOT_CONFIG, clock=clock,
        bot_lookup=lambda sender: sender == BOT,
        echo_lookup=lambda _peer, since: [text for at, text in evidence if at >= since],
    )


def test_a_bot_mention_never_reaches_the_agent_immediately():
    clock = Clock()
    decision = policy_for(clock).decide(sender=BOT, kind=14, dm=True)
    assert decision.reach == "digest"
    assert decision.interval_s == 1800
    assert "bot peer" in decision.why
    # Reach is capped; authority is still whatever the operator configured.
    assert decision.authority == "none"


def test_a_human_is_unaffected_by_the_bot_rules():
    clock = Clock()
    decision = policy_for(clock).decide(sender=HUMAN, kind=14, dm=True)
    assert decision.reach == "immediate"
    assert decision.interval_s is None


def test_a_duplicate_from_a_bot_cannot_promote_the_digest_to_immediate():
    clock = Clock()
    policy = policy_for(clock)
    first = policy.decide(sender=BOT, kind=14, dm=True, event_id="e1")
    again = policy.decide(sender=BOT, kind=14, dm=True, event_id="e1")
    assert first.reach == "digest"
    assert again.duplicate and again.reach == "digest"


def test_our_own_text_returning_from_a_bot_is_an_echo():
    clock = Clock()
    sent = [(clock.t, "Thursday at seven, then.")]
    policy = policy_for(clock, sent=sent)
    decision = policy.decide(sender=BOT, kind=14, dm=True, text="  Thursday   at seven, then. ")
    assert decision.reach == "drop"
    assert decision.blocked_reason == "echo"
    assert decision.deliverable is False


def test_an_echo_outside_the_window_is_a_normal_message():
    clock = Clock()
    policy = policy_for(clock, sent=[(clock.t, "an older sentence")])
    clock.t += 601  # window is 10m
    decision = policy.decide(sender=BOT, kind=14, dm=True, text="an older sentence")
    assert decision.reach == "digest"
    assert decision.blocked_reason is None


def test_a_human_repeating_our_words_is_not_an_echo():
    clock = Clock()
    policy = policy_for(clock, sent=[(clock.t, "Thursday at seven, then.")])
    decision = policy.decide(sender=HUMAN, kind=14, dm=True,
                             text="Thursday at seven, then.")
    assert decision.reach == "immediate"


def test_the_reply_cap_refuses_the_message_after_the_limit():
    clock = Clock()
    policy = policy_for(clock)
    reaches = [policy.decide(sender=BOT, kind=14, dm=True, text=f"line {i}",
                             event_id=f"e{i}").reach
               for i in range(3)]
    assert reaches == ["digest", "digest", "digest"]
    fourth = policy.decide(sender=BOT, kind=14, dm=True, text="line 4", event_id="e4")
    assert fourth.reach == "drop"
    assert fourth.blocked_reason == "bot_reply_cap"
    assert "3/h" in fourth.why
    clock.t += 3601
    assert policy.decide(sender=BOT, kind=14, dm=True, text="line 5",
                         event_id="e5").reach == "digest"


def test_the_reply_cap_never_touches_a_human():
    clock = Clock()
    policy = policy_for(clock)
    # Within the per-sender bucket's burst (10), so this measures the bot rules alone:
    # the token bucket is a separate mechanism with its own test.
    for i in range(10):
        assert policy.decide(sender=HUMAN, kind=14, dm=True,
                             text=f"line {i}", event_id=f"h{i}").reach == "immediate"


def test_a_zero_cap_means_no_bot_traffic_at_all():
    clock = Clock()
    policy = Policy(
        {**BOT_CONFIG, "bots": {"max_replies_per_hour": 0}},
        clock=clock, bot_lookup=lambda sender: sender == BOT,
    )
    decision = policy.decide(sender=BOT, kind=14, dm=True, text="hello")
    assert decision.blocked_reason == "bot_reply_cap"


def test_bot_rules_are_off_when_the_block_says_so():
    clock = Clock()
    policy = Policy(
        {**BOT_CONFIG, "bots": {"enabled": False}},
        clock=clock, bot_lookup=lambda sender: True,
    )
    assert policy.decide(sender=BOT, kind=14, dm=True).reach == "immediate"


def test_bad_bot_config_is_rejected_not_ignored():
    with pytest.raises(ValueError):
        Policy({"bots": {"max_replies_per_hour": -1}})
    with pytest.raises(ValueError):
        Policy({"bots": {"echo_window": "soon"}})


def test_normalise_text_only_collapses_whitespace():
    assert normalise_text("  a \n b  ") == "a b"
    assert normalise_text("OK") != normalise_text("ok")


# -- the incident --------------------------------------------------------

def _outbound(store: Store, peer: str, text: str, at: int, n: int) -> None:
    store.add(Inbound(
        id=f"out-{n}", wrapper_id=f"out-wrap-{n}", sender=ME, kind=14,
        created_at=at, content=text, conversation=peer, reply_to=None,
        reach="sent", authority="self", rule="outbound", why="sent by us",
    ))


def _inbound(store: Store, peer: str, text: str, at: int, event_id: str,
             decision) -> bool:
    return store.add(Inbound(
        id=event_id, wrapper_id=event_id, sender=peer, kind=14,
        created_at=at, content=text, conversation=peer, reply_to=None,
        reach=decision.reach, authority=decision.authority, rule=decision.rule,
        why=decision.why, blocked_reason=decision.blocked_reason,
        interval_s=decision.interval_s,
    ))


def echo_lookup(store: Store):
    return lambda peer, since: [row["content"] or ""
                                for row in store.recent_outbound(peer, since)]


def test_the_3370_row_bounce_stops_at_the_first_echo(tmp_path):
    """The incident: two agents repeated one sentence for hours and stored 3,370 rows.

    The loop is modelled exactly: the bot says something, we answer it, the bot sends our
    answer straight back. The echo rule refuses that second message, so the loop ends after
    one exchange instead of running until somebody notices.
    """
    clock = Clock(1_000_000.0)
    store = Store(tmp_path / "gateway.db", identity=ME)
    policy = Policy(BOT_CONFIG, clock=clock, bot_lookup=lambda sender: sender == BOT,
                    echo_lookup=echo_lookup(store))

    sentence = "The Manchester meetup is on Thursday at seven."
    delivered: list[str] = []
    for round_no in range(3370):  # the incident's size, not a smoke test
        at = int(clock.t) + round_no * 10
        # The bot speaks first; if it is allowed through, we answer with the same words.
        decision = policy.decide(sender=BOT, kind=14, dm=True, text=sentence,
                                 event_id=f"bot-{round_no}")
        _inbound(store, BOT, sentence, at, f"bot-{round_no}", decision)
        if decision.deliverable:
            delivered.append(decision.reach)
            _outbound(store, BOT, sentence, at + 1, round_no)
            continue
        break

    assert delivered == ["digest"], "one exchange, then the echo stopped it"
    rows = store.recent(20)
    assert [row["blocked_reason"] for row in rows if row["blocked_reason"]] == ["echo"]
    assert store.stats()["total"] == 3, rows
    assert store.stats()["total"] < 3370


def test_a_bot_that_rewords_every_echo_is_stopped_by_the_cap(tmp_path):
    """Echo suppression catches the literal loop; the cap catches one that varies."""
    clock = Clock(1_000_000.0)
    store = Store(tmp_path / "gateway.db", identity=ME)
    policy = Policy(BOT_CONFIG, clock=clock, bot_lookup=lambda sender: sender == BOT,
                    echo_lookup=echo_lookup(store))

    delivered = 0
    blocked = []
    for round_no in range(500):
        at = int(clock.t) + round_no
        text = f"reworded bounce number {round_no}"
        decision = policy.decide(sender=BOT, kind=14, dm=True, text=text,
                                 event_id=f"v-{round_no}")
        _inbound(store, BOT, text, at, f"v-{round_no}", decision)
        if decision.deliverable:
            delivered += 1
            _outbound(store, BOT, text, at + 1, round_no)
        else:
            blocked.append(decision.blocked_reason)

    assert delivered == 3  # the configured cap, not 500
    assert blocked and set(blocked) == {"bot_reply_cap"}
    assert store.stats()["total"] <= 3 + 3 + 500  # bounded; no 3,370-row flood


# -- wiring: kind-0 in, decisions out ------------------------------------

def gateway_config(tmp_path, peer_hex: str) -> dict:
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()})
    )
    return {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {"peers": [peer_hex]},
        "policies": [
            {"name": "peer-dms",
             "match": {"group": "peers", "kinds": [14, 1059], "dm": True},
             "reach": "immediate", "authority": "instructions"},
        ],
        "defaults": {"reach": "drop", "authority": "none"},
        "bots": {"max_replies_per_hour": 2, "echo_window": "15m",
                 "digest_interval": "15m"},
        "marmot": {"enabled": False},
    }


def wrap_for(sender_keys: sdk.Keys, recipient_hex: str, text: str, offset: int = 0) -> dict:
    author = sender_keys.public_key().to_hex()
    rumor = build_rumor(author, recipient_hex, text, created_at=int(time.time()) + offset)
    unsigned = unsigned_event(rumor, author)
    return json.loads(
        sdk.nip59_make_gift_wrap(sender_keys, sdk.PublicKey.parse(recipient_hex), unsigned)
        .as_json()
    )


async def test_a_kind0_event_marks_the_peer_and_is_never_stored_as_a_message(tmp_path,
                                                                            monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    peer = sender.public_key().to_hex()
    gw = Gateway(gateway_config(tmp_path, peer), socket_path=tmp_path / "gateway.sock")
    await gw.start()
    try:
        gw._on_event(RELAY, kind0(peer, bot=True, name="peer-bot"))
        await gw.store_writer.flush()
        assert gw.bots.is_bot(peer)
        assert gw.store.stats()["total"] == 0, "a profile is not a message"

        gw._on_event(RELAY, wrap_for(sender, gw.me, "are you there?"))

        await gw.store_writer.flush()
        rows = gw.store.recent(5)
        row = next(r for r in rows if r["sender"] == peer)
        assert row["reach"] == "digest"
        assert row["rule"] == "peer-dms"
        assert row["authority"] == "instructions", "only reach is capped for a bot"
        assert gw.store.pending_immediate() == []
    finally:
        await gw.stop()


async def test_our_own_words_returning_from_a_bot_are_stored_as_blocked(tmp_path,
                                                                       monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    peer = sender.public_key().to_hex()
    gw = Gateway(gateway_config(tmp_path, peer), socket_path=tmp_path / "gateway.sock")
    await gw.start()
    try:
        gw._on_event(RELAY, kind0(peer, bot=True))
        await gw.store_writer.flush()
        gw.store.add(Inbound(
            id="sent-1", wrapper_id="sent-wrap-1", sender=gw.me, kind=14,
            created_at=int(time.time()), content="Thursday at seven?",
            conversation=peer, reply_to=None, reach="sent", authority="self",
            rule="outbound", why="sent by us",
        ))
        await gw.store_writer.flush()
        gw._on_event(RELAY, wrap_for(sender, gw.me, "Thursday at seven?"))
        await gw.store_writer.flush()
        rows = gw.store.recent(5)
        row = next(r for r in rows if r["sender"] == peer)
        assert row["blocked_reason"] == "echo"
        assert row["reach"] == "drop"
        blocked = gw.frame_for(Inbound(
            id=row["id"], wrapper_id=row["wrapper_id"], sender=row["sender"],
            kind=row["kind"], created_at=row["created_at"], content=row["content"],
            conversation=row["conversation"], reply_to=None, plane=row["plane"],
            reach=row["reach"], authority=row["authority"], rule=row["rule"],
            why=row["why"], blocked_reason=row["blocked_reason"],
        ))
        assert blocked["type"] == "blocked"
        assert blocked["reason"] == "echo"
        assert "content" not in blocked
    finally:
        await gw.stop()


async def test_status_reports_the_bot_rules_and_who_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    peer = sender.public_key().to_hex()
    gw = Gateway(gateway_config(tmp_path, peer), socket_path=tmp_path / "gateway.sock")
    await gw.start()
    try:
        gw._on_event(RELAY, kind0(peer, bot=True, name="peer-bot"))
        await gw.store_writer.flush()
        frame = await gw.status()
        bots = frame["bots"]
        assert bots["enabled"] is True
        assert bots["max_replies_per_hour"] == 2
        assert bots["echo_window_s"] == 900
        assert bots["digest_interval_s"] == 900
        assert [entry["pubkey"] for entry in bots["flagged"]] == [peer]
        assert bots["flagged"][0]["name"] == "peer-bot"
    finally:
        await gw.stop()
