"""The queue worker's idle behaviour, tested as the monitor source the cron job now uses.

The acceptance criterion (QUEUE.md): an empty queue produces one line and no tool attempts. The
"no tool attempts" half is the scheduler's hash gate, so what is pinned here is the other half —
the single stable line it hashes, and the rule that the line moves exactly when the queue moves.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from queue_next import EMPTY_LINE, main, parse_items, render, state

REPO = Path(__file__).resolve().parents[1]

SAMPLE = """# Work queue

Some prose about how the queue works.

- [x] **Done thing** — already landed.
      — landed abc1234: it works.
- [ ] **Next thing** — do the next thing.
      Done when: it is done.
- [ ] **Parked thing** — needs a choice from Nathan.
      — BLOCKED: which way?

Rules for the cron: never work one already marked BLOCKED.
"""

EMPTY = """# Work queue

- [x] **Done thing** — already landed.
      — landed abc1234: it works.

Rules for the cron: one item per run.
"""


def test_first_unchecked_entry_is_the_one_offered():
    assert parse_items(SAMPLE)[0].checked is True
    assert render(SAMPLE).splitlines()[0] == "next: Next thing"


def test_blocked_entry_is_skipped_but_still_counted():
    assert "pending: 2 unchecked, 1 blocked" in render(SAMPLE)


def test_a_fully_parked_queue_reads_as_one_idle_line():
    text = "- [ ] **Parked** — needs a decision.\n      — BLOCKED: which way?\n"
    assert render(text) == "queue empty — 1 blocked, waiting on Nathan"
    assert len(render(text).splitlines()) == 1


def test_a_queue_with_only_finished_work_is_one_line():
    assert render(EMPTY) == EMPTY_LINE
    assert len(render(EMPTY).splitlines()) == 1


def test_rules_text_under_the_list_cannot_mark_the_last_item_blocked():
    text = (
        "- [ ] **Real item** — still to do.\n"
        "\n"
        "Rules for the cron: never work one already marked BLOCKED.\n"
    )
    item = parse_items(text)[0]
    assert (item.title, item.blocked) == ("Real item", False)
    assert render(text).splitlines()[0] == "next: Real item"


def test_output_is_stable_for_identical_input_and_moves_when_the_box_is_ticked():
    assert render(SAMPLE) == render(SAMPLE)
    ticked = SAMPLE.replace("- [ ] **Next thing**", "- [x] **Next thing**")
    assert render(ticked) != render(SAMPLE)


def test_waited_hours_are_whole_buckets_and_absent_without_an_mtime():
    assert "waited:" not in render(SAMPLE)
    assert render(SAMPLE, now=1000.0, mtime=1000.0).endswith("waited: 0h")
    assert render(SAMPLE, now=1000.0 + 3 * 3600 + 59, mtime=1000.0).endswith("waited: 3h")


def test_state_carries_the_same_facts_as_the_rendered_text():
    facts = state(SAMPLE, now=1000.0 + 7200, mtime=1000.0)
    assert facts == {
        "next": "Next thing",
        "unchecked": 2,
        "blocked": 1,
        "waited_hours": 2,
        "empty": False,
    }
    assert state(EMPTY)["empty"] is True


def test_an_unreadable_queue_is_a_failure_not_an_idle_line(tmp_path, capsys):
    assert main(["--queue", str(tmp_path / "missing.md")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_prose_without_entries_is_a_failure_not_an_idle_line(tmp_path, capsys):
    queue = tmp_path / "QUEUE.md"
    queue.write_text("# Work queue\n\nEverything was deleted by accident.\n", encoding="utf-8")
    assert main(["--queue", str(queue)]) == 2
    assert "no queue entries" in capsys.readouterr().err


def test_json_mode_prints_the_state(tmp_path, capsys):
    queue = tmp_path / "QUEUE.md"
    queue.write_text(SAMPLE, encoding="utf-8")
    assert main(["--queue", str(queue), "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["next"] == "Next thing"
    assert facts["unchecked"] == 2


@pytest.mark.parametrize("queue_name", ["QUEUE.md"])
def test_the_gate_wrapper_prints_exactly_what_the_module_prints(queue_name):
    """The live cron copy is a pass-through: prove the shell wrapper adds nothing and loses nothing."""
    queue = REPO / queue_name
    wrapper = subprocess.run(
        ["sh", str(REPO / "deploy" / "queue-gate.sh")],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "QUEUE_REPO": str(REPO), "QUEUE_FILE": str(queue)},
        check=True,
    )
    direct = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "queue_next.py"), "--queue", str(queue)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert wrapper.stdout == direct.stdout
    assert len(wrapper.stdout.strip().splitlines()) >= 1
