"""Which provider, model and credential a conversation's turn asks for.

The block decides one thing per conversation, and every test here is about that being *explicit*:
a rule wins because it is more specific (channel beats plane beats default), a name that is not
known is an error rather than a fallback, a credential is a variable's name and never a value, and
the route a turn ran on is visible afterwards. The last of those is what an operator actually
needs when a channel's answers come out on the wrong model: `hng route` says which rule to edit.
"""

from __future__ import annotations

import json
from collections import deque

import nostr_sdk as sdk
import pytest
import yaml

import adapter_harness as harness
from hermes_nostr_gateway import cli
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.planes.concord.channels import ChannelStream
from hermes_nostr_gateway.routing import (
    KnownChannel,
    Router,
    RoutingConfigError,
    conversation_plane,
    load_router,
    reported_route,
)
from hermes_nostr_gateway.store import Inbound, Store

adapter = harness.load_adapter("nostr_adapter_routing")

COMMUNITY = "c0ffee" * 8
OTHER_COMMUNITY = "beef" * 16
OPS = "11" * 32
GENERAL = "22" * 32

CONFIG = {
    "models": ["model-a", "model-b"],
    "default": {"provider": "p1", "model": "model-a", "key": "GATEWAY_KEY"},
    "planes": {"concord": {"provider": "p2", "model": "model-b", "key": "CONCORD_KEY"}},
    "channels": [
        # By id, because an id is the durable identity: a rule that names a channel by its
        # current name needs the plane's channel list to match, so an id is what resolves
        # everywhere -- including in `hng route` on a box with no invites to read.
        {"community": COMMUNITY, "channel": OPS, "provider": "p3", "model": "model-b",
         "key": "WORK_KEY"},
    ],
}


def router_for(**overrides) -> Router:
    return Router.from_config({**CONFIG, **overrides})


def chat(channel_id: str = OPS, community: str = COMMUNITY) -> str:
    return f"concord:{community}:{channel_id}"


@pytest.fixture
def keys(monkeypatch):
    """Every credential the tests name, set. Never a real one: the point is the name."""
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    monkeypatch.setenv("CONCORD_KEY", "concord-value")
    monkeypatch.setenv("WORK_KEY", "work-value")


# ── precedence ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("plane,conversation,known,rule,model,key", [
    # A channel rule beats the plane and the default...
    ("concord", chat(OPS), [KnownChannel(COMMUNITY, OPS, "ops")], "channel", "model-b",
     "WORK_KEY"),
    # Even when the plane holds the channel but no channel rule names it.
    ("concord", chat(GENERAL), [KnownChannel(COMMUNITY, GENERAL, "general")], "plane",
     "model-b", "CONCORD_KEY"),
    # A plane with no rule of its own falls to the default.
    ("nip17", "ab" * 32, None, "default", "model-a", "GATEWAY_KEY"),
    ("marmot", "marmot:group", None, "default", "model-a", "GATEWAY_KEY"),
])
def test_precedence_is_channel_then_plane_then_default(keys, plane, conversation, known,
                                                      rule, model, key):
    resolution = router_for().resolve(plane, conversation, known_channels=known)

    assert resolution.ok, resolution.detail
    assert (resolution.rule, resolution.route.model, resolution.route.key) == (rule, model, key)


def test_a_channel_rule_may_name_the_channel_it_routes(keys):
    """A name is convenience, so matching one needs the channel list — an id never does."""
    router = Router.from_config({
        "models": ["model-a"],
        "default": {"provider": "p", "model": "model-a"},
        "channels": [{"community": COMMUNITY, "channel": "ops", "provider": "by-name",
                      "model": "model-a"}],
    })

    with_list = router.resolve("concord", chat("33" * 32),
                               known_channels=[KnownChannel(COMMUNITY, "33" * 32, "ops")])
    without = router.resolve("concord", chat("33" * 32), known_channels=None)

    assert with_list.rule == "channel" and with_list.route.provider == "by-name"
    # Without the list there is nothing to match the name against, so it falls through to
    # the next rule rather than claiming a route it cannot identify.
    assert without.rule == "default"


