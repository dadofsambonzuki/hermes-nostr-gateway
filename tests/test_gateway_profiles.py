"""A routed conversation reaches Hermes carrying its profile -- or does not reach it at all.

The daemon's half of profile routing: resolve channel > plane > default for each frame, stamp
the answer on it, and refuse the frame when the profile it names cannot host a turn. That
covers all three ways a frame leaves this process -- live delivery, a replay after a crash, and
the refusal itself.

The runtime (a separate `HERMES_HOME` per profile, its own `state.db`, `agent:<profile>:`
session keys) is Hermes' and is exercised by Hermes' own tests; what is pinned here is the
contract between the two processes.

Placeholders only: no real community id, channel name or profile belongs in this file.
"""

from __future__ import annotations

import json
import time

import nostr_sdk as sdk
import pytest
import yaml

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.profiles import ProfilesConfigError
from hermes_nostr_gateway.store import Inbound, Store

COMMUNITY = "ab" * 32
CHANNEL_ID = "cd" * 32
PEER = "ee" * 32
GROUP = "aa" * 32


def hermes_home(tmp_path, *, multiplex: bool | None = True) -> str:
    """A Hermes home with one live profile and the multiplex flag set as asked."""
    home = tmp_path / "hermes"
    (home / "profiles" / "ops").mkdir(parents=True)
    (home / "profiles" / "ops" / ".env").write_text("A=1\n")
    gateway = {} if multiplex is None else {"multiplex_profiles": multiplex}
    (home / "config.yaml").write_text(yaml.safe_dump({"gateway": gateway}))
    return str(home / "config.yaml")


def config_for(tmp_path, profiles, *, multiplex: bool | None = True) -> dict:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(
        {"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    return {
        "identity": {"secret_file": str(secret)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {},
        "policies": [],
        "defaults": {"reach": "immediate", "authority": "instructions"},
        "hermes_config": hermes_home(tmp_path, multiplex=multiplex),
        "profiles": profiles,
    }


@pytest.fixture(autouse=True)
def no_lookups(monkeypatch):
    """Constructing a Gateway must not reach the network (kind-3 follows, wn-agent)."""
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


def held_channels():
    """What the Concord plane holds: the one channel the rules below name."""
    return [_stream(COMMUNITY, CHANNEL_ID, "example")]


def _stream(community: str, channel: str, name: str):
    from types import SimpleNamespace
    return SimpleNamespace(community_id=community, channel_id=channel, name=name)


def frame(plane: str = "concord", conversation: str | None = None, **extra) -> dict:
    data = {"type": "message", "plane": plane, "id": "e" * 64, "sender": PEER,
            "conversation": conversation or f"concord:{COMMUNITY}:{CHANNEL_ID}",
            "content": "hello"}
    data.update(extra)
    return data


def routed(gateway: Gateway, frame_: dict, conversation: str) -> dict:
    """The frame as the delivery path would send it for that conversation."""
    return gateway._routed(frame_, plane=frame_["plane"], conversation=conversation,
                           sender=PEER, message_id=frame_["id"])


def pending(store: Store, *, plane: str, conversation: str = GROUP) -> None:
    store.add(Inbound(id="e" * 64, wrapper_id="w" * 64, sender=PEER, kind=9,
                      created_at=int(time.time()), content="hello",
                      conversation=conversation, reply_to=None, plane=plane,
                      reach="immediate", authority="instructions"))


def test_a_channel_route_stamps_the_frame(tmp_path):
    cfg = config_for(tmp_path, {
        "default": "default",
        "planes": {"concord": "default"},
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    })
    gateway = Gateway(cfg)
    gateway.concord.channels = held_channels  # the live plane's own list, without a refresh
    conversation = f"concord:{COMMUNITY}:{CHANNEL_ID}"
    out = routed(gateway, frame(), conversation)
    assert out["type"] == "message"
    assert out["profile"] == "ops"
    assert out["content"] == "hello", "routing adds a field, it does not rewrite the message"


def test_a_plane_route_applies_to_every_conversation_on_it(tmp_path):
    gateway = Gateway(config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}}))
    assert routed(gateway, frame("marmot", GROUP), GROUP)["profile"] == "ops"


