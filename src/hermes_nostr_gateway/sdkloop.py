"""Bind the bindings' event loop deliberately: it is process-global, and it leaks.

`nostr-sdk`'s uniffi bindings resolve every async call's future through one module
global, `_UNIFFI_GLOBAL_EVENT_LOOP`, whenever it is set. That is deliberate — the Rust
side calls our async signer from a worker thread, where there is no running loop — but
it also means the *last* caller to set it owns it for the whole process.

`asyncio.run` in a worker thread (a cold `fetch`, a one-shot `publish`) set that global
to its own loop and then closed it. The daemon's long-lived notification pump kept
running on the main loop but built every new future on the dead one: one
`got Future attached to a different loop`, plus an `Event loop is closed` from the
continuation callback, per second, per relay — until every relay worker was dead and
`hng health` read `0/8 relay(s) ok` with nothing wrong at any relay.

Two rules keep that from happening again:

- A one-shot loop puts back the binding it found when it finishes (`run_isolated`).
- A long-lived path re-asserts its own loop before every await (`ensure`), so a leak
  from somewhere else costs one call instead of the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Iterator, TypeVar

import nostr_sdk as sdk

#: The generated bindings module; `import nostr_sdk` re-exports it as an attribute.
_BINDINGS = getattr(sdk, "nostr_sdk", sdk)

T = TypeVar("T")

#: The daemon's own loop, declared once at pump start. One-shot loops hand the binding back
#: to this rather than to whatever they found, because "whatever they found" can be another
#: one-shot's loop that is already closing.
_HOME: Any = None


def _set(loop: Any) -> None:
    """Point the bindings at `loop` (None means 'no global: use the running loop')."""
    setter = getattr(sdk, "uniffi_set_event_loop", None)
    if setter is not None:
        setter(loop)


def current() -> Any:
    """The loop the bindings will run their callbacks on, or None when unbound."""
    return getattr(_BINDINGS, "_UNIFFI_GLOBAL_EVENT_LOOP", None)


def bind(loop: asyncio.AbstractEventLoop | None = None) -> Any:
    """Bind the running loop (or `loop`); return the binding this replaces."""
    previous = current()
    _set(loop if loop is not None else asyncio.get_running_loop())
    return previous


def restore(previous: Any) -> None:
    """Put back a binding from `bind`, falling back to the daemon's loop.

    A one-shot loop must not leave the process unbound: the bindings resolve futures on the
    global from a Rust worker thread, where there is no running loop to fall back to, so
    restoring `None` turns into "no running event loop" on the next NIP-42 attempt — and
    that is permanent, because every following attempt inherits the same `None`. Prefer the
    home loop whenever the saved binding is gone or closed.
    """
    if previous is not None and previous.is_closed():
        previous = None
    if previous is None:
        home = _HOME
        previous = home if home is not None and not home.is_closed() else None
    _set(previous)


def set_home(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Declare the daemon's loop as the binding and the fallback for one-shot restores."""
    global _HOME
    _HOME = loop if loop is not None else asyncio.get_running_loop()
    _set(_HOME)


@contextlib.contextmanager
def bound(loop: asyncio.AbstractEventLoop | None = None) -> Iterator[None]:
    """Hold the binding for the block, then put back whatever was there before."""
    previous = bind(loop)
    try:
        yield
    finally:
        restore(previous)


def ensure(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Re-assert the running loop before an await, whatever a one-shot path left."""
    running = loop if loop is not None else asyncio.get_running_loop()
    if current() is not running:
        _set(running)


def run_isolated(coro: Awaitable[T]) -> T:
    """`asyncio.run` for a one-shot loop that must not keep the binding when it closes.

    Both caller shapes matter: a plain synchronous caller, and a caller inside a running
    loop that is handed a worker thread. Each gets a loop of its own, and the global is
    left as it was found — the daemon's main loop, usually.
    """
    previous = current()
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    finally:
        restore(previous)