def test_the_winner_says_what_it_beat(keys):
    channel = router_for().resolve("concord", chat(OPS), known_channels=None)
    plane = router_for().resolve("concord", chat(GENERAL), known_channels=None)
    default = router_for().resolve("nip17", "ab" * 32)

    assert "beats routing.planes.concord and routing.default" in channel.why
    assert "beats routing.default" in plane.why
    assert "no channel or plane rule" in default.why


def test_a_channel_rule_matches_an_id_before_a_name(keys):
    """A renamed channel keeps its route: the id is the identity, the name is convenience."""
    router = Router.from_config({
        "models": ["model-a"],
        "default": {"provider": "p", "model": "model-a"},
        "channels": [
            {"channel": "ops", "provider": "by-name", "model": "model-a"},
            {"channel": OPS, "provider": "by-id", "model": "model-a"},
        ],
    })

    resolution = router.resolve("concord", chat(OPS),
                                known_channels=[KnownChannel(COMMUNITY, OPS, "ops")])

    assert resolution.route.provider == "by-id"


def test_a_community_only_scopes_its_own_rule(keys):
    router = router_for(channels=[
        {"community": OTHER_COMMUNITY, "channel": OPS, "provider": "elsewhere",
         "model": "model-b"},
    ])

    # The rule names another community's channel of the same id, so this one is the plane's.
    resolution = router.resolve("concord", chat(OPS),
                                known_channels=[KnownChannel(COMMUNITY, OPS, "ops")])

    assert resolution.rule == "plane"


# ── names that are not known ────────────────────────────────────────────────


def test_an_unknown_model_is_rejected_at_load():
    router = Router.from_config({
        "models": ["model-a"],
        "default": {"provider": "p", "model": "model-typo"},
    })

    assert router.problems
    assert "model-typo" in router.problems[0] and "routing.models" in router.problems[0]
    assert router.resolve("nip17", "ab" * 32).error == "route_invalid"


def test_a_block_that_cannot_be_loaded_is_refused_not_defaulted():
    with pytest.raises(RoutingConfigError):
        load_router({"default": {"provider": "p", "model": "model-a"}})


def test_an_unknown_plane_name_is_a_load_error():
    router = router_for(planes={"concordd": {"provider": "p", "model": "model-a"}})

    assert any("concordd" in problem and "not a plane" in problem
               for problem in router.problems)


def test_a_default_is_required_once_routing_is_configured():
    router = Router.from_config({"models": ["model-a"]})

    assert any("routing.default is required" in problem for problem in router.problems)


def test_a_duplicate_channel_rule_keeps_the_first():
    router = router_for(channels=[
        {"community": COMMUNITY, "channel": OPS, "provider": "first", "model": "model-a"},
        {"community": COMMUNITY, "channel": OPS.upper(), "provider": "second",
         "model": "model-b"},
    ])

    assert any("already routed" in problem for problem in router.problems)
    assert [rule.route.provider for rule in router.channels] == ["first"]


def test_a_misspelled_field_is_reported_rather_than_ignored():
    router = router_for(default={"provider": "p", "modell": "model-a"})

    assert any("modell" in problem for problem in router.problems)


# ── secrets are references ──────────────────────────────────────────────────


def test_a_key_is_a_name_and_a_value_is_refused_without_being_echoed():
    secret = "sk-live-abcdef0123456789"
    router = router_for(default={"provider": "p", "model": "model-a", "key": secret})

    assert router.problems
    assert not any(secret in problem for problem in router.problems)
    assert "NAME of an environment variable" in router.problems[0]


def test_a_long_token_without_a_separator_is_refused_as_a_value():
    token = "A" * 48
    router = router_for(default={"provider": "p", "model": "model-a", "key": token})

    assert router.problems
    assert not any(token in problem for problem in router.problems)


