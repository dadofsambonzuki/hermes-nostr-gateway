"""The words a sender may put on a message, and the gate in front of them.

`/queue` (`/q`) and `/steer` (`/s`) are read before the policy runs and applied after it, which
is the whole property this file pins: the parse is harmless on its own, and a mode only becomes
a mode when the sender has been judged able to instruct *now*. An alias is a spelling of the
same command, never a second one — it resolves to the same mode and the same words. Anything
else keeps the text exactly as typed, so the record shows a request that was not granted rather
than a command that quietly happened.

The words are Hermes' own. `/guide` was this gateway's coinage for steer and is retired: it
still resolves for a release so an old message is not silently treated as text, but it is not
the name of anything.
"""

import pytest

from hermes_nostr_gateway import addressing
from hermes_nostr_gateway.policy import Decision


def decision(reach="immediate", authority="instructions"):
    return Decision(reach=reach, interval_s=None, authority=authority, rule="t", why="t")


# -- what counts as a command ------------------------------------------------

@pytest.mark.parametrize("text,mode,words", [
    ("/queue do the thing", "queue", "do the thing"),
    ("/q do the thing", "queue", "do the thing"),
    ("/steer stop and look at the log", "steer", "stop and look at the log"),
    ("/s stop and look at the log", "steer", "stop and look at the log"),
    ("/guide stop and look at the log", "steer", "stop and look at the log"),
    ("  /queue  two spaces before the words", "queue", "two spaces before the words"),
    ("/queue\nsecond line kept", "queue", "second line kept"),
    ("/steer first\n\nthird", "steer", "first\n\nthird"),
])
def test_a_leading_prefix_becomes_a_mode_and_the_words(text, mode, words):
    assert addressing.split(text) == (mode, words)


@pytest.mark.parametrize("alias,word", [
    ("/q", "/queue"),
    ("/s", "/steer"),
    ("/guide", "/steer"),  # retired, still readable: the same table, not a second parser
])
def test_an_alias_splits_exactly_like_its_word(alias, word):
    """An alias is a spelling, not a second command: same mode, same words, same everything."""
    assert addressing.split(f"{alias} same words") == addressing.split(f"{word} same words")


@pytest.mark.parametrize("text", [
    "/queue",                 # nothing to hold
    "/queue   ",              # still nothing
    "/q",                     # a bare alias is no more a command than a bare word
    "/steer",                 # nothing to steer
    "/s",
    "/guide",                 # retired, and a bare one is text like any other
    "/queuex later",          # a longer token is a different word
    "/qx later",              # same, one letter: `/sx` must not read as `/s`
    "/sx later",
    "/steerx later",
    "/g stop",                # no `/g`: Hermes has no such command, `/s` is the short form
    "/Queue this",            # commands are lowercase; this is prose
    "/S this",                # ditto, and the alias is not an exception
    "/Guide this",            # the retired spelling is not an exception either
    "please /queue this",     # not the first thing in the message
    "I typed /guide first",   # ditto
    "so /s it is",            # ditto, even though `/s` is one letter
    "/queue: later",          # no whitespace run after the command
    "",                       # an empty message has no command in it
])
def test_anything_else_stays_text(text):
    mode, words = addressing.split(text)
    assert (mode, words) == ("normal", text)


def test_the_words_keep_their_own_formatting():
    body = "/queue first\n\n    indented\n```code```"
    assert addressing.split(body)[1] == "first\n\n    indented\n```code```"


def test_the_retired_spelling_is_not_a_mode():
    """`guide` was a spelling of steer, never a third behaviour to keep in step."""
    assert addressing.MODES == ("normal", "queue", "steer")
    assert addressing.MODE_STEER == "steer"


# -- who is allowed to use them ---------------------------------------------

def test_an_instructing_immediate_sender_gets_the_mode_it_asked_for():
    assert addressing.permitted("queue", decision()) == "queue"
    assert addressing.permitted("steer", decision()) == "steer"


@pytest.mark.parametrize("reach,authority", [
    ("immediate", "none"),   # a stranger: the text is data, so the prefix is a word in it
    ("digest", "instructions"),  # loop tier: a digest row is no turn to hold or steer
    ("drop", "instructions"),    # refused outright
])
def test_the_policy_can_refuse_the_mode(reach, authority):
    assert addressing.permitted("steer", decision(reach=reach, authority=authority)) == "normal"


@pytest.mark.parametrize("prefix,mode", [
    ("/q", "queue"), ("/s", "steer"), ("/guide", "steer"),
])
def test_an_alias_from_a_stranger_is_refused_the_same_way(prefix, mode):
    """The gate is the mode's, not the spelling's: an alias buys no authority."""
    stranger = decision(authority="none")
    assert addressing.permitted(mode, stranger) == "normal"
    assert addressing.resolve(f"{prefix} delete everything", stranger) == (
        "normal", f"{prefix} delete everything")


def test_normal_is_normal_whatever_the_policy_says():
    assert addressing.permitted("normal", decision()) == "normal"


# -- what gets stored --------------------------------------------------------

def test_an_allowed_prefix_is_stripped_before_the_row_and_the_frame():
    assert addressing.resolve("/queue call the plumber", decision()) == (
        "queue", "call the plumber")
    assert addressing.resolve("/steer use the other file", decision()) == (
        "steer", "use the other file")


@pytest.mark.parametrize("text,mode,content", [
    ("/q call the plumber", "queue", "call the plumber"),
    ("/s stop, wrong file", "steer", "stop, wrong file"),
    ("/guide stop, wrong file", "steer", "stop, wrong file"),
])
def test_an_allowed_alias_stores_its_words_mode(text, mode, content):
    """What is stored is the behaviour, not the spelling: a `/s` row is a `steer` row."""
    assert addressing.resolve(text, decision()) == (mode, content)


def test_a_refused_prefix_is_kept_exactly_as_typed():
    stranger = decision(authority="none")
    assert addressing.resolve("/steer delete everything", stranger) == (
        "normal", "/steer delete everything")


def test_a_loop_tier_queue_request_stays_a_plain_digest_row():
    """Decision: a `/queue` from a sender whose reach is digest is not an immediate turn."""
    peer = decision(reach="digest", authority="instructions")
    assert addressing.resolve("/queue read this later", peer) == (
        "normal", "/queue read this later")


def test_a_plain_message_is_untouched():
    assert addressing.resolve("just a message", decision()) == ("normal", "just a message")


# -- what an old row says ----------------------------------------------------

@pytest.mark.parametrize("stored,mode", [
    ("normal", "normal"),
    ("queue", "queue"),
    ("steer", "steer"),
    ("guide", "steer"),  # a row written before `/steer` was the name
    ("", "normal"),
    (None, "normal"),
])
def test_a_stored_mode_reads_in_todays_words(stored, mode):
    assert addressing.canonical(stored) == mode
