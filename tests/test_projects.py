"""Channel -> checkout mapping: the lookup, its scoping, and what it refuses to guess.

Placeholders only: no real community id, repository or board slug belongs in this file.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.projects import ProjectMap, load_registry, project_for_conversation
from hermes_nostr_gateway.store import Store

COMMUNITY = "ab" * 32
CHANNEL_ID = "cd" * 32
CHANNEL_NAME = "example-channel"
PROJECT = "example-project"
BOARD = "example-board"


@pytest.fixture()
def checkout(tmp_path):
    repo = tmp_path / "example-checkout"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# notes\n")
    (repo / ".git").mkdir()
    return repo


def hermes_home(tmp_path: Path, *, registry: bool = True, project_slug: str = PROJECT,
                board_slug: str | None = BOARD) -> Path:
    """A miniature Hermes home: a project registry and a board, and nothing else."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    if registry:
        conn = sqlite3.connect(home / "projects.db")
        try:
            conn.execute(
                "create table projects (id text primary key, slug text not null unique,"
                " name text not null, board_slug text, primary_path text,"
                " created_at integer not null, archived integer not null default 0)")
            conn.execute("insert into projects values (?,?,?,?,?,?,0)",
                         ("p_example", project_slug, "Example Project", board_slug, "", 0))
            conn.commit()
        finally:
            conn.close()
    return home


def write_board(home: Path, *, slug: str = BOARD, project_id: str | None = "p_example") -> None:
    where = home / "kanban" / "boards" / slug
    where.mkdir(parents=True, exist_ok=True)
    (where / "board.json").write_text(json.dumps(
        {"slug": slug, "name": "Example Board", "project_id": project_id}))


def config_file(tmp_path: Path, projects: list[dict]) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"concord": {"projects": projects}}))
    return str(path)


def verdict(name: str, mark: str = "ok") -> str:
    """One check line as `hng projects --check` prints it: a fixed-width verdict column."""
    return f"{name:<9}{mark:<9}"


# -- parsing ---------------------------------------------------------------


def test_resolves_by_channel_name(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "Example-Channel", "path": str(checkout)},
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_name=CHANNEL_NAME)
    assert project is not None
    assert project.path == checkout
    assert project.name == "example-checkout"
    assert project.ok and project.is_git and project.has_agents_md
    # A checkout with no queue is manual work, not a broken mapping.
    assert project.project == "" and project.board == ""


def test_parses_the_project_and_board_it_declares(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(checkout),
         "project": PROJECT, "board": BOARD},
    ]})
    (project,) = mapping.all()
    assert project.project == PROJECT and project.board == BOARD
    assert project.ok and project.config_error == ""


def test_board_without_a_project_is_rejected_and_explained(checkout, caplog):
    with caplog.at_level(logging.WARNING, logger="hng.projects"):
        mapping = ProjectMap.from_config({"projects": [
            {"channel": CHANNEL_NAME, "path": str(checkout), "board": BOARD},
        ]})
    (project,) = mapping.all()
    assert not project.ok
    assert "without a project" in project.config_error
    assert "nothing anchors the worktree" in project.config_error
    assert CHANNEL_NAME in caplog.text and "without a project" in caplog.text
    # Not half-used: the declared queue is not in force, and the entry never resolves.
    assert project.board == "" and project.project == ""
    assert mapping.resolve(channel_name=CHANNEL_NAME) is None


def test_board_slug_must_be_one_path_segment(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(checkout), "project": PROJECT,
         "board": "../../etc"},
    ]})
    (project,) = mapping.all()
    assert not project.ok
    assert "not a slug" in project.config_error
    assert mapping.resolve(channel_name=CHANNEL_NAME) is None


def test_resolves_by_channel_id_even_when_renamed(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_ID, "path": str(checkout), "name": "example"},
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_id=CHANNEL_ID.upper(),
                              channel_name="renamed")
    assert project is not None and project.name == "example"


@pytest.mark.parametrize("declared", [CHANNEL_ID, CHANNEL_NAME])
def test_a_channel_id_and_name_both_reach_the_entry(checkout, declared):
    """A message carries both; either form of the mapping answers with the same queue."""
    mapping = ProjectMap.from_config({"projects": [
        {"channel": declared, "path": str(checkout), "project": PROJECT, "board": BOARD},
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                              channel_name=CHANNEL_NAME)
    assert project is not None
    assert (project.project, project.board) == (PROJECT, BOARD)


def test_id_match_beats_a_name_match(checkout, tmp_path):
    other = tmp_path / "example-other"
    other.mkdir()
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(other)},          # matches by name only
        {"channel": CHANNEL_ID, "path": str(checkout)},         # matches by id
    ]})
    project = mapping.resolve(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                              channel_name=CHANNEL_NAME)
    assert project is not None and project.path == checkout


def test_community_scoping(checkout, tmp_path):
    scoped = tmp_path / "example-scoped"
    scoped.mkdir()
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(scoped), "community": COMMUNITY},
        {"channel": CHANNEL_NAME, "path": str(checkout)},        # any community
    ]})
    assert mapping.resolve(community_id=COMMUNITY,
                           channel_name=CHANNEL_NAME).path == scoped
    assert mapping.resolve(community_id="aa" * 32, channel_name=CHANNEL_NAME).path == checkout