def test_a_missing_key_fails_closed_and_never_borrows_the_default_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    monkeypatch.setenv("CONCORD_KEY", "concord-value")
    monkeypatch.delenv("WORK_KEY", raising=False)
    known = [KnownChannel(COMMUNITY, OPS, "ops")]

    resolution = router_for().resolve("concord", chat(OPS), known_channels=known)

    assert resolution.error == "route_key_missing"
    assert resolution.route is None, "a refused route must not be usable by a caller"
    assert "$WORK_KEY" in resolution.detail
    assert "gateway-value" not in resolution.detail and "concord-value" not in resolution.detail


def test_a_key_is_read_at_use_time_from_the_environment_then_the_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WORK_KEY='from-the-file'\n# a comment\nOTHER=x\n")
    router = router_for(default={"provider": "p", "model": "model-a", "key": "WORK_KEY"},
                        env_file=str(env_file))
    route = router.default

    assert route.key_value() == "from-the-file"
    monkeypatch.setenv("WORK_KEY", "from-the-environment")
    assert route.key_value() == "from-the-environment", "the environment wins, as it should"

    monkeypatch.delenv("WORK_KEY")
    env_file.unlink()
    with pytest.raises(LookupError) as caught:
        route.key_value()
    assert "WORK_KEY" in str(caught.value) and "from-the-file" not in str(caught.value)


def test_a_route_asks_for_no_key_when_none_is_named(keys):
    route = router_for(default={"provider": "p", "model": "model-a"}).default

    assert route.key_value() == ""
    assert route.key_available() is True


def test_the_frame_carries_the_name_and_never_the_value(keys, monkeypatch):
    monkeypatch.setenv("WORK_KEY", "work-value")
    frame = router_for().resolve("concord", chat(OPS),
                                 known_channels=None).to_frame()

    assert frame["key"] == "WORK_KEY"
    assert "work-value" not in json.dumps(frame)


# ── Concord: a route is config, not protocol ────────────────────────────────


def test_a_channel_that_no_longer_exists_is_unresolved_not_the_plane_route(keys):
    resolution = router_for().resolve(
        "concord", chat("99" * 32),
        known_channels=[KnownChannel(COMMUNITY, GENERAL, "general")])

    assert resolution.error == "route_unresolved"
    assert resolution.route is None
    assert "99" * 32 in resolution.detail


def test_a_channel_list_that_is_not_known_yet_skips_the_check(keys):
    """Before the first refresh an empty list is "not asked", not "every channel is gone"."""
    resolution = router_for().resolve("concord", chat(OPS), known_channels=None)

    assert resolution.ok


def test_a_channel_rule_for_a_channel_this_gateway_does_not_hold_is_reported(keys):
    problems = router_for().channel_problems([KnownChannel(COMMUNITY, GENERAL, "general")])

    assert problems and OPS in problems[0]


def test_a_channel_rule_matching_a_held_id_is_not_reported(keys):
    problems = router_for().channel_problems([KnownChannel(COMMUNITY, OPS, "ops")])

    assert problems == []


# ── the conversation's plane ────────────────────────────────────────────────


def test_the_plane_is_read_from_the_conversation_shape():
    assert conversation_plane(chat(OPS)) == "concord"
    assert conversation_plane("marmot:group") == "marmot"
    assert conversation_plane("ab" * 32) == "nip17"
    assert conversation_plane("npub1" + "q" * 58) == "nip17"
    assert conversation_plane("something else") == ""


def test_an_unconfigured_gateway_resolves_nothing():
    router = Router.from_config(None)

    assert not router
    assert router.describe() == ["routing is not configured: every turn uses the agent's own "
                                 "model and credential"]


# ── the gateway: frames, replay, the reply's audit ──────────────────────────


