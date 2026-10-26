"""The wheel pin exists in two files; a drift between them silently installs the wrong one.

CI installs requirements.txt and then `pip install -e .`, so a pyproject URL that lags
behind replaces the good wheel with the old build — which is exactly how CI failed once
(eight Concord tests, all with 'does not expose nip44_v2_conversation_key').
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = re.compile(r"https://\S+/nostr_sdk-\S+\.whl")


def _urls(path: Path) -> list[str]:
    return URL.findall(path.read_text())


def test_requirements_and_pyproject_pin_the_same_wheel():
    req = _urls(ROOT / "requirements.txt")
    proj = _urls(ROOT / "pyproject.toml")
    assert req, "requirements.txt must pin the wheel by URL"
    assert proj, "pyproject.toml must pin the wheel by URL"
    assert req == proj, f"wheel pin drift: requirements={req} pyproject={proj}"


def test_requirements_pins_a_hash():
    text = (ROOT / "requirements.txt").read_text()
    assert re.search(r"--hash=sha256:[0-9a-f]{64}", text), "the wheel must be hash-pinned"