def test_unmapped_channel_is_none(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(checkout)},
    ]})
    assert mapping.resolve(community_id=COMMUNITY, channel_name="general") is None


def test_empty_config_resolves_nothing():
    assert ProjectMap.from_config({}).resolve(channel_name=CHANNEL_NAME) is None
    assert ProjectMap.from_config({"projects": []}).resolve(channel_name=CHANNEL_NAME) is None
    assert not ProjectMap.from_config({})


def test_missing_path_is_reported_and_never_resolved(tmp_path):
    missing = tmp_path / "gone"
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(missing)},
    ]})
    assert mapping.resolve(channel_name=CHANNEL_NAME) is None
    (project,) = mapping.all()
    assert not project.ok and "does not exist" in project.problem


def test_tilde_is_expanded(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Projects" / "example").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    mapping = ProjectMap.from_config({"projects": [
        {"channel": "example", "path": "~/Projects/example"},
    ]})
    project = mapping.resolve(channel_name="example")
    assert project is not None and project.path == home / "Projects" / "example"


def test_duplicate_mapping_keeps_the_first_and_warns(checkout, tmp_path, caplog):
    second = tmp_path / "second"
    second.mkdir()
    with caplog.at_level(logging.WARNING, logger="hng.projects"):
        mapping = ProjectMap.from_config({"projects": [
            {"channel": CHANNEL_NAME, "path": str(checkout)},
            {"channel": CHANNEL_NAME, "path": str(second)},
        ]})
    assert [p.path for p in mapping.all()] == [checkout]
    assert "mapped twice" in caplog.text


def test_map_form_is_accepted(checkout):
    mapping = ProjectMap.from_config({"projects": {CHANNEL_NAME: str(checkout)}})
    project = mapping.resolve(channel_name=CHANNEL_NAME)
    assert project is not None and project.path == checkout


# -- the registry and the check verb ---------------------------------------


def test_registry_reads_projects_and_boards(tmp_path):
    home = hermes_home(tmp_path)
    write_board(home)
    registry = load_registry(home)
    assert registry.error == ""
    assert registry.project(PROJECT)["board_slug"] == BOARD
    assert registry.board(BOARD)["slug"] == BOARD
    assert registry.board("no-such-board") is None


def test_a_missing_registry_is_reported_not_invented(tmp_path):
    registry = load_registry(tmp_path / "nowhere")
    assert registry.projects == {} and "no " in registry.error


def test_gaps_name_the_project_and_board_that_are_not_there(tmp_path):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(tmp_path), "project": PROJECT, "board": BOARD},
    ]})
    registry = load_registry(tmp_path / "nowhere")
    gaps = mapping.gaps(registry)
    assert [(project.channel, check.name) for project, check in gaps] == [
        (CHANNEL_NAME, "project"), (CHANNEL_NAME, "board")]
    # A channel that declares neither is manual work: no gap, no registry read.
    manual = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(tmp_path)}]})
    assert manual.gaps() == []


