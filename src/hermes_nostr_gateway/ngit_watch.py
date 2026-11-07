"""The ngit watcher: a relay event wakes the pass; the pass still decides.

Why the poll stays
------------------
The bridge's source of truth is the repository, not the relay. `ngit issue list --json` and
the checkout's refs are what say an issue is open or applied and a PR is still open or
merged; a kind-1621/1618 event says only that something was *proposed*. A revision arrives
as another event (1619), an issue can be resolved or reopened later, and a merge is a
kind-1631 that has to be matched against the branch. A subscription can therefore replace
the *wait*, never the *decision*.

So the ten-minute scheduled pass stays exactly what it was -- the floor: a restart, a relay
that did not deliver, a nudge lost while the daemon was down -- and this module only shortens
the wait. It subscribes to the kinds the bridge reads, for the mapped repositories' `#a`
addresses only (never the whole relay), coalesces a channel's events, and runs
`hng ngit sync --channel <conversation>`: the same command the timer runs, with the same store,
the same cursor and the same decisions.

Comments are a second filter, because they cannot be part of the first: `ngit issue comment`
and `ngit pr comment` publish NIP-22 kind 1111, which carries root-scope `E`/`K`/`P` and
parent `e`/`k`/`p` and **no `a` tag at all**. A `#a` subscription can therefore never match
one. So comments are asked for by root kind (`#K`: an issue or a PR) and attributed to a
repository here, by the root event id: the ids the pass has already linked are read from the
store when the plan is built, and an issue or PR event that arrives while we are listening
teaches its own id. A comment whose root nobody knows is ignored, and the scheduled pass
still reads it.

Two writers to one cursor is the failure this avoids. The watcher writes no store, no board and
no cursor and creates nothing: it reads the link table once, for the issue and PR ids a comment
can name, and otherwise only writes a wake for a channel and starts the pass. Everything the
pass does is idempotent (idempotency keys on tasks and notices, a link table over event
ids), so a spurious wake costs one poll -- and a lost nudge costs at most one scheduled
interval, because the timer is still there.

Where it listens
----------------
The relay rule is the pass's own (`ngit.relays`, else the repository announcement's relay
hints and `relays.read`, capped): listening somewhere the pass does not read would wake it
for nothing, and listening only on `relays.read` would miss the grasp servers a NIP-34
repository actually publishes to.

Delivery is at-least-once by design: the same event reaches us from every relay that
carries it, and a resubscribe replays a window. The debounce makes both harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

from .ngit import (
    DEFAULT_WATCH_DEBOUNCE_S,
    KIND_APPLIED,
    KIND_CLOSED,
    KIND_COMMENT,
    KIND_ISSUE,
    KIND_PR,
    BridgeConfig,
    Ngit,
)
from .projects import Project, ProjectMap

log = logging.getLogger("hng.ngit.watch")

#: A PR revision: the tip moved, so the review task's brief is stale until a pass reads it.
#: Not in the bridge's "kinds it announces" list, but it is exactly the event a burst of
#: five revisions arrives as, and a watcher that ignored it would leave revisions to cron.
KIND_PR_UPDATE = 1619

#: What a mapped repository's subscription asks for. Narrow on purpose: these kinds plus a
#: `#a` address, never a kind on its own. Status kinds other than `applied` and `closed`
#: (draft, reopened) change nothing the pass acts on, so they are not worth a wake.
WATCH_KINDS = (KIND_ISSUE, KIND_PR, KIND_PR_UPDATE, KIND_APPLIED, KIND_CLOSED)

#: Comments are the one thing the `#a` filter cannot see. ngit 3.0.5 publishes every comment
#: as NIP-22 kind 1111 -- root-scope `E`/`K`/`P`, parent `e`/`k`/`p`, no `a` -- so a comment
#: gets its own subscription, and the watcher attributes it to a repository by its root id.
COMMENT_KINDS = (KIND_COMMENT,)

#: The root kinds a comment can hang off: an issue or a PR. `#K` is the root scope, so it
#: holds for a reply to a comment (`k` = 1111) as well as for a top-level one.
COMMENT_ROOT_KINDS = (str(KIND_ISSUE), str(KIND_PR))

#: The kinds whose own event id is a comment's root. A revision (1619) names the PR and a
#: merge (1631) names the PR's commit, so neither is one.
ROOT_KINDS = (KIND_ISSUE, KIND_PR)

#: A wake is delayed by at most this many debounce intervals: a channel that keeps trickling
#: events must still get a pass, or a busy repository would starve its own watcher.
MAX_COALESCE_FACTOR = 4

#: A pass that has not finished by now is not going to, and the child must not outlive it.
PASS_TIMEOUT_S = 600.0

#: How far back a fresh subscription reads. A resubscribe replays this window, which costs at
#: most one coalesced pass per channel -- the same catch-up cron would do, sooner. Without a
#: window every reconnect would ask for the relay's whole history.
WATCH_SINCE_DAYS = 1

#: Event ids remembered to keep the "events coalesced" count honest when the same event
#: arrives from several relays. Bounded: this is a counter's evidence, not a history.
SEEN_LIMIT = 512


class NgitWatchError(RuntimeError):
    """The watcher cannot be built; the message says what configuration is missing."""


# -- what is watched ---------------------------------------------------------

@dataclass(frozen=True)
class WatchTarget:
    """One mapped repository, and the channel whose pass a wake belongs to."""

    address: str        # `30617:<pubkey>:<identifier>`, the `#a` the events carry
    channel: str        # `concord:<community>:<channel>`, what `--channel` takes
    repo: Path
    board: str
    #: The issue and PR event ids this repository's pass has already linked. A comment
    #: names only its root, so these are how one is attributed to a repository.
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchPlan:
    """What to listen for and where, or why there is nothing to listen for."""

    targets: tuple[WatchTarget, ...] = ()
    relays: tuple[str, ...] = ()
    debounce_s: float = DEFAULT_WATCH_DEBOUNCE_S
    #: Why the watcher is not watching. Empty when it is.
    reason: str = ""
    #: Mapped channels it cannot watch, one line each, for the log.
    skips: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.targets and self.relays)


@dataclass(frozen=True)
class NgitWatchConfig:
    """`ngit.watch` decided once, before anything is subscribed to."""

    enabled: bool = False
    debounce_s: float = DEFAULT_WATCH_DEBOUNCE_S
    #: The operator wrote `ngit.watch` in the config (as opposed to leaving the default).
    explicit: bool = False
    reason: str = ""

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "NgitWatchConfig":
        """On by default once the bridge is configured; off for every reason that isn't that.

        "Configured" is `ngit.assignee`: without it `hng ngit sync` refuses to run, so a
        watcher would only start passes that exit 2. An explicit `watch: true` without it is
        reported as a configuration problem (the caller warns), never silently attempted.
        """
        block = (cfg or {}).get("ngit") if isinstance(cfg, dict) else None
        settings = BridgeConfig.from_config(block)
        reason = ""
        if settings.watch is False:
            reason = "ngit.watch: false"
        elif not settings.assignee:
            reason = ("ngit.assignee is not set, so `hng ngit sync` would refuse every run:"
                      " set it to the profile a ready task is dispatched to")
        elif not (settings.issues or settings.prs):
            reason = "ngit.issues and ngit.prs are both false: there is nothing to wake"
        return cls(enabled=not reason, debounce_s=settings.watch_debounce_s,
                   explicit=settings.watch is not None, reason=reason)


def conversation_for(project: Project, streams: Iterable[Any]) -> tuple[str, str]:
    """The conversation string the pass takes, and why there is none.

    An entry that spells the conversation out (`channel: concord:<community>:<channel>`) is
    used as written: that is the form a cron job uses, and the pass resolves it through the
    same mapping. An entry that names a channel by id or name needs a joined stream to turn
    it into a conversation -- and that stream is also the evidence that a notice can be
    posted into it at all, since a wake that cannot reach the channel is half a wake.
    """
    text = str(project.channel or "").strip()
    if text.lower().startswith("concord:"):
        return text, ""
    wanted = text.lower()
    community = str(project.community or "").strip().lower()
    in_scope = [stream for stream in streams
                if not community or str(stream.community_id).lower() == community]
    matches = [stream for stream in in_scope if str(stream.channel_id).lower() == wanted]
    # An id match beats a name match, exactly like `ProjectMap.resolve`: a renamed channel
    # must not steal the account of a channel that happens to have the old name.
    if not matches:
        matches = [stream for stream in in_scope if str(stream.name).lower() == wanted]
    if len(matches) == 1:
        return str(matches[0].chat_id), ""
    if not matches:
        return "", f"channel {text!r} is not joined (no concord stream names it)"
    return "", f"{len(matches)} joined channels match {text!r}: use its id"


def _repo_ref(path: Path) -> Any:
    """The checkout's NIP-34 address and relay hints, read where cron reads them."""
    return Ngit(path).repository()


