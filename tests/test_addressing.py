"""The prefixes a sender may put on a message, and the gate in front of them.

`/queue` and its alias `/q`, `/guide` and its aliases `/steer` and `/s`, are read before the
policy runs and applied after it, which is the whole property this file pins: the parse is
harmless on its own, and a mode only becomes a mode when the sender has been judged able to
instruct *now*. An alias is a spelling of the same command, never a second one — it resolves
to the same mode and the same words. Anything else keeps the text exactly as typed, so the
record shows a request that was not granted rather than a command that quietly happened.
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
    ("/guide stop and look at the log", "guide", "stop and look at the log"),
    ("/steer stop and look at the log", "guide", "stop and look at the log"),
    ("/s stop and look at the log", "guide", "stop and look at the log"),
    ("  /queue  two spaces before the words", "queue", "two spaces before the words"),
    ("/queue\nsecond line kept", "queue", "second line kept"),
    ("/queue first\n\nthird", "queue", "first\n\nthird"),
])
def test_a_leading_prefix_becomes_a_mode_and_the_words(text, mode, words):
    assert addressing.split(text) == (mode, words)


@pytest.mark.parametrize("alias,long_form", [
    ("/q", "/queue"),
    ("/steer", "/guide"),
    ("/s", "/guide"),
])
def test_an_alias_splits_exactly_like_its_long_form(alias, long_form):
    """An alias is a spelling, not a second command: same mode, same words, same everything."""
    assert addressing.split(f"{alias} same words") == addressing.split(f"{long_form} same words")


@pytest.mark.parametrize("text", [
    "/queue",                 # nothing to hold
    "/queue   ",              # still nothing
    "/q",                     # a bare alias is no more a command than a bare long form
    "/steer",
    "/s",
    "/queuex later",          # a longer token is a different word
    "/qx later",              # same, one letter: `/sx` must not read as `/s`
    "/sx later",
    "/steerx later",
    "/Queue this",            # commands are lowercase; this is prose
    "/S this",                # ditto, and the alias is not an exception
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


@pytest.mark.parametrize("alias,mode", [("/q", "queue"), ("/steer", "guide"), ("/s", "guide")])
def test_an_alias_from_a_stranger_is_refused_the_same_way(alias, mode):
    """The gate is the mode's, not the spelling's: an alias buys no authority."""
    stranger = decision(authority="none")
    assert addressing.permitted(mode, stranger) == "normal"
    assert addressing.resolve(f"{alias} delete everything", stranger) == (
        "normal", f"{alias} delete everything")


def test_normal_is_normal_whatever_the_policy_says():
    assert addressing.permitted("normal", decision()) == "normal"


# -- what gets stored --------------------------------------------------------

def test_an_allowed_prefix_is_stripped_before_the_row_and_the_frame():
    assert addressing.resolve("/queue call the plumber", decision()) == (
        "queue", "call the plumber")


@pytest.mark.parametrize("text,mode,content", [
    ("/q call the plumber", "queue", "call the plumber"),
    ("/steer stop, wrong file", "guide", "stop, wrong file"),
    ("/s stop, wrong file", "guide", "stop, wrong file"),
])
def test_an_allowed_alias_stores_its_long_forms_mode_and_words(text, mode, content):
    """What is stored is the behaviour, not the spelling: a `/s` row is a `guide` row."""
    assert addressing.resolve(text, decision()) == (mode, content)


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