def gateway_for(tmp_path, routing=None) -> Gateway:
    secret_file = tmp_path / "identity.json"
    secret_file.write_text(json.dumps({"secret_key_hex": sdk.Keys.generate()
                                       .secret_key().to_hex()}))
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": False, "hermes_platform": False},
    }
    if routing is not None:
        cfg["routing"] = routing
    return Gateway(cfg, socket_path=tmp_path / "gateway.sock")


def learn_channel(gateway: Gateway, *, channel_id: str = OPS, name: str = "ops") -> None:
    """What a refresh does, without the network: one readable channel, known from now on."""
    address = "aa" * 31 + channel_id[0]
    gateway.concord._streams[address] = ChannelStream(
        community_id=COMMUNITY, channel_id=channel_id, name=name, private=False, epoch=0,
        address=address, chat_id=chat(channel_id))
    gateway.concord.refreshed = True


def inbound(conversation: str, *, plane: str = "concord", **overrides) -> Inbound:
    fields = dict(id="r1", wrapper_id="w1", sender="ab" * 32, kind=9, created_at=1,
                  content="hello", conversation=conversation, reply_to=None, plane=plane,
                  reach="immediate", authority="instructions", rule="r", why="w")
    fields.update(overrides)
    return Inbound(**fields)


def test_a_frame_carries_the_route_its_turn_was_asked_for(tmp_path, keys, monkeypatch):
    gw = gateway_for(tmp_path, CONFIG)
    learn_channel(gw)
    monkeypatch.setenv("WORK_KEY", "work-value")
    try:
        frame = gw.frame_for(inbound(chat(OPS)))

        assert frame["type"] == "message"
        assert (frame["route"]["provider"], frame["route"]["model"]) == ("p3", "model-b")
        assert frame["route"]["key"] == "WORK_KEY"
        assert frame["route"]["rule"] == "channel"
        assert "work-value" not in json.dumps(frame)
    finally:
        gw.store.close()


def test_a_missing_key_makes_a_blocked_frame_instead_of_a_turn(tmp_path, keys, monkeypatch):
    gw = gateway_for(tmp_path, CONFIG)
    learn_channel(gw)
    monkeypatch.delenv("WORK_KEY", raising=False)
    try:
        frame = gw.frame_for(inbound(chat(OPS)))

        assert frame["type"] == "blocked"
        assert frame["reason"] == "route_key_missing"
        assert "$WORK_KEY" in frame["why"]
        assert "content" not in frame, "a refused turn must not look like chat"
    finally:
        gw.store.close()


def test_a_dm_frame_carries_the_default_route(tmp_path, keys):
    gw = gateway_for(tmp_path, CONFIG)
    try:
        frame = gw.frame_for(inbound("cd" * 32, plane="nip17", kind=14))

        assert frame["route"]["rule"] == "default"
        assert frame["route"]["model"] == "model-a"
    finally:
        gw.store.close()


def test_an_unrouted_gateway_sends_no_route_at_all(tmp_path):
    gw = gateway_for(tmp_path)
    try:
        frame = gw.frame_for(inbound(chat(OPS)))

        assert "route" not in frame
    finally:
        gw.store.close()


def test_a_replay_carries_the_same_route_and_refuses_when_the_key_is_gone(
        tmp_path, keys, monkeypatch):
    gw = gateway_for(tmp_path, CONFIG)
    learn_channel(gw)
    try:
        gw.store.add(inbound(chat(OPS)))
        replayed = gw.replay_pending()
        assert replayed and replayed[0]["route"]["key"] == "WORK_KEY"
        assert replayed[0]["replay"] is True

        monkeypatch.delenv("WORK_KEY", raising=False)
        refused = gw.replay_pending()
        assert refused[0]["type"] == "blocked"
        assert refused[0]["reason"] == "route_key_missing"
    finally:
        gw.store.close()


