"""The shipped pass units: one oneshot per channel, a timer that carries the credential.

The host proof is the cutover's (`systemctl start`, then an unattended tick). Everything
that can be checked without installing anything is checked here: the template pair, the
credential, the interval, the stagger and the kill timeout.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SERVICE = DEPLOY / "hermes-nostr-gateway-ngit@.service"
TIMER = DEPLOY / "hermes-nostr-gateway-ngit@.timer"

CREDENTIAL = "nostr_secret:/etc/hermes-nostr-gateway/identity.json"


def sections(path: Path) -> dict[str, dict[str, list[str]]]:
    """A unit file as `{section: {key: [values]}}`, keeping repeated keys (`Environment=`)."""
    parsed: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = parsed.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"a setting before any section in {path.name}: {line}"
        key, sep, value = line.partition("=")
        assert sep, f"not a setting in {path.name}: {line}"
        current.setdefault(key.strip(), []).append(value.strip())
    return parsed


def one(unit: dict[str, dict[str, list[str]]], section: str, key: str) -> str:
    values = unit.get(section, {}).get(key) or []
    assert len(values) == 1, f"{section}.{key} appears {len(values)} times"
    return values[0]


def environment(unit: dict[str, dict[str, list[str]]], name: str) -> str:
    for value in unit.get("Service", {}).get("Environment", []):
        key, sep, rest = value.partition("=")
        if sep and key == name:
            return rest
    return ""


def test_the_two_units_are_one_template_pair():
    """`foo@bar.timer` starts `foo@bar.service` by default, so the stems have to match."""
    assert SERVICE.name == "hermes-nostr-gateway-ngit@.service"
    assert TIMER.name == "hermes-nostr-gateway-ngit@.timer"
    assert TIMER.name.replace(".timer", ".service") == SERVICE.name


def test_the_service_is_a_oneshot_for_one_channel_pass():
    unit = sections(SERVICE)
    assert one(unit, "Service", "Type") == "oneshot"
    exec_start = one(unit, "Service", "ExecStart")
    assert "--config" in exec_start
    assert "ngit sync --channel %i --json" in exec_start
    assert "%i" in exec_start, "the channel is the unit instance, not a written-out id"


def test_the_pass_reads_the_identity_as_the_daemons_credential():
    """The half of the bug this fixes: a scheduled pass can read `secret_credential` too."""
    unit = sections(SERVICE)
    assert one(unit, "Service", "LoadCredential") == CREDENTIAL


def test_a_hung_pass_is_killed_before_the_next_tick():
    unit = sections(SERVICE)
    timeout = int(one(unit, "Service", "TimeoutStartSec"))
    assert 0 < timeout < 10 * 60, "a pass must not outlive the ten-minute interval"


def test_the_pass_finds_both_binaries_it_shells_out_to():
    unit = sections(SERVICE)
    path = environment(unit, "PATH")
    assert "/.venv/bin" in path, "hng comes from the checkout's virtualenv"
    assert "/.local/bin" in path, "ngit comes from the operator's local bin"


def test_the_timer_is_ten_minutes_and_survives_downtime():
    unit = sections(TIMER)
    assert one(unit, "Timer", "OnCalendar") == "*:0/10"
    assert one(unit, "Timer", "Persistent") == "true"


def test_instances_are_staggered_so_two_passes_do_not_start_together():
    unit = sections(TIMER)
    delay = one(unit, "Timer", "RandomizedDelaySec")
    match = re.fullmatch(r"(\d+)", delay)
    assert match, f"expected a plain number of seconds, got {delay!r}"
    assert 0 < int(match.group(1)) <= 60


def test_the_timer_is_what_gets_enabled():
    unit = sections(TIMER)
    assert one(unit, "Install", "WantedBy") == "timers.target"
    assert "Unit" not in unit.get("Timer", {}), (
        "no Unit= override: the default same-instance service is the pair shipped here")