def _no_known_roots(_address: str) -> Sequence[str]:
    """No link table to ask (a CLI, a test): a comment wakes only once its root is seen."""
    return ()


def _roots_of(known_roots: Callable[[str], Sequence[str]], address: str) -> tuple[str, ...]:
    """The root ids the pass knows for this repository, or none if the lookup fails."""
    try:
        return tuple(dict.fromkeys(str(root) for root in known_roots(address) or () if root))
    except Exception as exc:  # noqa: BLE001 - a broken lookup must not stop the watcher
        log.warning("ngit watch: cannot read known roots for %s: %s", address, exc)
        return ()


def watch_plan(cfg: dict[str, Any], projects: ProjectMap, streams: Sequence[Any], *,
               read_relays: Sequence[str] = (),
               repo_ref: Callable[[Path], Any] = _repo_ref,
               known_roots: Callable[[str], Sequence[str]] = _no_known_roots) -> WatchPlan:
    """Which repositories to listen for, on which relays, or why none.

    Blocking on purpose (`ngit repo --json` is a child process per checkout), so the daemon
    builds this in a thread. Every mapping that declares a board is a candidate: a channel
    with no board is manual work, and nothing about it would be woken.

    `known_roots` answers an address's issue and PR event ids -- the ids a NIP-22 comment
    can name as its root. It is a read of the pass's own link table, injected so a test
    does not need a store; the watcher writes nothing back.
    """
    settings = BridgeConfig.from_config((cfg or {}).get("ngit"))
    config = NgitWatchConfig.from_config(cfg)
    if not config.enabled:
        return WatchPlan(debounce_s=config.debounce_s, reason=config.reason)

    targets: list[WatchTarget] = []
    skips: list[str] = []
    hints: list[str] = []
    for project in projects.all():
        if not project.ok:
            skips.append(f"{project.channel}: {project.problem or project.config_error}")
            continue
        if not project.board:
            continue  # no board: a checkout for manual work, not a queue to wake
        channel, why = conversation_for(project, streams)
        if not channel:
            skips.append(f"{project.channel}: {why}")
            continue
        try:
            repository = repo_ref(project.path)
        except Exception as exc:  # noqa: BLE001 - one checkout must not stop the rest
            skips.append(f"{project.channel}: cannot read {project.path}: {exc}")
            continue
        if not getattr(repository, "address", ""):
            skips.append(f"{project.channel}: {project.path} is not announced on Nostr"
                        " (no NIP-34 address): PRs and issues are not watched here")
            continue
        targets.append(WatchTarget(address=repository.address, channel=channel,
                                   repo=project.path, board=project.board,
                                   roots=_roots_of(known_roots, repository.address)))
        hints.extend(getattr(repository, "relays", ()) or ())

    if not targets:
        return WatchPlan(debounce_s=config.debounce_s, skips=tuple(skips),
                         reason="no mapped channel has a checkout announced on Nostr")

    # The pass's own rule: an explicit `ngit.relays` is the operator's choice; otherwise the
    # repositories' announcement hints and then the daemon's read set, capped. Listening
    # anywhere else would wake the pass for events it does not read.
    if settings.relays:
        relays = tuple(settings.relays)
    else:
        relays = tuple(dict.fromkeys((*hints, *read_relays)))[:8]
    if not relays:
        return WatchPlan(debounce_s=config.debounce_s, skips=tuple(skips),
                         reason="no relays to listen on: set ngit.relays or relays.read")
    return WatchPlan(targets=tuple(targets), relays=relays,
                     debounce_s=config.debounce_s, skips=tuple(skips))


