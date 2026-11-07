"""The ngit watcher: an event wakes the pass, and the pass is still what decides.

Placeholders only: keys, channel ids, addresses and board slugs here are invented. No test
opens a socket or runs `ngit`: the plan is built from an injected repo lookup, the pass from
an injected runner, and the clock is a counter.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway import ngit_watch as watch
from hermes_nostr_gateway.ngit import DEFAULT_WATCH_DEBOUNCE_S, BridgeConfig
from hermes_nostr_gateway.ngit_watch import KIND_PR_UPDATE
from hermes_nostr_gateway.projects import ProjectMap

COMMUNITY = "ab" * 32
CHANNEL = "cd" * 32
OTHER_CHANNEL = "ce" * 32
OWNER = "ee" * 32
ADDRESS = f"30617:{OWNER}:example-repo"
OTHER_ADDRESS = f"30617:{OWNER}:other-repo"
CONVERSATION = f"concord:{COMMUNITY}:{CHANNEL}"
RELAY = "wss://relay.example"
HINTS = ("wss://repo.example", "wss://other.example")


# -- fixtures and doubles ----------------------------------------------------

def a_stream(*, channel_id: str = CHANNEL, name: str = "example",
             community: str = COMMUNITY):
    return SimpleNamespace(community_id=community, channel_id=channel_id, name=name,
                           chat_id=f"concord:{community}:{channel_id}")


class Clock:
    """A clock a test moves by hand: debounce arithmetic without sleeping."""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def a_repo(address: str = ADDRESS, relays: tuple[str, ...] = HINTS):
    return SimpleNamespace(address=address, relays=relays)


def event_id(seed: str) -> str:
    """A 64-hex event id with no meaning beyond being unique in one test."""
    return (seed * 64)[:64]


class Runner:
    """Stands in for `hng ngit sync`: records the channels it was asked to pass."""

    def __init__(self, *, gate: asyncio.Event | None = None, error: Exception | None = None):
        self.calls: list[str] = []
        self.gate = gate
        self.error = error

    async def __call__(self, target: watch.WatchTarget) -> watch.PassOutcome:
        self.calls.append(target.channel)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return watch.PassOutcome(exit_code=0, duration_s=0.01, output="posted: 1 message")


def a_project_cfg(path: Path, *, board: str = "example-board",
                  channel: str = CHANNEL, community: str = COMMUNITY) -> dict:
    return {"concord": {"projects": [{
        "channel": channel, "path": str(path), "name": "example",
        "project": "example", "board": board, "community": community}]}}


def a_plan(tmp_path: Path, *, ngit: dict | None = None, streams=None,
           repo=None, read_relays: tuple[str, ...] = (RELAY,), board: str = "example-board",
           channel: str = CHANNEL, roots: tuple[str, ...] = ()) -> watch.WatchPlan:
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    cfg = {**a_project_cfg(checkout, board=board, channel=channel),
           "ngit": {"assignee": "default", **(ngit or {})}}
    projects = ProjectMap.from_config(cfg["concord"])
    return watch.watch_plan(cfg, projects,
                            [a_stream()] if streams is None else streams,
                            read_relays=read_relays,
                            repo_ref=lambda _path: repo or a_repo(),
                            known_roots=lambda _address: roots)


def a_watcher(plan: watch.WatchPlan, *, runner=None, clock: Clock | None = None):
    clock = clock or Clock()
    return watch.NgitWatcher(plan, runner=runner or Runner(), now=clock), clock


def an_event(event_id: str = "e1" * 32, *, address: str = ADDRESS,
             kind: int = 1621) -> dict:
    return {"id": event_id, "pubkey": "11" * 32, "kind": kind, "created_at": 1000,
            "content": "body", "tags": [["a", address, "wss://relay.example"],
                                        ["subject", "Fix the thing"]]}


def a_comment(event_id: str = "c1" * 32, *, root: str, kind: int = 1111,
              root_kind: str = "1621") -> dict:
    """A published NIP-22 comment: root `E`/`K`/`P`, parent `e`/`k`/`p`, and no `a`."""
    return {"id": event_id, "pubkey": "22" * 32, "kind": kind, "created_at": 2000,
            "content": "a verdict", "tags": [
                ["E", root, "wss://relay.example"],
                ["K", root_kind],
                ["P", "33" * 32, "wss://relay.example"],
                ["e", root, "wss://relay.example"],
                ["k", root_kind],
                ["p", "33" * 32, "wss://relay.example"],
            ]}


# -- the plan: what is watched, and where ------------------------------------

def test_plan_maps_a_joined_channel_to_its_conversation(tmp_path):
    plan = a_plan(tmp_path, read_relays=("wss://read.example",))

    assert plan.enabled
    assert [target.channel for target in plan.targets] == [CONVERSATION]
    assert plan.targets[0].address == ADDRESS
    assert plan.targets[0].board == "example-board"
    # The repository's own relay hints first, then the daemon's read set.
    assert plan.relays == (*HINTS, "wss://read.example")
    assert plan.debounce_s == DEFAULT_WATCH_DEBOUNCE_S
    assert plan.reason == ""


def test_plan_prefers_an_explicit_ngit_relays(tmp_path):
    plan = a_plan(tmp_path, ngit={"relays": ["wss://chosen.example"]},
                  read_relays=("wss://read.example",))

    assert plan.relays == ("wss://chosen.example",)


def test_plan_caps_the_relay_list_at_eight(tmp_path):
    hints = tuple(f"wss://hint{i}.example" for i in range(10))
    plan = a_plan(tmp_path, repo=a_repo(relays=hints), read_relays=("wss://read.example",))

    assert len(plan.relays) == 8
    assert plan.relays == hints[:8]


def test_plan_is_off_when_watch_is_false(tmp_path):
    plan = a_plan(tmp_path, ngit={"watch": False})

    assert not plan.enabled
    assert plan.reason == "ngit.watch: false"


def test_plan_is_off_without_an_assignee(tmp_path):
    """A watcher whose every pass would exit 2 is not a watcher: say why, do not start."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = {**a_project_cfg(checkout), "ngit": {"watch": True}}
    plan = watch.watch_plan(cfg, ProjectMap.from_config(cfg["concord"]), [a_stream()],
                            read_relays=(RELAY,), repo_ref=lambda _path: a_repo())

    assert not plan.enabled
    assert plan.reason.startswith("ngit.assignee is not set")
    config = watch.NgitWatchConfig.from_config(cfg)
    assert config.explicit and not config.enabled


