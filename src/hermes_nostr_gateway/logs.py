"""Keep keys out of log lines without hiding them from a person who asks.

A peer's npub plus the relays it appears on in the same line is a social graph, and a
pasted ``journalctl`` excerpt is then a durable, linkable record of one. So every line
the daemon emits is rewritten once, before a handler formats it:

* ``nsec1…`` becomes ``nsec1[redacted]`` at every level — a secret in a log has no
  diagnostic value that outweighs being pasted into a ticket;
* ``npub1…`` keeps its first eight characters unless the emitting logger is running at
  DEBUG, which is enough to correlate lines without resolving the key.

The filter is installed on the logger tree rather than called from each log site: a
convention is not a guarantee, and the one ``log.info`` that forgets it would be the one
that leaks. ``install_log_redaction`` attaches it to the root logger and to every logger
object alive at that moment, so the record itself is rewritten before any later handler
(``caplog`` included) sees it, and to every handler those loggers own, so a third-party
logger that propagates to the daemon's handler is covered too. A logger created after
installation is still filtered when it propagates to the root handler; only its record
object is not rewritten in place.

The whole value is one flag away: ``hng serve -v`` puts the tree at DEBUG, and then even
the INFO startup line carries the full npub. CLI output is evidence, not a log: ``hng
status``, ``hng health`` and ``hng adapter status`` print what they always printed.

The Hermes adapter cannot import this module — it is installed by copying one file into
``$HERMES_HOME/plugins/nostr`` — so ``integrations/hermes-plugin/adapter.py`` carries the
same two patterns beside its own logger. Change one, change the other.
"""

from __future__ import annotations

import logging
import re

#: A secret never has a shortened form worth reading; redact the whole value.
NSEC_PATTERN = re.compile(r"nsec1[0-9a-z]+")

#: An npub keeps its prefix and three payload characters: enough to tell two peers apart
#: in a series of lines, not enough to resolve the key.
NPUB_PATTERN = re.compile(r"npub1[0-9a-z]{20,}")
NPUB_PREFIX_CHARS = 8

#: The daemon's one log format. `%(name)s` is how a pasted line names its subsystem.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def redact(text: str, *, truncate_npubs: bool = True) -> str:
    """Rewrite keys in *text*: nsecs always, npubs when *truncate_npubs*."""
    text = NSEC_PATTERN.sub("nsec1[redacted]", text)
    if truncate_npubs:
        text = NPUB_PATTERN.sub(
            lambda found: found.group(0)[:NPUB_PREFIX_CHARS] + "…", text)
    return text


class LogRedactionFilter(logging.Filter):
    """Shorten npubs unless the emitter is at DEBUG; redact nsecs at every level.

    Filters run before formatting, so the rewrite is done as the message objects: the
    record's args are consumed into the message and dropped. That is what makes an INFO
    line already shortened for every handler, not just the one the filter sits on.

    The decision is the emitting logger's effective level, not the record's own: the
    startup line is logged at INFO in both modes, and ``hng serve -v`` has to print it
    whole or the operator cannot read the identity from the journal.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string is the log's problem
            return True
        quiet = not logging.getLogger(record.name).isEnabledFor(logging.DEBUG)
        rewritten = redact(message, truncate_npubs=quiet)
        if rewritten != message:
            record.msg = rewritten
            record.args = ()
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> LogRedactionFilter:
    """Attach one redaction filter to *logger* (default: root) and everything under it.

    Called once per process after logging is configured. Repeating it is harmless: the
    same filter object is reused rather than stacked.
    """
    root = logging.getLogger() if logger is None else logger
    filt = next((f for f in root.filters if isinstance(f, LogRedactionFilter)), None)
    if filt is None:
        filt = LogRedactionFilter()

    whole_tree = logger is None
    targets = [root]
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if not isinstance(obj, logging.Logger):
            continue  # a PlaceHolder for a parent nobody has asked for yet
        if whole_tree or obj is root or obj.name.startswith(root.name + "."):
            targets.append(obj)
    for target in targets:
        if filt not in target.filters:
            target.addFilter(filt)
        for handler in target.handlers:
            if filt not in handler.filters:
                handler.addFilter(filt)
    return filt


def configure_logging(verbose: bool = False) -> None:
    """The daemon's logging setup: level, format, and the redaction filter.

    ``hng serve -v`` is the documented way to read a full key back out of the journal;
    at INFO the filter keeps eight characters of an npub so lines remain correlatable.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    # basicConfig does nothing when the root logger already has a handler, and `-v`
    # must mean DEBUG either way.
    logging.getLogger().setLevel(level)
    install_log_redaction()