def test_check_passes_when_the_checkout_project_and_board_are_all_there(tmp_path, monkeypatch,
                                                                        capsys):
    home = hermes_home(tmp_path)
    write_board(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 0
    out = capsys.readouterr().out
    assert verdict("checkout") in out and verdict("project") in out and verdict("board") in out
    assert "bound to project" in out


def test_check_names_each_missing_piece_per_channel(tmp_path, monkeypatch, capsys):
    home = hermes_home(tmp_path)          # the project is registered, the board is not
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 1
    out = capsys.readouterr().out
    assert verdict("checkout") in out
    assert verdict("project") in out
    assert verdict("board", "MISSING") in out
    assert f"no board '{BOARD}'" in out
    assert "1 piece(s) missing across 1 channel(s)" in out


def test_check_reports_an_unregistered_project_and_a_missing_checkout(tmp_path, monkeypatch,
                                                                     capsys):
    home = hermes_home(tmp_path, registry=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME,
                                   "path": str(tmp_path / "gone"),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 1
    out = capsys.readouterr().out
    assert verdict("checkout", "MISSING") + "path does not exist" in out
    assert verdict("project", "MISSING") + "cannot read the project registry" in out


def test_check_reports_a_project_that_is_not_registered(tmp_path, monkeypatch, capsys):
    home = hermes_home(tmp_path, project_slug="some-other-project")
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 1
    out = capsys.readouterr().out
    assert verdict("project", "MISSING") + f"no project '{PROJECT}' registered" in out
    assert verdict("board", "MISSING") + f"no project '{PROJECT}' registered to anchor it" in out


def test_check_rejects_a_board_bound_to_another_project(tmp_path, monkeypatch, capsys):
    home = hermes_home(tmp_path, board_slug=None)
    write_board(home, project_id="p_something-else")
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 1
    out = capsys.readouterr().out
    assert f"board '{BOARD}' is not bound to project '{PROJECT}'" in out


def test_check_accepts_a_board_that_names_the_project_from_its_own_side(tmp_path, monkeypatch,
                                                                       capsys):
    """Hermes records the link from either side, so the board's own project_id counts."""
    home = hermes_home(tmp_path, board_slug=None)
    write_board(home, project_id="p_example")
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 0
    assert verdict("board") in capsys.readouterr().out


def test_check_reports_a_board_without_a_project_as_a_config_error(tmp_path, monkeypatch,
                                                                  capsys):
    home = hermes_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check"]) == 1
    out = capsys.readouterr().out
    assert verdict("config", "MISSING") in out and "without a project" in out


def test_json_carries_the_whole_mapping(tmp_path, capsys):
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [
        {"channel": CHANNEL_NAME, "path": str(checkout), "project": PROJECT, "board": BOARD},
        {"channel": "other-channel", "path": str(checkout)},
    ])

    assert cli.main(["--config", path, "projects", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["channel"] for row in rows] == [CHANNEL_NAME, "other-channel"]
    first = rows[0]
    assert (first["project"], first["board"]) == (PROJECT, BOARD)
    assert first["path"] == str(checkout) and first["ok"] is True
    assert first["config_error"] == "" and first["name"] == "example-checkout"
    # Absence is explicit, not an implied default.
    assert (rows[1]["project"], rows[1]["board"]) == ("", "")


def test_json_with_check_carries_the_verdicts(tmp_path, monkeypatch, capsys):
    home = hermes_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [{"channel": CHANNEL_NAME, "path": str(checkout),
                                   "project": PROJECT, "board": BOARD}])

    assert cli.main(["--config", path, "projects", "--check", "--json"]) == 1
    (row,) = json.loads(capsys.readouterr().out)
    assert [check["name"] for check in row["checks"]] == ["checkout", "project", "board"]
    assert {check["name"]: check["ok"] for check in row["checks"]} == {
        "checkout": True, "project": True, "board": False}


def test_plain_listing_shows_the_queue_each_channel_declares(tmp_path, capsys):
    checkout = tmp_path / "example-checkout"
    checkout.mkdir()
    path = config_file(tmp_path, [
        {"channel": CHANNEL_NAME, "path": str(checkout), "project": PROJECT, "board": BOARD},
        {"channel": "other-channel", "path": str(checkout)},
    ])

    assert cli.main(["--config", path, "projects"]) == 0
    out = capsys.readouterr().out
    assert f"project={PROJECT} board={BOARD}" in out
    assert "project=- board=-" in out


# -- identity, for routing -------------------------------------------------


def test_identify_answers_for_a_checkout_that_is_not_there(tmp_path):
    """Routing asks which project a channel is, not where its work starts."""
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(tmp_path / "gone"), "project": PROJECT},
    ]})
    (entry,) = mapping.all()

    assert not entry.ok, "the checkout is missing, so resolve() refuses it"
    assert mapping.resolve(channel_name=CHANNEL_NAME) is None
    assert mapping.identify(channel_name=CHANNEL_NAME).project == PROJECT


def test_identify_does_not_answer_for_a_mapping_with_no_project(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(checkout)},
    ]})

    assert mapping.identify(channel_name=CHANNEL_NAME) is None


def test_project_for_conversation_reads_a_name_mapped_entry_by_channel_id(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_NAME, "path": str(checkout), "project": PROJECT},
    ]})
    known = [SimpleNamespace(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                             name=CHANNEL_NAME)]

    assert project_for_conversation(f"concord:{COMMUNITY}:{CHANNEL_ID}", mapping,
                                    known) == PROJECT


def test_project_for_conversation_reads_an_id_mapped_entry_by_channel_name(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_ID, "path": str(checkout), "project": PROJECT},
    ]})
    known = [SimpleNamespace(community_id=COMMUNITY, channel_id=CHANNEL_ID,
                             name=CHANNEL_NAME)]

    assert project_for_conversation(f"concord:{COMMUNITY}:{CHANNEL_NAME}", mapping,
                                    known) == PROJECT


def test_project_for_conversation_is_empty_for_anything_but_a_channel(checkout):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_ID, "path": str(checkout), "project": PROJECT},
    ]})

    # A DM peer is a hex string too, so only the `concord:` shape may resolve a project.
    assert project_for_conversation("ab" * 32, mapping) == ""
    assert project_for_conversation(f"concord:{COMMUNITY}:{'99' * 32}", mapping) == ""
    assert project_for_conversation("", mapping) == ""


# -- the plane's own view --------------------------------------------------


def test_plane_exposes_the_frame_field(checkout, tmp_path):
    mapping = ProjectMap.from_config({"projects": [
        {"channel": CHANNEL_ID, "path": str(checkout)},
    ]})
    with Store(tmp_path / "projects.db") as store:
        plane = ConcordPlane(store, projects=mapping)
        assert plane.project_for(community_id=COMMUNITY, channel_id=CHANNEL_ID) == {
            "name": "example-checkout", "path": str(checkout)}
        assert plane.project_for(community_id=COMMUNITY, channel_name="general") is None
        # No map configured: the plane answers None rather than inventing a project.
        assert ConcordPlane(store).project_for(channel_name=CHANNEL_NAME) is None