def test_watch_is_on_by_default_once_the_bridge_is_configured():
    assert watch.NgitWatchConfig.from_config(
        {"ngit": {"assignee": "default"}}).enabled
    # Not configured at all: no assignee means nothing for a pass to dispatch to.
    assert not watch.NgitWatchConfig.from_config({}).enabled
    assert not watch.NgitWatchConfig.from_config({"ngit": {}}).enabled


def test_a_nonsense_debounce_falls_back_to_the_default():
    settings = BridgeConfig.from_config({"assignee": "default",
                                         "watch_debounce_s": "within a minute"})

    assert settings.watch_debounce_s == DEFAULT_WATCH_DEBOUNCE_S
    assert BridgeConfig.from_config({"assignee": "default"}).watch is None


def test_plan_skips_a_checkout_that_is_not_a_nostr_repo(tmp_path):
    plan = a_plan(tmp_path, repo=a_repo(address=""))

    assert not plan.enabled
    assert "no NIP-34 address" in plan.skips[0]
    assert "not announced on Nostr" in plan.skips[0]


def test_plan_skips_a_channel_the_daemon_has_not_joined(tmp_path):
    plan = a_plan(tmp_path, streams=[])

    assert not plan.enabled
    assert "not joined" in plan.skips[0]


def test_plan_skips_a_mapping_with_no_board(tmp_path):
    """A checkout with no board is manual work: there is no queue to wake."""
    plan = a_plan(tmp_path, board="")

    assert not plan.enabled
    assert plan.skips == ()