async def test_the_reply_records_the_route_the_adapter_reported(tmp_path, keys, monkeypatch):
    """The audit trail is per reply: what the turn ran on, not what the config meant."""
    gw = gateway_for(tmp_path, CONFIG)
    peer = sdk.Keys.generate().public_key().to_hex()
    rumor = {"id": "aa" * 32, "kind": 14, "created_at": 1, "content": "here you go"}
    wraps = {"peer": {"id": "bb" * 32, "kind": 1059}, "self": {"id": "cc" * 32, "kind": 1059}}
    monkeypatch.setattr(gw, "_build_wraps", lambda text, peer, reply_to: (rumor, wraps))
    monkeypatch.setattr(gw, "_recipient_relays", lambda peer: ["wss://relay.example"])

    async def fake_publish_all(wraps, relays):
        return {label: {relay: (True, None) for relay in relays} for label in wraps}

    monkeypatch.setattr(gw, "_publish_all", fake_publish_all)
    try:
        frame = await gw.send(to=peer, text="here you go",
                              route={"provider": "p3", "model": "model-b",
                                     "key": "WORK_KEY", "rule": "channel"})

        assert frame["route"]["model"] == "model-b"
        row = next(r for r in gw.store.recent(5) if r["id"] == "aa" * 32)
        assert row["route"]["model"] == "model-b" and row["route"]["key"] == "WORK_KEY"
    finally:
        gw.store.close()


def test_a_reported_key_that_is_a_value_is_dropped_not_stored():
    reported = reported_route({"provider": "p", "model": "model-b",
                               "key": "sk-live-0123456789", "rule": "channel"})

    assert reported == {"provider": "p", "model": "model-b", "rule": "channel"}


# ── the CLI ─────────────────────────────────────────────────────────────────


def write_config(tmp_path, routing) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "identity": {"secret_file": str(tmp_path / "identity.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "cli.db"),
        "groups": {}, "policies": [], "defaults": {},
        "routing": routing,
    }))
    return str(path)


def test_hng_route_prints_the_table(tmp_path, keys, capsys):
    path = write_config(tmp_path, CONFIG)

    assert cli.main(["--config", path, "route"]) == 0

    out = capsys.readouterr().out
    assert "default" in out and "planes.concord" in out
    assert "routing.channels[0]" in out and "$WORK_KEY (set)" in out


def test_hng_route_names_the_rule_that_won(tmp_path, keys, capsys):
    path = write_config(tmp_path, CONFIG)

    assert cli.main(["--config", path, "route", chat(OPS)]) == 0

    out = capsys.readouterr().out
    assert "model-b" in out and "beats routing.planes.concord and routing.default" in out
    assert "work-value" not in out


def test_hng_route_fails_a_conversation_whose_key_is_missing(tmp_path, keys, monkeypatch,
                                                             capsys):
    path = write_config(tmp_path, CONFIG)
    monkeypatch.delenv("WORK_KEY", raising=False)

    assert cli.main(["--config", path, "route", chat(OPS)]) == 1

    out = capsys.readouterr().out
    assert "route_key_missing" in out and "$WORK_KEY" in out


def test_hng_route_check_exits_non_zero_on_a_block_that_cannot_be_loaded(tmp_path, capsys):
    path = write_config(tmp_path, {"default": {"provider": "p", "model": "model-a"}})

    assert cli.main(["--config", path, "route", "--check"]) == 1

    assert "routing.models is required" in capsys.readouterr().err


def test_hng_route_check_reports_a_channel_rule_for_a_channel_that_is_gone(tmp_path, keys,
                                                                           capsys):
    store_path = tmp_path / "cli.db"
    with Store(store_path) as store:
        store.record_invite(id="w1", inviter="ab" * 32, community_id=COMMUNITY,
                            name="Example", relays=[], bundle={
                                "community_id": COMMUNITY,
                                "channels": [{"id": GENERAL, "name": "general"}]})
    path = write_config(tmp_path, CONFIG)

    assert cli.main(["--config", path, "route", "--check"]) == 1

    err = capsys.readouterr().err
    assert f"no channel {OPS!r} is held" in err


