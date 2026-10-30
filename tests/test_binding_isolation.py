"""The bindings' event loop is process-global: prove the suite hands it back.

`sdkloop` binds uniffi's `_UNIFFI_GLOBAL_EVENT_LOOP` to a loop, and a test that starts a
pool or a gateway binds its own and leaves it there. The next test then inherits a loop
that is already closed — invisible until something signs on it from a Rust worker thread,
where there is no running loop to fall back to. NIP-42 does, so the failure lands in a
neighbour: `test_nip42_auth.py` failed only when the rest of the suite had run first.

`tests/conftest.py` owns the binding between tests. These two tests are the proof: the
first leaves a dead loop bound exactly as 58 tests in this suite used to, and the second
is the neighbour that must not see it.
"""

from __future__ import annotations

import asyncio

from hermes_nostr_gateway import sdkloop

#: Filled by the first test and checked by the second: pytest runs a file in order.
_LEAKED: dict[str, asyncio.AbstractEventLoop] = {}


def test_a_test_that_leaves_a_dead_loop_bound_is_contained():
    """The polluter: bind a loop that is already dead, and walk away without unbinding."""
    loop = asyncio.new_event_loop()
    loop.close()
    _LEAKED["loop"] = loop
    sdkloop._HOME = loop
    sdkloop._set(loop)  # what `set_home()` does inside a pool or a gateway
    assert sdkloop.current() is loop


def test_the_next_test_starts_from_the_binding_its_predecessor_found():
    """The neighbour: it must not inherit the dead loop above."""
    assert "loop" in _LEAKED, "the polluting test must run first: see this file's order"
    assert sdkloop.current() is not _LEAKED["loop"], (
        "the bindings' loop was handed to a neighbour: tests/conftest.py's "
        "`sdkloop_binding` fixture is what stops a dead loop crossing a test boundary")
    assert sdkloop._HOME is not _LEAKED["loop"], (
        "the one-shot home loop was handed to a neighbour: see tests/conftest.py")