def test_plan_watches_a_mapping_keyed_by_the_whole_conversation(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    cfg = {"concord": {"projects": [{"channel": CONVERSATION, "path": str(checkout),
                                     "project": "example", "board": "example-board"}]},
           "ngit": {"assignee": "default"}}
    plan = watch.watch_plan(cfg, ProjectMap.from_config(cfg["concord"]), [], read_relays=(RELAY,),
                            repo_ref=lambda _path: a_repo())

    assert plan.targets[0].channel == CONVERSATION


# -- coalescing --------------------------------------------------------------

async def test_a_burst_of_five_revisions_produces_one_pass(tmp_path):
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    for revision in range(5):
        watcher.handle_event(RELAY, an_event(event_id=event_id(f"r{revision}"),
                                             kind=KIND_PR_UPDATE))
        clock.advance(0.5)
    assert await watcher.tick() == 0, "nothing may run before the debounce is up"

    clock.advance(watcher.debounce_s)
    assert await watcher.tick() == 1
    await asyncio.sleep(0)

    assert runner.calls == [CONVERSATION]
    state = watcher.status()["channels"][0]
    assert state["events"] == 5, "five events, one pass -- and the count says so"
    assert state["passes"] == 1
    assert state["last_exit"] == 0
    assert state["due_in_s"] is None


async def test_the_same_event_from_three_relays_is_one_wake(tmp_path):
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    event = an_event()
    for _ in range(3):
        watcher.handle_event(RELAY, event)
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert watcher.events == 1
    assert runner.calls == [CONVERSATION]


async def test_an_event_for_another_repository_is_ignored(tmp_path):
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    watcher.handle_event(RELAY, an_event(address=OTHER_ADDRESS))
    watcher.handle_event(RELAY, {"id": "f" * 64, "kind": 1621, "tags": []})
    clock.advance(watcher.debounce_s * 2)

    assert await watcher.tick() == 0
    assert runner.calls == []


async def test_a_merge_and_an_issue_wake_the_channel(tmp_path):
    """The kinds the pass acts on that name their repository are the kinds that wake it."""
    for kind in (1631, 1618, 1619, 1621):
        assert kind in watch.WATCH_KINDS
    watcher, clock = a_watcher(a_plan(tmp_path))
    for kind in (1631, 1621):
        watcher.handle_event(RELAY, an_event(event_id=event_id(str(kind)), kind=kind))
    clock.advance(watcher.debounce_s)

    assert await watcher.tick() == 1


@pytest.mark.parametrize("root_kind", ["1621", "1618"])
async def test_a_comment_with_no_address_wakes_the_channel_by_its_root(tmp_path, root_kind):
    """ngit 3.0.5 publishes comments as NIP-22 1111, which carries `E` and no `a`.

    This is the acceptance case, for both an issue root (1621) and a PR root (1618): the
    comment names only the thing it replies to, and that id is what the watcher matches on.
    """
    root = event_id("ab")
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path, roots=(root,)), runner=runner)
    assert watch.KIND_COMMENT == 1111
    assert 1111 in watch.COMMENT_KINDS
    assert root_kind in watch.COMMENT_ROOT_KINDS

    # No `a` tag at all, exactly like event 43eef0cd on relay.ngit.dev.
    watcher.handle_event(RELAY, a_comment(root=root, root_kind=root_kind))
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert runner.calls == [CONVERSATION]


async def test_a_reply_to_a_comment_wakes_by_its_root_not_its_parent(tmp_path):
    """A reply's `e` is the comment it answers; its `E` is still the issue."""
    root, parent = event_id("ab"), event_id("cd")
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path, roots=(root,)), runner=runner)

    reply = a_comment(event_id=event_id("ef"), root=root)
    reply["tags"] = [["E", root], ["K", "1621"], ["P", "33" * 32],
                     ["e", parent], ["k", "1111"], ["p", "44" * 32]]
    watcher.handle_event(RELAY, reply)
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert runner.calls == [CONVERSATION]


