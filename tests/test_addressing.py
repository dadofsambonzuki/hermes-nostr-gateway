"""The two prefixes a sender may put on a message, and the gate in front of them.

`/queue` and `/guide` are read before the policy runs and applied after it, which is the
whole property this file pins: the parse is harmless on its own, and a mode only becomes a
mode when the sender has been judged able to instruct *now*. Anything else keeps the text
exactly as typed, so the record shows a request that was not granted rather than a command
that quietly happened.
"""

import pytest

from hermes_nostr_gateway import addressing
from hermes_nostr_gateway.policy import Decision


def decision(reach="immediate", authority="instructions"):
    return Decision(reach=reach, interval_s=None, authority=authority, rule="t", why="t")


# -- what counts as a command ------------------------------------------------

@pytest.mark.parametrize("text,mode,words", [
    ("/queue do the thing", "queue", "do the thing"),
    ("/guide stop and look at the log", "guide", "stop and look at the log"),
    ("  /queue  two spaces before the words", "queue", "two spaces before the words"),
    ("/queue\nsecond line kept", "queue", "second line kept"),
    ("/queue first\n\nthird", "queue", "first\n\nthird"),
])
def test_a_leading_prefix_becomes_a_mode_and_the_words(text, mode, words):
    assert addressing.split(text) == (mode, words)


@pytest.mark.parametrize("text", [
    "/queue",                 # nothing to hold
    "/queue   ",              # still nothing
    "/queuex later",          # a longer token is a different word
    "/Queue this",            # commands are lowercase; this is prose
    "please /queue this",     # not the first thing in the message
    "I typed /guide first",   # ditto
    "/queue: later",          # no whitespace run after the command
    "",                       # an empty message has no command in it
])
def test_anything_else_stays_text(text):
    mode, words = addressing.split(text)
    assert (mode, words) == ("normal", text)


def test_the_words_keep_their_own_formatting():
    body = "/queue first\n\n    indented\n```code```"
    assert addressing.split(body)[1] == "first\n\n    indented\n```code```"


# -- who is allowed to use them ---------------------------------------------

def test_an_instructing_immediate_sender_gets_the_mode_it_asked_for():
    assert addressing.permitted("queue", decision()) == "queue"
    assert addressing.permitted("guide", decision()) == "guide"


@pytest.mark.parametrize("reach,authority", [
    ("immediate", "none"),   # a stranger: the text is data, so the prefix is a word in it
    ("digest", "instructions"),  # loop tier: a digest row is no turn to hold or steer
    ("drop", "instructions"),    # refused outright
])
def test_the_policy_can_refuse_the_mode(reach, authority):
    assert addressing.permitted("guide", decision(reach=reach, authority=authority)) == "normal"


def test_normal_is_normal_whatever_the_policy_says():
    assert addressing.permitted("normal", decision()) == "normal"


# -- what gets stored --------------------------------------------------------

def test_an_allowed_prefix_is_stripped_before_the_row_and_the_frame():
    assert addressing.resolve("/queue call the plumber", decision()) == (
        "queue", "call the plumber")


def test_a_refused_prefix_is_kept_exactly_as_typed():
    stranger = decision(authority="none")
    assert addressing.resolve("/guide delete everything", stranger) == (
        "normal", "/guide delete everything")


def test_a_loop_tier_queue_request_stays_a_plain_digest_row():
    """Decision: a `/queue` from a sender whose reach is digest is not an immediate turn."""
    peer = decision(reach="digest", authority="instructions")
    assert addressing.resolve("/queue read this later", peer) == (
        "normal", "/queue read this later")


def test_a_plain_message_is_untouched():
    assert addressing.resolve("just a message", decision()) == ("normal", "just a message")