# -- one channel's pending wake ----------------------------------------------

@dataclass
class Wake:
    """A channel's coalesced events, and the pass that answers them."""

    channel: str
    #: When the current burst started (for the ceiling) and when it is due; None = idle.
    first_at: Optional[float] = None
    due_at: Optional[float] = None
    events: int = 0
    running: bool = False
    #: An event arrived while the pass was running: the pass may not have read it.
    again: bool = False
    passes: int = 0
    last_pass_at: Optional[float] = None
    last_duration_s: Optional[float] = None
    last_exit: Optional[int] = None
    last_error: str = ""

    def as_dict(self, *, now: float) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "events": self.events,
            "passes": self.passes,
            "running": self.running,
            "due_in_s": (round(self.due_at - now, 1)
                         if self.due_at is not None and not self.running else None),
            "last_pass_at": self.last_pass_at,
            "last_duration_s": self.last_duration_s,
            "last_exit": self.last_exit,
            "last_error": self.last_error,
        }


# -- running the pass --------------------------------------------------------

@dataclass(frozen=True)
class PassOutcome:
    """What one `hng ngit sync` child did, in a shape the watcher can report."""

    exit_code: int
    duration_s: float
    #: The pass's own last line of output: what it wrote is the report.
    output: str = ""
    error: str = ""