async def test_a_comment_on_an_unknown_root_is_ignored(tmp_path):
    """Comments for the rest of the relay must not wake a pass for this channel."""
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path, roots=(event_id("ab"),)), runner=runner)

    watcher.handle_event(RELAY, a_comment(root=event_id("99"), event_id=event_id("c2")))
    clock.advance(watcher.debounce_s * 2)

    assert await watcher.tick() == 0
    assert runner.calls == []
    assert watcher.status()["roots"] == 1


async def test_an_issue_seen_live_teaches_its_root_so_its_comment_wakes(tmp_path):
    """An issue filed after the plan was built is in no link yet: the event teaches it."""
    root = event_id("ab")
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path, roots=()), runner=runner)

    watcher.handle_event(RELAY, an_event(event_id=root, kind=1621))
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    # A second comment on that issue, and no second `a`-carrying event to say so.
    watcher.handle_event(RELAY, a_comment(root=root, event_id=event_id("c3")))
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert runner.calls == [CONVERSATION, CONVERSATION]
    assert watcher.status()["roots"] == 1


def test_a_revision_or_merge_never_teaches_a_root(tmp_path):
    """Only an issue or a PR id can be a comment's `E`; a 1619/1631 id cannot."""
    watcher, _clock = a_watcher(a_plan(tmp_path, roots=()))

    for kind in (1619, 1631):
        watcher.handle_event(RELAY, an_event(event_id=event_id(str(kind)), kind=kind))

    assert watcher.status()["roots"] == 0


async def test_a_trickle_cannot_starve_the_wake(tmp_path):
    """A channel that keeps receiving events still gets a pass, by the ceiling."""
    runner = Runner()
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    seen = 0
    for _ in range(20):
        watcher.handle_event(RELAY, an_event(event_id=event_id(str(seen))))
        seen += 1
        clock.advance(20)          # faster than the 30s debounce, for ever
        await watcher.tick()
        await asyncio.sleep(0)
        if runner.calls:
            break

    # 20 events at 20s apart is 400s of trickle; the ceiling is 4 * 30s = 120s, so the pass
    # must have run by the seventh event rather than never.
    assert runner.calls, "the debounce ceiling never fired: the channel starved"
    assert seen <= 8, f"the wake waited for {seen} events (~{seen * 20}s), not the ceiling"


async def test_one_pass_per_channel_at_a_time(tmp_path):
    gate = asyncio.Event()
    runner = Runner(gate=gate)
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    watcher.handle_event(RELAY, an_event())
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)
    assert runner.calls == [CONVERSATION]
    assert watcher.status()["channels"][0]["running"] is True

    # A second event while the pass runs: the pass may not have read it, so one more pass --
    # after this one, never alongside it.
    watcher.handle_event(RELAY, an_event(event_id="e2" * 32))
    clock.advance(watcher.debounce_s * 3)
    assert await watcher.tick() == 0, "a pass ran while another one was in flight"

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert watcher.status()["channels"][0]["running"] is False

    clock.advance(watcher.debounce_s)
    assert await watcher.tick() == 1
    await asyncio.sleep(0)
    assert runner.calls == [CONVERSATION, CONVERSATION], "the mid-pass event was lost"


async def test_a_failed_pass_is_reported_and_the_next_wake_still_runs(tmp_path):
    runner = Runner(error=RuntimeError("ngit is not installed"))
    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)

    watcher.handle_event(RELAY, an_event())
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    state = watcher.status()["channels"][0]
    assert state["last_exit"] == -1
    assert "ngit is not installed" in state["last_error"]

    runner.error = None
    watcher.handle_event(RELAY, an_event(event_id="e3" * 32))
    clock.advance(watcher.debounce_s)
    assert await watcher.tick() == 1