def test_an_unrouted_conversation_carries_no_profile(tmp_path):
    """Absent means the receiving profile: the wire is unchanged for everything unrouted."""
    gateway = Gateway(config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}}))
    out = routed(gateway, frame("nip17", PEER), PEER)
    assert "profile" not in out and out["type"] == "message"


def test_no_profiles_block_changes_nothing(tmp_path):
    gateway = Gateway(config_for(tmp_path, None))
    out = routed(gateway, frame("nip17", PEER), PEER)
    assert "profile" not in out and out["type"] == "message"


def test_a_profile_deleted_after_load_refuses_the_frame(tmp_path):
    """The load-time check covers config; this is the same question asked again at delivery."""
    gateway = Gateway(config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}}))
    assert routed(gateway, frame("marmot", GROUP), GROUP)["profile"] == "ops"

    (tmp_path / "hermes" / "profiles" / "ops" / ".env").unlink()
    out = routed(gateway, frame("marmot", GROUP), GROUP)
    assert out["type"] == "blocked"
    assert out["reason"] == "profile_unknown"
    assert "not a Hermes profile" in out["detail"]
    assert "content" not in out, "a refused frame must never look like chat"


def test_a_profile_deleted_after_load_is_refused_even_as_the_default(tmp_path):
    gateway = Gateway(config_for(tmp_path, {"default": "ops"}))
    (tmp_path / "hermes" / "profiles" / "ops").rename(tmp_path / "hermes" / "gone")
    out = routed(gateway, frame("nip17", PEER), PEER)
    assert out["type"] == "blocked" and out["reason"] == "profile_unknown"


def test_a_replayed_frame_carries_the_profile_too(tmp_path):
    """A crash between the store write and the ack replays the message, not a default turn."""
    gateway = Gateway(config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}}))
    pending(gateway.store, plane="marmot")
    frames = gateway.replay_pending()
    assert [item["profile"] for item in frames] == ["ops"]
    assert frames[0]["replay"] is True


def test_a_replay_whose_profile_is_gone_is_blocked_not_run(tmp_path):
    """A refused replay is offered as `blocked`, the same as a refused route.

    It never becomes a run -- `blocked` is not `message` -- but it is visible rather than
    silent: the row stays pending, so the turn runs once the profile is back, and the
    operator sees why it did not.
    """
    gateway = Gateway(config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}}))
    pending(gateway.store, plane="marmot")
    (tmp_path / "hermes" / "profiles" / "ops" / ".env").unlink()
    (frame,) = gateway.replay_pending()
    assert frame["type"] == "blocked" and frame["reason"] == "profile_unknown"


def test_the_daemon_refuses_to_start_with_an_unknown_profile(tmp_path):
    cfg = config_for(tmp_path, {"default": "default", "planes": {"marmot": "ghost"}})
    with pytest.raises(ProfilesConfigError) as caught:
        Gateway(cfg)
    assert "no profile 'ghost'" in str(caught.value)
    assert "hermes profile create ghost" in str(caught.value)


def test_the_daemon_refuses_a_non_multiplexed_hermes(tmp_path):
    cfg = config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}},
                     multiplex=False)
    with pytest.raises(ProfilesConfigError) as caught:
        Gateway(cfg)
    assert "agent:main" in str(caught.value)


def test_an_unset_multiplex_flag_starts_with_a_warning(tmp_path):
    cfg = config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}},
                     multiplex=None)
    gateway = Gateway(cfg)
    assert gateway.profiles.problems == ()
    assert any("multiplexes profiles" in warning for warning in gateway.profiles.warnings)


def test_a_standalone_profile_cannot_be_routed_to(tmp_path):
    cfg = config_for(tmp_path, {"default": "default", "planes": {"marmot": "ops"}})
    (tmp_path / "hermes" / "profiles" / "ops" / "config.yaml").write_text(
        yaml.safe_dump({"gateway": {"standalone": True}}))
    with pytest.raises(ProfilesConfigError) as caught:
        Gateway(cfg)
    assert "gateway.standalone: true" in str(caught.value)
