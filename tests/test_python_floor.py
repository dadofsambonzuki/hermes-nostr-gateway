"""The declared Python floor is stated in four places; they have to name the same version.

The 3.11 floor was real on paper and never gated — CI ran 3.12 only — so nothing noticed that
the whole suite hangs on 3.11 (root cause in docs/decisions.md, "Python 3.12 is the floor").
These tests make a drift between the claim and the thing that proves it a test failure rather
than a contributor's afternoon.
"""

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _floor_from_pyproject() -> tuple[int, int]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    spec = data["project"]["requires-python"].strip()
    match = re.fullmatch(r">=\s*(\d+)\.(\d+)", spec)
    assert match, f"requires-python must be a plain >=X.Y floor, got {spec!r}"
    return int(match.group(1)), int(match.group(2))


def _ci_versions() -> list[tuple[int, int]]:
    workflow = yaml.safe_load((ROOT / ".ngit" / "act" / "workflows" / "test.yml").read_text())
    values = workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]
    versions: list[tuple[int, int]] = []
    for value in values:
        major, minor = str(value).split(".")[:2]
        versions.append((int(major), int(minor)))
    return versions


def _readme_floor() -> tuple[int, int]:
    text = (ROOT / "README.md").read_text()
    match = re.search(r"^Python (\d+)\.(\d+)\+ and ", text, re.MULTILINE)
    assert match, "README must state the floor as 'Python X.Y+ and ...'"
    return int(match.group(1)), int(match.group(2))


def _ruff_target() -> tuple[int, int]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    value = data["tool"]["ruff"]["target-version"]
    match = re.fullmatch(r"py(\d)(\d+)", value)
    assert match, f"unexpected [tool.ruff] target-version {value!r}"
    return int(match.group(1)), int(match.group(2))


def _docker_python() -> tuple[int, int]:
    text = (ROOT / "Dockerfile").read_text()
    match = re.search(r"^FROM python:(\d+)\.(\d+)", text, re.MULTILINE)
    assert match, "Dockerfile must name its Python as `FROM python:X.Y...`"
    return int(match.group(1)), int(match.group(2))


def test_ci_runs_the_declared_floor_and_a_version_above_it():
    floor = _floor_from_pyproject()
    versions = _ci_versions()
    assert floor in versions, (
        f"requires-python says >={floor[0]}.{floor[1]} but CI never runs it: {versions}"
    )
    assert any(version > floor for version in versions), (
        f"requires-python claims >={floor[0]}.{floor[1]} but CI only runs the floor itself: "
        f"{versions}"
    )


def test_readme_names_the_same_floor():
    assert _readme_floor() == _floor_from_pyproject(), (
        "the README's Python floor and pyproject's requires-python disagree"
    )


def test_ruff_targets_the_floor():
    assert _ruff_target() == _floor_from_pyproject(), (
        "ruff's target-version must match the floor, or lint accepts syntax the floor cannot run"
    )


def test_the_runtime_image_is_at_or_above_the_floor():
    assert _docker_python() >= _floor_from_pyproject(), (
        "the Dockerfile's Python is below the declared floor"
    )
