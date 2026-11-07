"""`hng deps`: the `deps:` block, the dry run, and the dispatch from the CLI.

The pipeline is exercised against a miniature repository and stub tools: a real git
checkout, a real `git diff`, and a scanner that answers with one finding and then clears it.
That is enough to prove what the command decides and — for the dry run — what it refuses to
write. Placeholders only: no real npub, host or repository belongs in this file.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.deps import (
    DepsConfig,
    DepsConfigError,
    DepsRepo,
    cleared_advisories,
    findings,
    load_deps_config,
    run_deps,
)

ROOT = Path(__file__).resolve().parents[1]
OWNER = "npub1exampleowner"
SIGNER = "npub1examplebot"
COORDINATOR = "npub1examplecoordinator"


def block(**overrides):
    raw = {
        "repositories": [{"name": "example-repo", "owner": OWNER, "branch": "main"}],
        "min_severity": 7,
        "workspace": "/opt/deps-bot",
        "state_dir": "/var/lib/nostr-deps",
        "log": "/var/log/nostr-deps.log",
        "signer": SIGNER,
        "coordinator": COORDINATOR,
        "workflow": ".ngit/act/workflows/dependencies.yml",
        "open_prs": True,
    }
    raw.update(overrides)
    return {"deps": raw}


# -- the `deps:` block --------------------------------------------------------

def test_the_whole_block_parses():
    cfg = load_deps_config(block())
    assert cfg.repositories == (DepsRepo("example-repo", OWNER, "main"),)
    assert cfg.min_severity == 7.0
    assert str(cfg.workspace) == "/opt/deps-bot"
    assert str(cfg.state_dir) == "/var/lib/nostr-deps"
    assert str(cfg.log) == "/var/log/nostr-deps.log"
    assert cfg.signer == SIGNER
    assert cfg.coordinator == COORDINATOR
    assert cfg.workflow == ".ngit/act/workflows/dependencies.yml"
    assert cfg.open_prs is True


def test_defaults_fill_in_and_the_branch_defaults_to_main():
    cfg = load_deps_config({"deps": {"repositories": [{"name": "one", "owner": OWNER}]}})
    assert cfg.repositories == (DepsRepo("one", OWNER, "main"),)
    assert cfg.min_severity == 7.0
    assert str(cfg.workspace) == "/opt/deps-bot"
    assert cfg.open_prs is True
    assert cfg.commit_name == "day.ag deps bot"
    assert cfg.commit_email == "deps-bot@day.ag"


def test_the_owner_can_be_a_bare_owner_and_the_address_is_the_nostr_url():
    repo = DepsRepo("example-repo", OWNER, "master")
    assert repo.address == f"nostr://{OWNER}/example-repo"


@pytest.mark.parametrize("cfg, message", [
    ({}, "no `deps:` block"),
    ({"deps": []}, "deps must be a mapping"),
    ({"deps": {"repositories": []}}, "non-empty list"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "min_severity": "high"}},
     "min_severity must be a number"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "min_severity": 11}},
     "CVSS score"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "open_prs": "yes"}},
     "open_prs must be true or false"),
    ({"deps": {"repositories": [{"name": "../escape", "owner": OWNER}]}},
     "plain directory name"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER},
                                {"name": "one", "owner": OWNER}]}}, "must be unique"),
    ({"deps": {"repositories": [{"owner": OWNER}]}}, "name is required"),
    ({"deps": {"repositories": [{"name": "one"}]}}, "owner is required"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER}], "repozitories": []}},
     "unknown key"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER, "bracnh": "main"}]}},
     "unknown key"),
])
def test_a_bad_block_is_refused_by_name(cfg, message):
    with pytest.raises(DepsConfigError) as exc:
        load_deps_config(cfg)
    assert message in str(exc.value)


def test_the_example_config_documents_the_block_with_placeholders_only():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    cfg = load_deps_config(example)
    assert [repo.name for repo in cfg.repositories] == ["example-repo"]
    assert cfg.repositories[0].owner.startswith("<"), cfg.repositories[0].owner
    assert cfg.signer.startswith("<"), cfg.signer
    assert cfg.coordinator.startswith("<"), cfg.coordinator
    assert cfg.open_prs is True


# -- report helpers -----------------------------------------------------------

def report(path: Path, rows):
    import json

    data = {"results": [{"packages": [
        {"package": {"name": name, "version": version},
         "groups": [{"max_severity": score, "ids": [vid]}]}
        for name, version, score, vid in rows]}]}
    path.write_text(json.dumps(data))


def test_findings_respect_the_floor_and_count_distinct_rows(tmp_path):
    path = tmp_path / "report.json"
    report(path, [("left-pad", "1.0.0", 9.0, "GHSA-one"),
                  ("left-pad", "1.0.0", 7.5, "GHSA-two"),
                  ("left-pad", "1.0.0", 4.0, "GHSA-low")])
    assert findings(path, 7.0) == "2"
    assert findings(path, 8.0) == "1"
    assert findings(tmp_path / "missing.json", 7.0) == "?"


def test_cleared_advisories_names_what_the_bump_fixes(tmp_path):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    report(before, [("left-pad", "1.0.0", 9.0, "GHSA-one")])
    report(after, [])
    cleared = cleared_advisories(before, after, 7.0)
    assert cleared == ("GHSA-one (left-pad 1.0.0) "
                       "https://osv.dev/vulnerability/GHSA-one")


# -- the pipeline, against a real checkout and stub tools ---------------------

OSV = """#!/bin/sh
set -eu
case "$1" in
  scan)
    out=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "--output-file" ]; then out="$2"; fi
      shift
    done
    if [ -f .fixed ]; then
      printf '%s' '{"results": []}' > "$out"
    else
      printf '%s' '{"results":[{"packages":[{"package":{"name":"left-pad","version":"1.0.0"},"groups":[{"max_severity":"9.0","ids":["GHSA-test-0001"]}]}]}]}' > "$out"
    fi
    ;;
  fix)
    printf '\\n// bumped\\n' >> package-lock.json
    : > .fixed
    ;;
