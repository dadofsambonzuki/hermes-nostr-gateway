"""A log line is a document: keep keys out of it, on the real logger.

A peer's npub plus the relay it arrived on is a social graph once the line is pasted
into a ticket or an issue. The rule is installed on the logger rather than applied by
hand at every log site, so these tests log through the logger the daemon and the adapter
actually use and assert what a handler would print.

The keys here are bech32-shaped and checksum-free — built in this file, never a real
key. A fixture holding one would be the leak the filter exists to prevent.
"""

from __future__ import annotations

import io
import logging

from adapter_harness import load_adapter
from hermes_nostr_gateway.logs import (
    LogRedactionFilter,
    configure_logging,
    install_log_redaction,
    redact,
)

#: `npub1`/`nsec1` followed by a bech32 character set, twice over: the right shape for
#: the patterns, and no encoded key at all.
_BODY = "qzry9x8gf2tvdw0s3jn54khce6mua7l" * 2
NPUB = "npub1" + _BODY
NSEC = "nsec1" + _BODY
SHORT_NPUB = NPUB[:8] + "…"

#: A 64-hex event id is how the store is debugged, and the filter must not touch it.
EVENT_ID = "0123456789abcdef" * 4


def test_redact_rewrites_keys_by_shape():
    line = f"peer {NPUB} key {NSEC} nostr:{NPUB}"
    assert redact(line) == f"peer {SHORT_NPUB} key nsec1[redacted] nostr:{SHORT_NPUB}"


def test_redact_can_keep_the_npub_whole():
    # DEBUG chops nothing off the identity; the secret is still redacted.
    assert redact(f"{NPUB} {NSEC}", truncate_npubs=False) == f"{NPUB} nsec1[redacted]"


def test_an_info_record_through_the_real_logger_is_shortened(caplog):
    install_log_redaction()
    with caplog.at_level(logging.INFO, logger="hng.gateway"):
        logging.getLogger("hng.gateway").info("peer %s keyed %s", NPUB, NSEC)

    message = caplog.records[-1].getMessage()
    assert SHORT_NPUB in message
    assert NPUB not in message
    assert "nsec1[redacted]" in message
    assert NSEC not in message


def test_a_debug_record_keeps_the_npub_and_still_redacts_the_nsec(caplog):
    install_log_redaction()
    with caplog.at_level(logging.DEBUG, logger="hng.gateway"):
        logging.getLogger("hng.gateway").debug("peer %s keyed %s", NPUB, NSEC)

    message = caplog.records[-1].getMessage()
    assert NPUB in message
    assert "nsec1[redacted]" in message
    assert NSEC not in message


def test_a_debug_run_keeps_the_npub_on_an_info_line(caplog):
    """`hng serve -v`: the startup line is logged at INFO, and -v must still print it whole."""
    install_log_redaction()
    with caplog.at_level(logging.DEBUG, logger="hng.gateway"):
        logging.getLogger("hng.gateway").info("gateway up: %s on 4 relays", NPUB)

    assert NPUB in caplog.records[-1].getMessage()


def test_an_event_id_is_not_touched(caplog):
    install_log_redaction()
    with caplog.at_level(logging.INFO, logger="hng.store"):
        logging.getLogger("hng.store").info("stored event %s", EVENT_ID)

    assert EVENT_ID in caplog.records[-1].getMessage()


def test_the_filter_rewrites_before_a_handler_formats_the_line():
    """A real handler, not caplog: the record is already rewritten on the way in."""
    log = logging.getLogger("hng.gateway")
    install_log_redaction()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("peer %s", NPUB)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)

    assert SHORT_NPUB in stream.getvalue()
    assert NPUB not in stream.getvalue()


def test_configure_logging_installs_the_filter_and_verbose_means_debug():
    root = logging.getLogger()
    old_level, old_handlers = root.level, list(root.handlers)
    try:
        configure_logging(verbose=True)
        assert root.level == logging.DEBUG
        assert any(isinstance(f, LogRedactionFilter) for f in root.filters)
        assert all(any(isinstance(f, LogRedactionFilter) for f in handler.filters)
                   for handler in root.handlers)
    finally:
        root.setLevel(old_level)
        for handler in list(root.handlers):
            if handler not in old_handlers:
                root.removeHandler(handler)


def test_the_adapter_logger_shortens_the_same_line(caplog):
    """The adapter carries its own copy of the filter; it must behave the same."""
    adapter = load_adapter("nostr_adapter_log_redaction")
    assert any(isinstance(f, adapter.LogRedactionFilter) for f in adapter.logger.filters)

    with caplog.at_level(logging.INFO, logger=adapter.logger.name):
        adapter.logger.info("nostr peer %s keyed %s", NPUB, NSEC)
    message = caplog.records[-1].getMessage()
    assert SHORT_NPUB in message and NPUB not in message
    assert "nsec1[redacted]" in message and NSEC not in message

    with caplog.at_level(logging.DEBUG, logger=adapter.logger.name):
        adapter.logger.debug("nostr peer %s", NPUB)
    assert NPUB in caplog.records[-1].getMessage()
