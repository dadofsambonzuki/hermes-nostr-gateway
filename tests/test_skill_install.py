"""`hng skill install`: the repo's generic skill, installed idempotently.

The genericity gate lives here as well as in review: `skills/` is the half shipped with the
repository, so it must carry placeholders and none of this deployment's facts.
"""

from __future__ import annotations

from pathlib import Path

from hermes_nostr_gateway import cli

ROOT = Path(__file__).resolve().parents[1]
SKILL_FILE = ROOT / "skills" / "nostr-gateway" / "SKILL.md"

#: The deployment's own community id: the one value the repo's skill must never carry.
BOX_COMMUNITY = "20284389e292b1678490181d7902022e9b8e317effd0f808cc33ff41283abff0"


def test_print_prints_the_bundled_skill(capsys):
    assert cli.main(["skill", "install", "--print"]) == 0
    assert capsys.readouterr().out == SKILL_FILE.read_text(encoding="utf-8")


def test_install_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    target = tmp_path / "hermes" / "skills" / "nostr-gateway" / "SKILL.md"

    assert cli.main(["skill", "install"]) == 0
    assert target.read_text(encoding="utf-8") == SKILL_FILE.read_text(encoding="utf-8")
    assert "installed" in capsys.readouterr().out

    stamp = target.stat().st_mtime_ns
    assert cli.main(["skill", "install"]) == 0
    assert "already current" in capsys.readouterr().out
    assert target.stat().st_mtime_ns == stamp, "a second install rewrote the file"


def test_an_edited_copy_is_refused_until_forced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    target = tmp_path / "hermes" / "skills" / "nostr-gateway" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("edited by hand\n", encoding="utf-8")

    assert cli.main(["skill", "install"]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "edited by hand\n"

    assert cli.main(["skill", "install", "--force"]) == 0
    assert target.read_text(encoding="utf-8") == SKILL_FILE.read_text(encoding="utf-8")


def test_profile_install_uses_that_profiles_skills_tree(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert cli.main(["skill", "install", "--profile", "work"]) == 0
    target = (tmp_path / "hermes" / "profiles" / "work" / "skills" / "nostr-gateway"
              / "SKILL.md")
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == SKILL_FILE.read_text(encoding="utf-8")
    assert not (tmp_path / "hermes" / "skills" / "nostr-gateway").exists()


def test_the_bundled_skill_has_no_box_facts():
    text = SKILL_FILE.read_text(encoding="utf-8")
    for forbidden in ("day.ag", "npub1", BOX_COMMUNITY):
        assert forbidden not in text, f"the shipped skill leaks {forbidden!r}"
    # ...and it does carry the generic shape instead.
    assert "wss://relay.example" in text
    assert "<npub>" in text
    assert "concord:<community-id>:<channel-id>" in text


def test_the_bundled_skill_carries_the_procedure():
    """The sections the acceptance asks for are present, as procedure rather than prose."""
    text = SKILL_FILE.read_text(encoding="utf-8")
    for needle in ("hng doctor --json", "hng health --json", "hng status --json",
                   "hng drops --json", "hng recent", "hng projects --check",
                   "hng route", "ngit sync", "--dry-run", "pending", "held",
                   "ready", "refused", "dropped", "ngit:pr:<event id>",
                   "concord.projects", "PATH", "worktree"):
        assert needle in text, f"the shipped skill never mentions {needle!r}"