esac
exit 0
"""

QUIET = "#!/bin/sh\nexit 0\n"


@pytest.fixture()
def checkout(tmp_path):
    """A bare origin, a seed commit, and the bot's clone under its workspace."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (("osv-scanner", OSV), ("npm", QUIET), ("npx", QUIET), ("node", QUIET)):
        script = tools / name
        script.write_text(body)
        script.chmod(0o755)

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
    (seed / "package.json").write_text('{"name":"example","version":"1.0.0"}\n')
    (seed / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}\n')
    env = {"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@example"}
    subprocess.run(["git", "add", "."], cwd=seed, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=seed, check=True, env=env)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin, check=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "clone", "--quiet", str(origin), str(workspace / "example-repo")],
                   check=True)
    return tools, workspace, seed


def config(checkout, tmp_path, **overrides):
    tools, workspace, _seed = checkout
    raw = dict(
        repositories=(DepsRepo("example-repo", OWNER, "main"),),
        workspace=workspace,
        state_dir=tmp_path / "state",
        log=tmp_path / "deps.log",
        signer=SIGNER,
        coordinator=COORDINATOR,
    )
    raw.update(overrides)
    return DepsConfig(**raw)


def head(path: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def branches(path: Path) -> list[str]:
    done = subprocess.run(["git", "branch", "--list", "--format=%(refname:short)"],
                          cwd=path, check=True, capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def env_for(checkout) -> dict[str, str]:
    tools, _workspace, _seed = checkout
    return {"PATH": f"{tools}:/usr/bin:/bin"}


def test_a_dry_run_says_what_it_would_propose_and_writes_nothing(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    repo = cfg.workspace / "example-repo"
    before = head(repo)

    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, out=out, env=env_for(checkout)) == 0
    text = out.getvalue()

    assert "findings at or above 7: 1" in text
    assert "proposal changes 1 -> 0 findings" in text
    assert "gates passed" in text
    assert "dry run: would push pr/deps-" in text
    assert "GHSA-test-0001" in text
    # No write anywhere: the checkout is back at the origin tip, no proposal branch, no state.
    assert head(repo) == before
    assert branches(repo) == ["main", "work/deps"]
    assert not (tmp_path / "state").exists()


def test_a_dry_run_still_reports_a_proposal_it_would_not_repeat(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    repo = cfg.workspace / "example-repo"
    # The hash of the lockfile the stub scanner produces, recorded as the last proposal.
    import hashlib

    fixed = (repo / "package-lock.json").read_bytes() + b"\n// bumped\n"
    cfg.state_dir.mkdir(parents=True)
    (cfg.state_dir / "last-example-repo").write_text(hashlib.sha256(fixed).hexdigest() + "\n")

    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, out=out, env=env_for(checkout)) == 0
    assert "same proposal as the last run - not repeating it" in out.getvalue()
    assert "would push" not in out.getvalue()


def test_open_prs_false_scans_and_builds_but_publishes_nothing(checkout, tmp_path):
    cfg = config(checkout, tmp_path, open_prs=False)
    repo = cfg.workspace / "example-repo"
    before = head(repo)

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    text = out.getvalue()
    assert "deps.open_prs is false" in text
    assert "ready, not pushed" in text
    assert head(repo) == before
    assert not (tmp_path / "state").exists()


def test_an_unknown_repository_is_refused_by_name(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, only_repo="nope", out=out,
                    env=env_for(checkout)) == 2


# -- the CLI ------------------------------------------------------------------

def test_hng_deps_run_dry_run_reaches_the_pipeline(monkeypatch, tmp_path):
    import hermes_nostr_gateway.deps as deps_mod

    seen = {}

    def fake_run(cfg, *, dry_run=False, only_repo=None, out=None, env=None):
        seen.update(dry_run=dry_run, only_repo=only_repo, block=cfg)
        return 0

    monkeypatch.setattr(deps_mod, "run_deps", fake_run)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(block()))

    assert cli.main(["--config", str(path), "deps", "run", "--dry-run"]) == 0
    assert seen["dry_run"] is True
    assert seen["only_repo"] is None
    assert seen["block"].signer == SIGNER


def test_hng_deps_dry_run_is_the_same_thing(monkeypatch, tmp_path):
    import hermes_nostr_gateway.deps as deps_mod

    seen = {}
    monkeypatch.setattr(deps_mod, "run_deps",
                        lambda cfg, **kw: seen.update(kw) or 0)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(block()))

    assert cli.main(["--config", str(path), "deps", "dry-run", "--repo", "example-repo"]) == 0
    assert seen["dry_run"] is True
    assert seen["only_repo"] == "example-repo"


def test_a_bad_deps_block_is_a_config_error_not_a_traceback(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"deps": {"repositories": []}}))
    assert cli.main(["--config", str(path), "deps", "run"]) == 2
    assert "deps: deps.repositories must be a non-empty list" in capsys.readouterr().err