def test_hng_route_check_passes_for_a_channel_rule_the_gateway_holds(tmp_path, keys, capsys):
    store_path = tmp_path / "cli.db"
    with Store(store_path) as store:
        store.record_invite(id="w1", inviter="ab" * 32, community_id=COMMUNITY,
                            name="Example", relays=[], bundle={
                                "community_id": COMMUNITY,
                                "channels": [{"id": OPS, "name": "ops"}]})
    path = write_config(tmp_path, CONFIG)

    assert cli.main(["--config", path, "route", "--check"]) == 0
    assert "routing loads" in capsys.readouterr().out


def test_hng_serve_refuses_to_start_on_a_block_it_cannot_route_by(tmp_path, capsys):
    path = write_config(tmp_path, {"models": ["model-a"],
                                   "default": {"provider": "p", "model": "nope"}})

    assert cli.main(["--config", path, "serve"]) == 2
    assert "refusing to start: routing" in capsys.readouterr().err


# ── the adapter ─────────────────────────────────────────────────────────────


def _frame(**extra):
    return {"type": "message", "plane": "nip17", "id": "rumor-1", "sender": "ab" * 32,
            "conversation": "ab" * 32, "kind": 14, "created_at": 1761400000,
            "content": "hi", "reach": "immediate", "authority": "instructions", **extra}


def _adapter():
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._delivered_ids = deque(maxlen=512)
    inst._home_hex = ""
    inst._config = None
    inst.delivered = []
    inst.acked = []
    inst.requests = []

    async def _request(payload, timeout=30.0):
        inst.requests.append(payload)
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
        return {"type": "ok", "ok": True, "message_id": "m1"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


async def test_the_adapter_puts_the_route_on_the_event_and_reports_it_back(monkeypatch):
    monkeypatch.setenv("WORK_KEY", "work-value")
    inst = _adapter()
    frame = _frame(route={"provider": "p3", "model": "model-b", "key": "WORK_KEY",
                          "rule": "channel", "why": "channel rule"})

    await inst._deliver(frame)

    event = inst.delivered[0]
    assert event.metadata["nostr_route"]["model"] == "model-b"
    assert event.metadata["nostr_route"]["key"] == "WORK_KEY"
    assert "work-value" not in json.dumps(event.metadata)

    await inst.send(chat_id="ab" * 32, content="hi back")
    sent = next(request for request in inst.requests if request["op"] == "send")
    assert sent["route"]["model"] == "model-b" and sent["route"]["key"] == "WORK_KEY"


async def test_the_adapter_refuses_a_turn_whose_key_is_missing(monkeypatch):
    monkeypatch.delenv("WORK_KEY", raising=False)
    inst = _adapter()

    held = await inst._deliver(_frame(route={"provider": "p3", "model": "model-b",
                                             "key": "WORK_KEY", "rule": "channel"}))

    assert inst.delivered == [], "a turn must not run on another key's credential"
    assert inst.acked == [], "an unacknowledged frame is offered again once the key is there"
    assert held == adapter._HELD_ROUTE, (
        "an unacked frame has to be reported to the reader, or it is parked on a live socket"
    )


async def test_the_adapter_refuses_a_route_the_gateway_could_not_resolve():
    inst = _adapter()

    held = await inst._deliver(_frame(route={"error": "route_unresolved",
                                             "detail": "no channel", "rule": "channel"}))

    assert inst.delivered == [] and inst.acked == []
    assert held == adapter._HELD_ROUTE


async def test_an_unrouted_frame_is_delivered_exactly_as_before():
    inst = _adapter()
    frame = _frame()

    assert await inst._deliver(frame) is None, "a delivered frame is settled: nothing to hold"

    assert inst.delivered and inst.delivered[0].metadata == {}
    await inst.send(chat_id="ab" * 32, content="hi back")
    sent = next(request for request in inst.requests if request["op"] == "send")
    assert "route" not in sent
