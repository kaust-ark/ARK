"""RoomClient against an in-process stand-in of the SharedNet V1 API."""

import threading
import time

import pytest

from ark.sharednet.room import Invite, RoomClient, RoomError
from tests.fake_sharednet import FakeSharedNet

pytestmark = pytest.mark.unit


@pytest.fixture
def fake():
    with FakeSharedNet() as server:
        yield server


def test_invite_parses_the_pasted_text():
    invite = Invite.parse(
        "Read https://sharednet.ai/skill.md and join Room rom_abc123.\n"
        "ROOM=rom_abc123 TOKEN=rit_" + "a" * 43 + "\nBASE=http://127.0.0.1:3001\nBrief: go",
    )
    assert invite.room_id == "rom_abc123"
    assert invite.token == "rit_" + "a" * 43
    assert invite.base_url == "http://127.0.0.1:3001"
    assert Invite.parse("rom_x rit_" + "b" * 43).base_url == "https://sharednet.ai"
    with pytest.raises(ValueError):
        Invite.parse("nothing here")


def test_join_send_read_wait(fake):
    a = RoomClient(fake.base_url, fake.room_id)
    history = a.join(fake.invite, "writer")
    assert history == []
    assert a.token.startswith("rmt_")
    assert a.member_id.startswith("mem_")
    assert a.name == "writer"

    first = a.send("hello")
    assert first.sequence == 1
    assert a.last_sequence == 0, "sending does not count as seeing"

    b = RoomClient(fake.base_url, fake.room_id)
    seen = b.join(fake.invite, "reviewer")
    assert [m.content for m in seen] == ["hello"]
    assert seen[0].sender_name == "writer"
    assert b.last_sequence == 1

    reply = b.send("hi back", reply_to=first.message_id)
    assert reply.reply_to == first.message_id

    fresh = a.messages(after=a.last_sequence)
    assert [m.content for m in fresh] == ["hello", "hi back"]
    assert a.last_sequence == 2

    # wait returns as soon as something new arrives
    got = {}

    def waiter():
        got["items"] = a.wait(timeout=5)

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.2)
    b.send("third")
    thread.join(timeout=6)
    assert [m.content for m in got["items"]] == ["third"]
    assert a.last_sequence == 3

    # and empty at timeout
    assert a.wait(timeout=0) == []


def test_errors_carry_sharednet_codes(fake):
    client = RoomClient(fake.base_url, fake.room_id)
    with pytest.raises(RoomError) as bad_invite:
        client.join("rit_" + "z" * 43, "x")
    assert bad_invite.value.status == 401
    assert bad_invite.value.code == "invalid_token"

    fake.store.revoked = True
    with pytest.raises(RoomError) as revoked:
        client.join(fake.invite, "x")
    assert revoked.value.code == "invite_revoked"

    fake.store.revoked = False
    client.join(fake.invite, "x")
    with pytest.raises(ValueError):
        client.send("y" * 40000)
