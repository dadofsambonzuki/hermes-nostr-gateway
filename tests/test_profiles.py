"""Profiles: which Hermes profile a plane or channel runs as, and what refuses to guess.

The runtime is Hermes' (a profile is a `HERMES_HOME`, and a turn runs as one because
`SessionSource.profile` names it). What these tests pin is this gateway's half: the route
table for its own conversations, precedence, and every case in which it refuses rather than
letting a turn run as the default profile.

Placeholders only: no real community id, channel name or profile belongs in this file.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.profiles import (
    PLANES,
    ProfileRouter,
    ProfilesConfigError,
    channels_from_invites,
    hermes_profile_state,
    load_profiles,
    profile_home,
    require_profiles,
)

COMMUNITY = "ab" * 32
OTHER_COMMUNITY = "ef" * 32
CHANNEL_ID = "cd" * 32
CHANNEL_NAME = "example-channel"


# -- fixtures -----------------------------------------------------------


@pytest.fixture()
def hermes(tmp_path: Path) -> Path:
    """A miniature Hermes home: two live profiles, and a multiplexing gateway config."""
    home = tmp_path / "hermes"
    for name, marker in (("ops", ".env"), ("work", "config.yaml")):
        (home / "profiles" / name).mkdir(parents=True)
        (home / "profiles" / name / marker).write_text("model: {}\n")
    (home / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": True}}))
    return home


def config_for(hermes: Path, profiles: dict | None = None, **extra) -> dict:
    cfg = {"hermes_config": str(hermes / "config.yaml"), "profiles": profiles}
    cfg.update(extra)
    return cfg


def write_config(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "gateway.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def router_for(hermes: Path, profiles: dict) -> ProfileRouter:
    return load_profiles(config_for(hermes, profiles))


# -- precedence ---------------------------------------------------------


def test_channel_beats_plane_beats_default(hermes):
    router = router_for(hermes, {
        "default": "default",
        "planes": {"concord": "work"},
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    })
    routed = router.resolve("concord", f"concord:{COMMUNITY}:{CHANNEL_ID}")
    assert routed.ok and routed.profile == "ops" and routed.rule == "channel"
    assert "profiles.planes.concord" in routed.why and "profiles.default" in routed.why

    plane = router.resolve("concord", f"concord:{COMMUNITY}:{'ee' * 32}")
    assert plane.ok and plane.profile == "work" and plane.rule == "plane"

    fallback = router.resolve("marmot", "ff" * 32)
    assert fallback.ok and fallback.profile == "default" and fallback.rule == "default"


def test_a_channel_matches_on_id_before_name(hermes):
    """An id match beats a name match across the whole table, as the project map does."""
    router = router_for(hermes, {
        "default": "default",
        "channels": [
            {"channel": CHANNEL_ID, "profile": "work"},
            {"channel": CHANNEL_NAME, "profile": "ops"},
        ],
    })
    held = [types.SimpleNamespace(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                                 name=CHANNEL_NAME)]
    by_id = router.resolve("concord", f"concord:{COMMUNITY}:{CHANNEL_ID}", known_channels=held)
    assert by_id.profile == "work", "the id rule wins even though a name rule also applies"
    # The same room, named by a *different* id rule: only the name rule can match it.
    other = "ff" * 32
    held = [types.SimpleNamespace(community_id=COMMUNITY, channel_id=other,
                                 name=CHANNEL_NAME)]
    assert router.resolve("concord", f"concord:{COMMUNITY}:{other}",
                          known_channels=held).profile == "ops"


def test_a_community_scopes_only_its_own_rule(hermes):
    router = router_for(hermes, {
        "default": "default",
        "channels": [
            {"community": COMMUNITY, "channel": CHANNEL_NAME, "profile": "ops"},
            {"channel": "everywhere", "profile": "work"},
        ],
    })
    held = [
        types.SimpleNamespace(community_id=OTHER_COMMUNITY, channel_id=CHANNEL_ID,
                              name=CHANNEL_NAME),
        types.SimpleNamespace(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                              name=CHANNEL_NAME),
    ]
    assert router.resolve("concord", f"concord:{OTHER_COMMUNITY}:{CHANNEL_ID}",
                          known_channels=held).profile == "default"
    assert router.resolve("concord", f"concord:{COMMUNITY}:{CHANNEL_ID}",
                          known_channels=held).profile == "ops"


def test_a_default_of_another_profile_is_honoured(hermes):
    """`profiles.default` is a route like any other, not a synonym for the default profile."""
    router = router_for(hermes, {"default": "work"})
    resolution = router.resolve("marmot", "aa" * 32)
    assert resolution.profile == "work"
    assert resolution.frame() == {"profile": "work"}, "the frame must name it, not omit it"


def test_the_default_profile_is_omitted_from_the_frame(hermes):
    """Absent means "the receiving profile": the wire stays unchanged for unrouted traffic."""
    router = router_for(hermes, {"default": "default"})
    assert router.resolve("nip17", "aa" * 32).frame() == {}
    router = router_for(hermes, {"default": "ops"})
    assert router.resolve("nip17", "aa" * 32).frame() == {"profile": "ops"}


def test_an_unconfigured_block_resolves_nothing_and_is_not_an_error(hermes):
    router = load_profiles(config_for(hermes, None))
    assert not router.configured and router.usable is False
    resolution = router.resolve("nip17", "aa" * 32)
    assert resolution.ok and resolution.profile == ""
    assert "not configured" in resolution.why


# -- the load gate ------------------------------------------------------


def test_an_unknown_profile_is_a_load_error_that_names_the_fix(hermes):
    router = router_for(hermes, {"default": "default", "planes": {"marmot": "ops-lite"}})
    assert not router.usable
    assert any("no profile 'ops-lite'" in problem and "hermes profile create ops-lite" in problem
               for problem in router.problems)
    with pytest.raises(ProfilesConfigError):
        require_profiles(config_for(hermes, {"default": "default", "planes": {"marmot": "nope"}}))


def test_a_name_hermes_would_refuse_is_refused(hermes):
    router = router_for(hermes, {"default": "Ops"})
    assert any("not a Hermes profile name" in problem for problem in router.problems)


def test_a_deleted_profile_is_not_live(hermes):
    (hermes / "profiles" / ".deleted").mkdir()
    (hermes / "profiles" / ".deleted" / "ops").write_text("deleted\n")
    home = profile_home("ops", root=hermes / "profiles", default_home=hermes)
    assert not home.live and "was deleted" in home.problem


def test_a_directory_without_identity_is_not_a_profile(hermes):
    (hermes / "profiles" / "hollow").mkdir()
    home = profile_home("hollow", root=hermes / "profiles", default_home=hermes)
    assert not home.live and "not a Hermes profile" in home.problem


def test_a_standalone_profile_cannot_host_a_routed_turn(hermes):
    (hermes / "profiles" / "ops" / "config.yaml").write_text(
        yaml.safe_dump({"gateway": {"standalone": True}}))
    router = router_for(hermes, {"default": "default", "planes": {"nip17": "ops"}})
    assert any("gateway.standalone: true" in problem for problem in router.problems)


def test_a_parked_profile_is_a_warning_not_a_refusal(hermes):
    (hermes / "profiles" / "ops" / "gateway.parked").write_text("")
    router = router_for(hermes, {"default": "default", "planes": {"nip17": "ops"}})
    assert router.problems == ()
    assert any("parked" in warning and "hermes -p ops gateway start" in warning
               for warning in router.warnings)


def test_multiplexing_off_is_a_refusal(hermes):
    (hermes / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": False}}))
    router = router_for(hermes, {"default": "default", "planes": {"nip17": "ops"}})
    assert any("agent:main" in problem and "gateway.multiplex_profiles" in problem
               for problem in router.problems)


def test_a_block_that_routes_nothing_does_not_depend_on_multiplexing(hermes):
    """`default: default` and nothing else changes no turn, so the flag cannot break it."""
    (hermes / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": False}}))
    router = router_for(hermes, {"default": "default"})
    assert router.problems == () and router.usable


def test_an_unset_multiplex_flag_is_a_warning(hermes):
    (hermes / "config.yaml").write_text(yaml.safe_dump({"gateway": {}}))
    router = router_for(hermes, {"default": "default", "planes": {"nip17": "ops"}})
    assert router.problems == ()
    assert any("multiplexes profiles" in warning for warning in router.warnings)


def test_hermes_profile_state_reads_both_shapes(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"multiplex_profiles": True}))
    assert hermes_profile_state({"hermes_config": str(path)})["multiplex"] is True
    path.write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": False}}))
    assert hermes_profile_state({"hermes_config": str(path)})["multiplex"] is False
    path.write_text(yaml.safe_dump({"gateway": {}}))
    state = hermes_profile_state({"hermes_config": str(path)})
    assert state["multiplex"] is None and "defaults on" in state["source"]
    missing = hermes_profile_state({"hermes_config": str(tmp_path / "absent.yaml")})
    assert missing["multiplex"] is None and "no Hermes config" in missing["source"]


def test_the_default_profile_cannot_be_missing(hermes):
    router = router_for(hermes, {"planes": {"nip17": "ops"}})
    assert any("profiles.default is required" in problem for problem in router.problems)


def test_an_unknown_plane_is_a_load_error(hermes):
    router = router_for(hermes, {"default": "default", "planes": {"marmott": "ops"}})
    assert any("is not a plane this gateway attaches" in problem for problem in router.problems)
    assert "marmot" in PLANES and "concord" in PLANES


def test_a_duplicate_channel_rule_is_a_load_error(hermes):
    router = router_for(hermes, {
        "default": "default",
        "channels": [
            {"channel": CHANNEL_NAME, "profile": "ops"},
            {"channel": CHANNEL_NAME.upper(), "profile": "work"},
        ],
    })
    assert any("already routed by profiles.channels[0]" in problem
               for problem in router.problems)


def test_an_unknown_field_is_reported_not_ignored(hermes):
    router = router_for(hermes, {"default": "default", "channels": [
        {"channel": CHANNEL_NAME, "profile": "ops", "profle": "work"}]})
    assert any("unknown field(s) profle" in problem for problem in router.problems)


# -- channel rules against what the gateway holds ------------------------


def test_a_rule_for_a_channel_not_held_is_unresolved_not_the_plane(hermes):
    router = router_for(hermes, {
        "default": "default",
        "planes": {"concord": "work"},
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    })
    resolution = router.resolve("concord", f"concord:{COMMUNITY}:{CHANNEL_ID}",
                                known_channels=[])
    assert not resolution.ok and resolution.error == "route_unresolved"
    assert resolution.frame() == {}


def test_channel_problems_names_the_stale_rule(hermes):
    router = router_for(hermes, {
        "default": "default",
        "channels": [
            {"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"},
            {"channel": CHANNEL_NAME, "profile": "work"},
        ],
    })
    held = [types.SimpleNamespace(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                                 name="renamed")]
    assert router.channel_problems(held) == [
        "profiles.channels[1]: no channel 'example-channel' is held in any community"]


def test_channels_from_invites_unions_per_community():
    rows = [
        {"community_id": COMMUNITY,
         "bundle": {"channels": [{"id": CHANNEL_ID, "name": CHANNEL_NAME},
                                 {"id": "ee" * 32, "name": "other"}]}},
        {"community_id": COMMUNITY, "bundle": {"channels": [{"id": CHANNEL_ID, "name": "x"}]}},
        {"community_id": OTHER_COMMUNITY, "bundle": {"channels": []}},
    ]
    held = channels_from_invites(rows)
    assert {(c.community_id, c.channel_id) for c in held} == {
        (COMMUNITY, CHANNEL_ID), (COMMUNITY, "ee" * 32)}


# -- the CLI ------------------------------------------------------------


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_profiles_prints_the_table(tmp_path, hermes, capsys):
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "planes": {"marmot": "work"},
        "channels": [{"channel": CHANNEL_NAME, "profile": "ops"}],
    }))
    code, out, _ = run_cli(capsys, "--config", str(path), "profiles")
    assert code == 0
    assert "default" in out and "planes.marmot" in out and "channels[0]" in out
    assert "ops" in out and str(hermes / "profiles" / "ops") in out


def test_cli_profiles_check_fails_on_an_unknown_profile(tmp_path, hermes, capsys):
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default", "planes": {"nip17": "ghost"}}))
    code, out, err = run_cli(capsys, "--config", str(path), "profiles", "--check")
    assert code == 1
    assert "no profile 'ghost'" in err
    assert out == ""


def test_cli_profiles_check_passes_for_a_good_table(tmp_path, hermes, capsys):
    path = write_config(tmp_path, config_for(hermes, {"default": "default"}))
    code, out, _ = run_cli(capsys, "--config", str(path), "profiles", "--check")
    assert code == 0
    assert "usable" in out


def test_cli_profiles_check_does_not_call_an_unchecked_rule_stale(tmp_path, hermes, capsys):
    """No stored invite means "cannot say", not "every channel rule is wrong"."""
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    }))
    code, out, err = run_cli(capsys, "--config", str(path), "profiles", "--check")
    assert code == 0
    assert "usable" in out
    assert "were not checked" in err


def test_cli_profiles_check_names_a_stale_channel_rule(tmp_path, hermes, capsys):
    """With the invite in hand, a rule for a room the gateway does not hold is a failure."""
    from hermes_nostr_gateway.store import Store

    store_path = tmp_path / "gateway.db"
    store = Store(store_path)
    store.record_invite(id="f" * 64, community_id=COMMUNITY,
                        bundle={"channels": [{"id": "ee" * 32, "name": "renamed"}]},
                        relays=[], inviter="aa" * 32, name="community")
    store.close()
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    }, store=str(store_path)))
    code, _out, err = run_cli(capsys, "--config", str(path), "profiles", "--check")
    assert code == 1
    assert "no channel" in err and "is held" in err


def test_cli_route_prints_the_resolved_profile(tmp_path, hermes, capsys):
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "planes": {"concord": "work"},
        "channels": [{"community": COMMUNITY, "channel": CHANNEL_ID, "profile": "ops"}],
    }))
    code, out, _ = run_cli(capsys, "--config", str(path), "route",
                           f"concord:{COMMUNITY}:{CHANNEL_ID}")
    assert code == 0
    assert f"concord:{COMMUNITY}:{CHANNEL_ID}" in out
    assert "(concord; the conversation's shape)" in out
    assert "profile    ops" in out
    assert "channel rule profiles.channels[0]" in out


def test_cli_route_refuses_an_unknown_profile_at_read_time(tmp_path, hermes, capsys):
    """The daemon refuses to start; a read-only verb reports the same and exits 1."""
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default", "planes": {"nip17": "ghost"}}))
    code, out, _ = run_cli(capsys, "--config", str(path), "route", "aa" * 32, "--plane",
                           "nip17")
    assert code == 1
    assert "UNRESOLVED profile_invalid" in out
    assert "no profile 'ghost'" in out


def test_cli_route_reads_the_plane_from_the_store(tmp_path, hermes, capsys):
    """A Marmot group id is a hex string like a peer key, so the plane comes from evidence."""
    from hermes_nostr_gateway.store import Inbound, Store

    store_path = tmp_path / "gateway.db"
    store = Store(store_path)
    store.add(Inbound(id="e" * 64, wrapper_id="w" * 64, sender="aa" * 32, kind=9,
                      created_at=1, content="hello", conversation="bb" * 32, reply_to=None,
                      plane="marmot"))
    store.close()
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default", "planes": {"marmot": "work"}}, store=str(store_path)))
    code, out, _ = run_cli(capsys, "--config", str(path), "route", "bb" * 32)
    assert code == 0
    assert "(marmot; the store)" in out and "profile    work" in out


def test_cli_route_reports_an_ambiguous_plane(tmp_path, hermes, capsys):
    from hermes_nostr_gateway.store import Inbound, Store

    store_path = tmp_path / "gateway.db"
    store = Store(store_path)
    for plane, kind in (("nip17", 1059), ("nip04", 4)):
        store.add(Inbound(id=f"{kind}0".ljust(64, "0"), wrapper_id=f"{kind}1".ljust(64, "1"),
                          sender="aa" * 32, kind=kind, created_at=1,
                          content=f"a message on {plane}", conversation="bb" * 32,
                          reply_to=None, plane=plane))
    store.close()
    path = write_config(tmp_path, config_for(
        hermes, {"default": "default", "planes": {"nip17": "ops", "nip04": "work"}},
        store=str(store_path)))
    code, out, _ = run_cli(capsys, "--config", str(path), "route", "bb" * 32)
    assert code == 1
    assert "ambiguous (" in out and "hold it); pass --plane" in out
    assert "plane_unknown" in out


def test_cli_route_refuses_a_name_rule_it_cannot_check(tmp_path, hermes, capsys):
    """A name rule with no channel list to match it against is unresolved, never the plane."""
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "planes": {"concord": "work"},
        "channels": [{"channel": CHANNEL_NAME, "profile": "ops"}],
    }))
    code, out, _ = run_cli(capsys, "--config", str(path), "route",
                           f"concord:{COMMUNITY}:{CHANNEL_ID}")
    assert code == 1
    assert "route_unresolved" in out and "no channel list" in out


def test_cli_route_prints_the_model_route_alongside(tmp_path, hermes, capsys):
    """The join with model routing: one verb, both decisions.

    Both blocks are in this build, so this is no longer a stub: the profile half and the
    model half print from one command.
    """
    path = write_config(tmp_path, config_for(hermes, {
        "default": "default",
        "channels": [{"channel": CHANNEL_ID, "profile": "ops"}],
    }, routing={"models": ["model-a"],
                "default": {"provider": "provider-a", "model": "model-a"}}))
    code, out, _ = run_cli(capsys, "--config", str(path), "route",
                           f"concord:{COMMUNITY}:{CHANNEL_ID}")
    assert code == 0
    assert "profile    ops" in out
    assert "model      provider-a/model-a" in out


def test_cli_route_json_carries_both_halves(tmp_path, hermes, capsys):
    path = write_config(tmp_path, config_for(hermes, {"default": "ops"}))
    code, out, _ = run_cli(capsys, "--config", str(path), "route", "aa" * 32, "--plane",
                           "nip17", "--json")
    assert code == 0
    data = json.loads(out)
    assert data["profile"] == "ops" and data["rule"] == "default"
    assert data["plane"] == "nip17" and data["plane_from"] == "asked for with --plane"
    assert data["model"] == [], "no model routing in this build, nothing invented"
