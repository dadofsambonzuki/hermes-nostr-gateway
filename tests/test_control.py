"""The control pane: where a confirmation goes, and whose word skips the question."""

from __future__ import annotations

import nostr_sdk as sdk

from hermes_nostr_gateway.control import ControlConfig, as_hex

NATHAN = sdk.Keys.generate().public_key()
SAM = sdk.Keys.generate().public_key()
PEER = sdk.Keys.generate().public_key()


def test_no_config_means_no_destination_and_nobody_trusted():
    control = ControlConfig.from_config(None)
    assert control.target("concord") is None
    assert control.trusted == frozenset()
    assert control.is_trusted(NATHAN.to_bech32()) is False


def test_a_destination_per_plane_is_read_and_kept_verbatim():
    control = ControlConfig.from_config({
        "concord": "concord:" + "ab" * 32 + ":" + "11" * 32,
        "nip17": NATHAN.to_bech32(),
        "marmot": "group-42",
    })
    assert control.target("concord") == "concord:" + "ab" * 32 + ":" + "11" * 32
    assert control.target("NIP17") == NATHAN.to_bech32(), "lookup is case-insensitive"
    assert control.target("marmot") == "group-42"
    assert control.target("nip04") is None, "a plane with no entry says so, it does not guess"


def test_a_plane_that_is_not_a_string_is_reported_not_used():
    control = ControlConfig.from_config({"concord": {"channel": "ops"}})
    assert control.target("concord") is None


def test_trusted_keys_are_normalised_so_an_npub_matches_the_hex_a_frame_carries():
    control = ControlConfig.from_config({"trusted_npubs": [NATHAN.to_bech32()]})
    assert control.trusted == frozenset({NATHAN.to_hex()})
    assert control.is_trusted(NATHAN.to_hex()) is True
    assert control.is_trusted(NATHAN.to_bech32().upper()) is True
    assert control.is_trusted(SAM.to_bech32()) is False
    assert control.is_trusted("not-a-key") is False
    assert control.is_trusted("") is False


def test_a_plane_list_adds_to_the_global_one_rather_than_replacing_it():
    control = ControlConfig.from_config({
        "trusted_npubs": [NATHAN.to_bech32()],
        "planes": {"concord": {"trusted_npubs": [SAM.to_bech32()]}},
    })
    assert control.trusted_in("concord") == frozenset({NATHAN.to_hex(), SAM.to_hex()})
    assert control.trusted_in("nip17") == frozenset({NATHAN.to_hex()})
    assert control.is_trusted(SAM.to_bech32(), plane="concord") is True
    assert control.is_trusted(SAM.to_bech32(), plane="nip17") is False


def test_junk_entries_are_reported_rather_than_silently_dropped():
    control = ControlConfig.from_config({
        "trusted_npubs": [NATHAN.to_bech32(), "nobody"],
        "planes": {"concord": {"trusted_npubs": ["also-nobody"]}},
    })
    assert control.trusted == frozenset({NATHAN.to_hex()})
    assert any("nobody" in problem for problem in control.problems)
    assert any("concord" in problem for problem in control.problems)


def test_describe_names_every_plane_including_the_unconfigured_ones():
    control = ControlConfig.from_config({"concord": "concord:aa:bb",
                                        "trusted_npubs": [NATHAN.to_bech32()]})
    lines = control.describe()
    assert any(line.startswith("concord") and "concord:aa:bb" in line for line in lines)
    assert any(line.startswith("nip17") and "not configured" in line for line in lines)
    assert any("1 trusted npub(s)" in line for line in lines)


def test_as_hex_takes_both_forms_and_refuses_anything_else():
    assert as_hex(NATHAN.to_bech32()) == NATHAN.to_hex()
    assert as_hex(NATHAN.to_hex().upper()) == NATHAN.to_hex()
    assert as_hex("") == ""
    assert as_hex("nsec1notapubkey") == ""
