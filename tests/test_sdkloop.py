"""The bindings' event loop is process-global, so a one-shot loop must not keep it.

The daemon runs one long-lived loop with one notification pump on it. Cold reads and
one-shot publishes run `asyncio.run` in a worker thread, and each of those loops used to
leave uniffi's `_UNIFFI_GLOBAL_EVENT_LOOP` pointing at itself after it closed: every
later future was built on a dead loop, so the pump died with `got Future attached to a
different loop` once a second for every relay. These tests pin both halves of the fix.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import sdkloop


@pytest.fixture(autouse=True)
def _clean_binding():
    """Every test starts and ends with no global binding: one process, one global."""
    sdkloop._HOME = None
    sdkloop.restore(None)
    yield
    sdkloop._HOME = None
    sdkloop.restore(None)


def in_a_thread(fn: Callable[[], Any]) -> Any:
    """Run `fn` on its own thread and hand back its result (or raise its error)."""
    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            result["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "the worker thread did not finish"
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_bind_returns_the_binding_it_replaced():
    async def main() -> None:
        loop = asyncio.get_running_loop()
        assert sdkloop.bind(loop) is None
        assert sdkloop.current() is loop
        sdkloop.restore(None)
        assert sdkloop.current() is None

    asyncio.run(main())


def test_a_one_shot_loop_does_not_keep_the_binding_after_it_closes():
    """`run_isolated` is the shape `fetch_*_blocking` and `publish` use."""

    async def one_shot() -> asyncio.AbstractEventLoop:
        # The daemon's binding is inherited, so the one-shot has to bind its own.
        assert sdkloop.current() is not asyncio.get_running_loop()
        sdkloop.bind()
        return asyncio.get_running_loop()

    async def main() -> None:
        mine = asyncio.get_running_loop()
        sdkloop.bind(mine)

        theirs = await asyncio.to_thread(sdkloop.run_isolated, one_shot())

        assert theirs.is_closed(), "asyncio.run closes its loop"
        assert sdkloop.current() is mine, "the daemon's binding came back"
        assert not sdkloop.current().is_closed()

    asyncio.run(main())


def test_a_leaked_binding_on_a_closed_loop_is_replaced_by_ensure():
    """The belt-and-braces half: something else bound a loop and closed it."""

    def leak() -> None:
        async def inner() -> None:
            sdkloop.bind()  # a module that never restores, e.g. the old fetch._client

        asyncio.run(inner())

    async def main() -> None:
        mine = asyncio.get_running_loop()
        await asyncio.to_thread(leak)

        leaked = sdkloop.current()
        assert leaked is not None and leaked.is_closed()

        sdkloop.ensure()

        assert sdkloop.current() is mine

    asyncio.run(main())


def test_ensure_does_not_rebind_a_loop_that_is_already_right(monkeypatch):
    calls: list[Any] = []
    real = sdk.uniffi_set_event_loop

    def spy(loop):
        calls.append(loop)
        real(loop)

    monkeypatch.setattr(sdk, "uniffi_set_event_loop", spy)

    async def main() -> None:
        loop = asyncio.get_running_loop()
        sdkloop.bind(loop)
        calls.clear()

        sdkloop.ensure()

        assert calls == [], "re-binding the same loop is not free: it is a list append"
        assert sdkloop.current() is loop

    asyncio.run(main())


def test_run_isolated_is_a_plain_asyncio_run_outside_a_loop():
    async def answer() -> int:
        return 42

    assert sdkloop.run_isolated(answer()) == 42
    assert sdkloop.current() is None


def test_bound_puts_the_binding_back_when_the_block_raises():
    async def main() -> None:
        loop = asyncio.get_running_loop()
        previous = sdkloop.bind(loop)

        with pytest.raises(RuntimeError):
            with sdkloop.bound():
                raise RuntimeError("boom")

        assert sdkloop.current() is loop
        sdkloop.restore(previous)

    asyncio.run(main())


def test_a_one_shot_restore_prefers_the_home_loop_over_unbinding():
    """Restoring a binding that is gone must not leave the process unbound.

    uniffi calls our signer from a Rust worker thread, where there is no running loop, so a
    `None` global is not "use the running loop" — it is "no running event loop", and every
    later NIP-42 attempt fails the same way. A one-shot that entered with nothing bound must
    therefore hand the binding back to the daemon's loop.
    """

    async def home() -> None:
        loop = asyncio.get_running_loop()
        sdkloop.set_home(loop)
        assert sdkloop.current() is loop
        sdkloop.restore(None)          # what run_isolated does when it found no binding
        assert sdkloop.current() is loop

    asyncio.run(home())
