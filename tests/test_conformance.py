"""The gateway's proactive conformance probes, run as tests.

These are not unit tests of a module: they are the protocol traps the retired phase-0
wrapper pinned down (spec §4, §7, §13.1), executed against the shipping code. A probe
here fails when an invariant is removed or drifts, which is the point of keeping it.
"""

from __future__ import annotations

import io

import pytest

from probe_nip17_conformance import PROBES, run_offline


@pytest.mark.parametrize("probe", PROBES, ids=[p.name for p in PROBES])
def test_conformance_probe(probe):
    probe.fn()


def test_the_probe_runner_reports_every_probe():
    assert run_offline(stream=io.StringIO()) == 0
