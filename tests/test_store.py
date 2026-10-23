

def test_same_message_over_two_planes_is_stored_once(tmp_path):
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "d.db") as s:
        base = dict(sender="aa" * 32, kind=14, created_at=1_700_000_000,
                    content="Agreed on both calls.", conversation="aa" * 32,
                    reply_to=None, reach="immediate", authority="instructions",
                    rule="test", why="test")
        assert s.add(Inbound(id="r1", wrapper_id="w1", plane="nip17", **base)) is True
        # same text, one second later, arriving over NIP-04 with a different id
        assert s.add(Inbound(id="r2", wrapper_id="w2", plane="nip04",
                             **{**base, "created_at": 1_700_000_001})) is False


def test_distinct_messages_from_same_sender_both_stored(tmp_path):
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "d2.db") as s:
        base = dict(sender="bb" * 32, kind=14, created_at=1_700_000_000,
                    conversation="bb" * 32, reply_to=None, reach="immediate",
                    authority="instructions", rule="t", why="t")
        assert s.add(Inbound(id="a", wrapper_id="a", content="one", plane="nip17", **base))
        assert s.add(Inbound(id="b", wrapper_id="b", content="two", plane="nip17", **base))
