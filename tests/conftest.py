"""Shared test setup: short sockets, and one owner for the bindings' process-global loop.

A Unix socket path is capped near 108 bytes. This box sets TMPDIR to a deep directory,
and pytest's per-test tmp_path then pushes the marmot tests' socket paths past the cap —
they fail with ENAMETOOLONG on a green tree. Redirect the temp base before pytest creates
any tmp_path directory.
"""
import os
import tempfile

import pytest

from hermes_nostr_gateway import sdkloop

if len(tempfile.gettempdir()) > 20:
    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = None

#: What the process looks like before any test runs: no binding, no home.
_BASELINE = (sdkloop.current(), sdkloop._HOME)


@pytest.fixture(autouse=True)
def sdkloop_binding():
    """Own uniffi's process-global loop between tests: snapshot it, then put it back.

    `_UNIFFI_GLOBAL_EVENT_LOOP` and `sdkloop._HOME` are process-global, and a test that
    starts a pool or a gateway binds its own event loop and never unbinds it. The next
    test then inherits a loop that is already closed — which stays invisible until
    something signs on it from a Rust worker thread, where there is no running loop to
    fall back to. NIP-42 does exactly that, so the failure lands in a *neighbour*: in a
    full run `test_nip42_auth.py` inherited state from tests it has nothing to do with.

    So the binding is owned here rather than by the tests that move it (a fixture, not a
    per-test patch): every test starts from the binding the process had before any test
    ran, and hands it back the same way. `test_binding_isolation.py` proves a leak no
    longer crosses a test boundary, so deleting this fixture fails loudly instead of
    quietly re-introducing the flake.
    """
    if sdkloop.current() is not _BASELINE[0] or sdkloop._HOME is not _BASELINE[1]:
        raise AssertionError(
            "the bindings' loop was left behind by the previous test: "
            f"{sdkloop.current()!r}, home {sdkloop._HOME!r} — a test may move the binding "
            "while it runs but must not hand a loop to the next one")
    try:
        yield
    finally:
        sdkloop._HOME = _BASELINE[1]
        sdkloop._set(_BASELINE[0])  # exact restore: `restore()` substitutes home
