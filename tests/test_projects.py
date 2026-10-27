"""Channel -> checkout mapping: the lookup, its scoping, and what it refuses to guess."""

from __future__ import annotations

import logging

import pytest

from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.projects import ProjectMap
from hermes_nostr_gateway.store import Store

COMMUNITY = "f56b69140a284cc9049761358be7f14893ccfb1cc03d4b30b1cc1c348f8d512f"
OPS_ID = "35dfdff55921366f368e7e108d49a4c95b0a110c91042269cb07d55164c1574a"


@pytest.fixture()
def checkout(tmp_path):
    repo = tmp_path / "hermes-nostr-gateway"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# notes\n")
    (repo / ".git").mkdir()
    return repo


def test_resolves_by_channel_name(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Ops", "path": str(checkout)},
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_name="ops")
    assert project is not None
    assert project.path == checkout
    assert project.name == "hermes-nostr-gateway"
    assert project.ok and project.is_git and project.has_agents_md


def test_resolves_by_channel_id_even_when_renamed(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": OPS_ID, "path": str(checkout), "name": "gateway"},
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_id=OPS_ID.upper(),
                              channel_name="renamed")
    assert project is not None and project.name == "gateway"


def test_id_match_beats_a_name_match(checkout, tmp_path):
    other = tmp_path / "btcmap"
    other.mkdir()
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Ops", "path": str(other)},          # matches by name only
        {"channel": OPS_ID, "path": str(checkout)},       # matches by id
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_id=OPS_ID, channel_name="Ops")
    assert project is not None and project.path == checkout


def test_community_scoping(checkout, tmp_path):
    scoped = tmp_path / "day-family"
    scoped.mkdir()
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Ops", "path": str(scoped), "community": COMMUNITY},
        {"channel": "Ops", "path": str(checkout)},        # any community
    ]})
    assert mapping.resolve(community_id=COMMUNITY, channel_name="Ops").path == scoped
    assert mapping.resolve(community_id="aa" * 32, channel_name="Ops").path == checkout


def test_unmapped_channel_is_none(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Ops", "path": str(checkout)},
    ]})
    assert mapping.resolve(community_id=COMMUNITY, channel_name="general") is None


def test_empty_config_resolves_nothing():
    assert ProjectMap.from_config({}).resolve(channel_name="Ops") is None
    assert ProjectMap.from_config({"projects": []}).resolve(channel_name="Ops") is None
    assert not ProjectMap.from_config({})


def test_missing_path_is_reported_and_never_resolved(tmp_path):
    missing = tmp_path / "gone"
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Ops", "path": str(missing)},
    ]})
    assert mapping.resolve(channel_name="Ops") is None
    (project,) = mapping.all()
    assert not project.ok and "does not exist" in project.problem


def test_tilde_is_expanded(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Projects" / "btcmap").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "btcmap", "path": "~/Projects/btcmap"},
    ]})
    project = mapping.resolve(channel_name="btcmap")
    assert project is not None and project.path == home / "Projects" / "btcmap"


def test_duplicate_mapping_keeps_the_first_and_warns(checkout, tmp_path, caplog):
    second = tmp_path / "second"
    second.mkdir()
    with caplog.at_level(logging.WARNING, logger="hng.projects"):
        mapping = ProjectMap.from_config({"projects": [
            {"channel": "Ops", "path": str(checkout)},
            {"channel": "Ops", "path": str(second)},
        ]})
    assert [p.path for p in mapping.all()] == [checkout]
    assert "mapped twice" in caplog.text


def test_map_form_is_accepted(checkout):
    mapping = ProjectMap.from_config({"projects": {"Ops": str(checkout)}})
    project = mapping.resolve(channel_name="Ops")
    assert project is not None and project.path == checkout


def test_plane_exposes_the_frame_field(checkout, tmp_path):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": OPS_ID, "path": str(checkout)},
    ]})
    with Store(tmp_path / "projects.db") as store:
        plane = ConcordPlane(store, projects=mapping)
        assert plane.project_for(community_id=COMMUNITY, channel_id=OPS_ID) == {
            "name": "hermes-nostr-gateway", "path": str(checkout)}
        assert plane.project_for(community_id=COMMUNITY, channel_name="general") is None
        # No map configured: the plane answers None rather than inventing a project.
        assert ConcordPlane(store).project_for(channel_name="Ops") is None