async def test_a_channel_is_only_woken_by_its_own_repository(tmp_path):
    """Two mapped channels, two addresses: an event names one and wakes one."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    for path in (first, second):
        path.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "path": str(first), "project": "one", "board": "one-board",
         "community": COMMUNITY},
        {"channel": OTHER_CHANNEL, "path": str(second), "project": "two", "board": "two-board",
         "community": COMMUNITY},
    ]}, "ngit": {"assignee": "default"}}
    lookups = {str(first): a_repo(), str(second): a_repo(OTHER_ADDRESS)}
    plan = watch.watch_plan(cfg, ProjectMap.from_config(cfg["concord"]),
                            [a_stream(), a_stream(channel_id=OTHER_CHANNEL, name="other")],
                            read_relays=(RELAY,), repo_ref=lambda path: lookups[str(path)])
    runner = Runner()
    watcher, clock = a_watcher(plan, runner=runner)

    watcher.handle_event(RELAY, an_event())
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert runner.calls == [CONVERSATION]


# -- the pass ----------------------------------------------------------------

def test_the_pass_is_the_command_cron_runs(tmp_path):
    plan = a_plan(tmp_path)
    argv = watch.pass_command(plan.targets[0], config_path="/etc/hng.yaml",
                              socket_path="/run/hng.sock")

    assert argv == [sys.executable, "-m", "hermes_nostr_gateway.cli",
                    "--config", "/etc/hng.yaml",
                    "ngit", "sync", "--channel", CONVERSATION,
                    "--socket", "/run/hng.sock"]
    # Nothing about the event, the task or the notice: the pass reads the repository.
    assert not any("e1" in part for part in argv)


async def test_the_watcher_hands_the_pass_only_a_channel(tmp_path):
    """The wake is a channel, not a decision: no event id, no task, no notice."""
    seen: list[watch.WatchTarget] = []

    async def runner(target: watch.WatchTarget) -> watch.PassOutcome:
        seen.append(target)
        return watch.PassOutcome(exit_code=0, duration_s=0.0)

    watcher, clock = a_watcher(a_plan(tmp_path), runner=runner)
    watcher.handle_event(RELAY, an_event(event_id="ab" * 32, kind=KIND_PR_UPDATE))
    clock.advance(watcher.debounce_s)
    await watcher.tick()
    await asyncio.sleep(0)

    assert len(seen) == 1
    assert seen[0].channel == CONVERSATION
    assert seen[0].address == ADDRESS


async def test_status_says_what_it_is_watching_and_how_many_passes_it_woke(tmp_path):
    watcher, clock = a_watcher(a_plan(tmp_path))
    watcher.handle_event(RELAY, an_event())
    state = watcher.status()

    assert state["enabled"] is True
    assert state["debounce_s"] == DEFAULT_WATCH_DEBOUNCE_S
    assert state["relays"] == list(a_plan(tmp_path).relays)
    assert state["targets"][0]["channel"] == CONVERSATION
    assert state["channels"][0]["due_in_s"] == pytest.approx(watcher.debounce_s)


# -- the gateway wiring ------------------------------------------------------

def gateway_cfg(tmp_path: Path, *, ngit: dict | None = None) -> dict:
    secret = "11" * 32
    (tmp_path / "secret.json").write_text(f'{{"secret_key_hex": "{secret}"}}')
    return {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [RELAY], "publish": []},
        "store": str(tmp_path / "gw.db"),
        "groups": {}, "policies": [], "defaults": {},
        "concord": {"projects": [{"channel": CHANNEL, "path": str(tmp_path),
                                  "project": "example", "board": "example-board",
                                  "community": COMMUNITY}]},
        "ngit": ngit if ngit is not None else {"assignee": "default"},
    }


async def wait_until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


@pytest.fixture()
def quiet_gateway(monkeypatch):
    """A Gateway that starts without a network: no follows, no Marmot, no concord refresh."""
    monkeypatch.setattr(gateway_mod.Gateway, "_load_follows", lambda self: ())
    monkeypatch.setattr(gateway_mod.Gateway, "_setup_marmot", lambda self, _cfg: None)

    async def no_refresh(self) -> int:
        return 0

    monkeypatch.setattr(gateway_mod.Gateway, "refresh_concord", no_refresh)
    return monkeypatch


class RecordingWorker:
    """Enough of a PoolWorker to see what the gateway built, and to be stopped."""

    made: list["RecordingWorker"] = []

    def __init__(self, _pool, relay, _me, **kwargs):
        self.relay = relay
        self.kinds = tuple(kwargs.get("kinds") or ())
        self.a_tags = tuple(kwargs.get("a_tags") or ())
        self.root_kinds = tuple(kwargs.get("root_kinds") or ())
        self.purpose = kwargs.get("purpose", "dm")
        self.events, self.last_error = 0, None
        self.handler = None
        self._stop = asyncio.Event()
        RecordingWorker.made.append(self)

    async def run(self, handler) -> None:
        self.handler = handler
        await self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()


async def test_watch_false_starting_the_gateway_adds_no_subscription(tmp_path, quiet_gateway):
    quiet_gateway.setattr(gateway_mod, "PoolWorker", RecordingWorker)
    RecordingWorker.made.clear()

    gateway = gateway_mod.Gateway(gateway_cfg(tmp_path, ngit={"assignee": "default",
                                                             "watch": False}),
                                  socket_path=tmp_path / "gw.sock")
    await gateway.start()
    try:
        state = (await gateway.status())["ngit_watch"]
        assert gateway.ngit_watch is None
        assert state["enabled"] is False
        assert state["reason"] == "ngit.watch: false"
        assert [w for w in RecordingWorker.made if w.purpose == "ngit"] == []
    finally:
        await gateway.stop()


async def test_watch_on_subscribes_only_to_the_mapped_repositories(tmp_path, quiet_gateway):
    quiet_gateway.setattr(gateway_mod, "PoolWorker", RecordingWorker)
    RecordingWorker.made.clear()
    plan = watch.WatchPlan(targets=(watch.WatchTarget(
        address=ADDRESS, channel=CONVERSATION, repo=tmp_path, board="example-board",
        roots=("ab" * 32,)),),
        relays=(RELAY,), debounce_s=5.0)
    quiet_gateway.setattr(gateway_mod, "watch_plan", lambda *a, **k: plan)

    gateway = gateway_mod.Gateway(gateway_cfg(tmp_path), socket_path=tmp_path / "gw.sock")
    await gateway.start()
    try:
        await wait_until(lambda: gateway.ngit_watch is not None)
        await wait_until(lambda: [w for w in RecordingWorker.made if w.purpose == "ngit"])
        worker = next(w for w in RecordingWorker.made if w.purpose == "ngit")
        watcher = gateway.ngit_watch
        assert watcher is not None

        assert worker.kinds == watch.WATCH_KINDS
        assert worker.a_tags == (ADDRESS,)
        assert worker.handler == watcher.handle_event, \
            "a repository event must not be routed through the message planes"

        # Comments are a second subscription: a NIP-22 1111 has no `a`, so the `#a` filter
        # above can never match one, and the root kind is what has to narrow it instead.
        comments = next(w for w in RecordingWorker.made if w.purpose == "ngit-comments")
        assert comments.kinds == watch.COMMENT_KINDS == (1111,)
        assert comments.root_kinds == watch.COMMENT_ROOT_KINDS == ("1621", "1618")
        assert comments.a_tags == ()
        assert comments.handler == watcher.handle_event

        state = (await gateway.status())["ngit_watch"]
        assert state["enabled"] is True
        assert state["targets"][0]["channel"] == CONVERSATION
        assert state["roots"] == 1
        assert state["relays"] == [RELAY]
    finally:
        await gateway.stop()


def test_a_bad_debounce_is_refused_rather_than_becoming_a_negative_wait():
    with pytest.raises(watch.NgitWatchError):
        watch.NgitWatcher(watch.WatchPlan(targets=(), relays=(), debounce_s=0))