def pass_command(target: WatchTarget, *, config_path: str | Path | None = None,
                 socket_path: str | Path | None = None,
                 python: str | None = None) -> list[str]:
    """The command cron runs, spelled the same way.

    `--config` first because it belongs to `hng` itself; `--socket` after the subcommand
    because that is the parser it was declared on. The interpreter is this process's, so a
    daemon in a virtualenv runs the pass in the same one instead of whatever `hng` PATH
    would find.
    """
    argv = [python or sys.executable, "-m", "hermes_nostr_gateway.cli"]
    if config_path:
        argv += ["--config", str(config_path)]
    argv += ["ngit", "sync", "--channel", target.channel]
    if socket_path:
        argv += ["--socket", str(socket_path)]
    return argv


class SubprocessPass:
    """Run the pass as a child: a separate process, so a slow ngit cannot stall the daemon.

    The pass is deliberately not reimplemented here. It owns the store, the board and the
    cursor, and it is the thing cron runs; a second in-process copy would be a second writer
    and a second set of bugs. `hng ngit sync` is already safe to run alongside the daemon
    (cron does it every ten minutes), which is what makes this a small change.
    """

    def __init__(self, *, config_path: str | Path | None = None,
                 socket_path: str | Path | None = None,
                 timeout: float = PASS_TIMEOUT_S):
        self.config_path = config_path
        self.socket_path = socket_path
        self.timeout = float(timeout)

    def command(self, target: WatchTarget) -> list[str]:
        return pass_command(target, config_path=self.config_path,
                            socket_path=self.socket_path)

    async def __call__(self, target: WatchTarget) -> PassOutcome:
        argv = self.command(target)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(target.repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as exc:
            return PassOutcome(exit_code=-1, duration_s=0.0,
                               error=f"cannot start {argv[0]}: {exc}"[:300])
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await _kill(proc)
            return PassOutcome(exit_code=-1, duration_s=time.monotonic() - started,
                               error=f"the pass did not finish in {self.timeout:.0f}s")
        except asyncio.CancelledError:
            # Shutdown: the child must not outlive the daemon that started it.
            await _kill(proc)
            raise
        duration = time.monotonic() - started
        stdout = (out or b"").decode("utf-8", errors="replace").strip()
        stderr = (err or b"").decode("utf-8", errors="replace").strip()
        code = int(proc.returncode or 0)
        return PassOutcome(exit_code=code, duration_s=duration, output=stdout,
                           error="" if code == 0 else _last_line(stderr or stdout))


async def _kill(proc: Any) -> None:
    """Kill a child and reap it, even while the caller is being cancelled."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(proc.wait(), 5)


def _last_line(text: str, limit: int = 300) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return (lines[-1] if lines else "")[:limit]


PassRunner = Callable[[WatchTarget], Awaitable[PassOutcome]]


class NgitWatcher:
    """One wake per channel, one pass at a time per channel, and no decisions of its own.

    The watcher is deliberately small: it maps an event to the channels whose repositories
    it names, coalesces, and starts the pass. It never opens the store or the board, never
    writes a cursor and never posts: `Wake` is its whole state, and `status()` is the
    evidence that the accelerator is working rather than a second source of truth.
    """

    def __init__(self, plan: WatchPlan, *, runner: PassRunner | None = None,
                 now: Callable[[], float] = time.time):
        if plan.debounce_s <= 0:
            raise NgitWatchError("the debounce must be positive")
        self.plan = plan
        self.debounce_s = float(plan.debounce_s)
        self._runner: PassRunner = runner or SubprocessPass()
        self._now = now
        self._wakes: dict[str, Wake] = {
            target.channel: Wake(channel=target.channel) for target in plan.targets}
        self._targets: dict[str, WatchTarget] = {
            target.channel: target for target in plan.targets}
        self._channels: dict[str, tuple[str, ...]] = {}
        for target in plan.targets:
            self._channels[target.address] = (*self._channels.get(target.address, ()),
                                              target.channel)
        #: Comment root id -> repository address. Seeded from the link table the plan read,
        #: then extended by every issue and PR event that arrives: a comment names only its
        #: root, so this is the whole of what attributes one to a repository.
        self._roots: dict[str, str] = {}
        for target in plan.targets:
            for root in target.roots:
                self._roots.setdefault(str(root), target.address)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._workers: list[Any] = []
        self.events = 0
        self.passes = 0
        self.started_at: float | None = None

    # -- the event -------------------------------------------------------

    def handle_event(self, relay: str, event: dict[str, Any]) -> None:
        """The pool's handler: a wake for the channel whose repository this names.

        Nothing here reads the event's content, author or status. That is the point: the
        event is evidence that *something* changed, and the pass is what decides what.

        Two ways to name a repository, because the events are shaped differently. An issue,
        PR, revision or merge carries `a` and is matched on it. A NIP-22 comment carries no
        `a` at all -- only its root (`E`), which is the issue or PR it replies to -- so it
        is matched against the ids this watcher has learned.
        """
        addresses = _repositories_of(event)
        if addresses:
            self._learn_root(event, addresses)
        else:
            address = self._roots.get(_root_of(event), "")
            addresses = (address,) if address else ()
        if not addresses:
            return
        channels = tuple(dict.fromkeys(
            channel for address in addresses for channel in self._channels.get(address, ())))
        if not channels:
            return
        event_id = str(event.get("id") or "")
        if event_id and event_id in self._seen:
            return  # the same event from another relay, or a replayed window
        if event_id:
            self._seen[event_id] = None
            while len(self._seen) > SEEN_LIMIT:
                self._seen.popitem(last=False)
        self.events += 1
        for channel in channels:
            self.wake(channel)

    def _learn_root(self, event: dict[str, Any], addresses: tuple[str, ...]) -> None:
        """Remember an issue's or PR's id, so a comment on it can be attributed later.

        The plan seeds this from the link table; this is the part that arrives after the
        plan was built -- an issue filed a minute ago, whose first comment is in no link
        yet. Only the kinds whose own id is a root: a revision names its PR, a merge names
        the commit, and neither can be a comment's `E`.
        """
        if int(event.get("kind") or 0) not in ROOT_KINDS:
            return
        event_id = str(event.get("id") or "")
        if not event_id or event_id in self._roots:
            return
        # Only an address this watcher maps: a multi-maintainer announcement names several,
        # and remembering one that has no channel would attribute the next comment to a
        # repository with no pass to wake.
        for address in addresses:
            if address in self._channels:
                self._roots[event_id] = address

    def wake(self, channel: str) -> None:
        """Coalesce one wake for a channel, delaying it by the debounce.

        Trailing edge, so a burst -- five revisions, an issue and its first comment -- is
        one pass rather than five. The ceiling matters as much: a channel with a steady
        trickle must not keep pushing its own pass out for ever.
        """
        now = self._now()
        wake = self._wakes.get(channel)
        if wake is None:
            if channel not in self._targets:
                return  # not a channel this watcher maps: there is no pass to run
            wake = Wake(channel=channel)
            self._wakes[channel] = wake
        wake.events += 1
        if wake.running:
            # The pass that is running may have read the repository before this event
            # landed, so the next one is needed -- after it finishes, not during.
            wake.again = True
            return
        if wake.first_at is None:
            wake.first_at = now
        ceiling = wake.first_at + self.debounce_s * MAX_COALESCE_FACTOR
        wake.due_at = min(now + self.debounce_s, ceiling)

    # -- the loop --------------------------------------------------------

    def due(self) -> list[Wake]:
        now = self._now()
        return [wake for wake in self._wakes.values()
                if wake.due_at is not None and not wake.running and wake.due_at <= now]

    async def tick(self) -> int:
        """Start a pass for every channel that is due and not already running."""
        started = 0
        for wake in self.due():
            self._launch(wake)
            started += 1
        return started

    def _launch(self, wake: Wake) -> None:
        target = self._targets.get(wake.channel)
        if target is None:
            return
        wake.running = True
        task = asyncio.create_task(self._pass(wake, target))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pass(self, wake: Wake, target: WatchTarget) -> None:
        started = self._now()
        try:
            outcome = await self._runner(target)
        except asyncio.CancelledError:
            wake.last_error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - one failed pass must not end the watcher
            wake.last_exit = -1
            wake.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("ngit watch: pass for %s failed", wake.channel)
        else:
            wake.last_exit = outcome.exit_code
            wake.last_error = outcome.error
            if outcome.exit_code == 0:
                log.info("ngit watch: pass for %s finished in %.1fs%s", wake.channel,
                         outcome.duration_s,
                         f": {_last_line(outcome.output, 200)}" if outcome.output else "")
            else:
                # the scheduled pass still runs: a pass that cannot read its checkout is a
                # problem to report, not a reason to stop waking the next one.
                log.warning("ngit watch: pass for %s exited %d: %s", wake.channel,
                            outcome.exit_code, outcome.error or "no output")
        finally:
            wake.running = False
            wake.passes += 1
            wake.last_pass_at = self._now()
            wake.last_duration_s = round(wake.last_pass_at - started, 2)
            self.passes += 1
            if wake.again:
                # An event landed while the pass ran: one more, after the debounce, so the
                # burst that arrives during a pass is still one pass.
                wake.again = False
                wake.first_at = self._now()
                wake.due_at = wake.first_at + self.debounce_s
            else:
                wake.first_at = None
                wake.due_at = None

    async def run(self, attach: Callable[[tuple[str, ...], Any], Any]) -> None:
        """Subscribe for the plan's addresses, then wake passes until stopped.

        `attach(addresses, handler)` is the daemon's: it builds the read workers for those
        `#a` addresses, and for comments by root kind, and hands every event back here.
        Kept as a callback so the watcher itself knows nothing about the pool, NIP-42 or
        reconnects.
        """
        addresses = tuple(sorted({target.address for target in self.plan.targets}))
        self.started_at = self._now()
        self._workers = list(attach(addresses, self.handle_event) or ())
        log.info("ngit watch: listening for %d repository(ies) on %d relay(s): %s",
                 len(addresses), len(self.plan.relays), ", ".join(self.plan.relays))
        try:
            while not self._stop.is_set():
                await self.tick()
                timeout = self._pause()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=timeout)
        finally:
            await self.aclose()

    def _pause(self) -> float:
        """How long to wait before looking for due wakes again: at most a second.

        A second is cheap and keeps a clock that moves under us (a test, a suspend/resume)
        from being ignored, while the loop still sleeps most of a debounce interval.
        """
        now = self._now()
        waits = [max(0.0, wake.due_at - now) for wake in self._wakes.values()
                 if wake.due_at is not None and not wake.running]
        # A floor, not just a cap: a wake that is due but could not be launched must not
        # turn this loop into a spin.
        return max(0.05, min([1.0, *waits])) if waits else 1.0

    async def aclose(self) -> None:
        """Stop waking, and make sure no pass child outlives us."""
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*self._tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    # -- evidence --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self._now()
        return {
            "enabled": True,
            "debounce_s": self.debounce_s,
            "relays": list(self.plan.relays),
            "targets": [{"channel": target.channel, "address": target.address,
                         "repo": str(target.repo), "board": target.board}
                        for target in self.plan.targets],
            "channels": [wake.as_dict(now=now) for wake in self._wakes.values()],
            "events": self.events,
            "passes": self.passes,
            #: How many issue and PR ids a comment can be attributed to. A comment whose
            #: root is not here is ignored, so this is the evidence the fix is working.
            "roots": len(self._roots),
            "alive": any(getattr(worker, "alive", False) for worker in self._workers),
            "workers": [str(getattr(worker, "subscription_id", ""))
                        for worker in self._workers],
            "started_at": self.started_at,
        }


def _repositories_of(event: dict[str, Any]) -> tuple[str, ...]:
    """Every NIP-34 repository address an event names with an `a` tag, in tag order.

    Kinds 1621, 1618, 1619 and 1631 all carry `a: 30617:<pubkey>:<identifier>`;
    1631's is optional in NIP-34 and ngit publishes it, which is what lets a merge wake
    the channel. A repository with several maintainers is announced under one address per
    maintainer and ngit tags all of them, so an event matches if *any* `a` names a mapped
    repository -- reading only the first would depend on tag order. The relay hint a tag
    may carry as a third element is not part of the match.

    Empty means "this event names no repository", which is the normal answer for a NIP-22
    comment: it is scoped by its root event (`E`) instead, see `_root_of`.
    """
    out: list[str] = []
    for tag in event.get("tags") or []:
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == "a":
            value = str(tag[1] or "").strip()
            if value.startswith("30617:") and value not in out:
                out.append(value)
    return tuple(out)


def _root_of(event: dict[str, Any]) -> str:
    """The event a NIP-22 comment hangs off: its root `E`, else the parent it replies to.

    NIP-22 scopes a comment with `E`/`K`/`P` (the thread's root) and `e`/`k`/`p` (what it
    replies to). For a top-level comment both name the issue or PR; for a reply to another
    comment only `E` does, so it is read first. Empty for an event with neither.
    """
    for name in ("E", "e"):
        for tag in event.get("tags") or []:
            if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == name:
                value = str(tag[1] or "").strip()
                if value:
                    return value
    return ""
