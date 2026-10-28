"""Checks on test helpers that are not covered by a provider contract."""

from __future__ import annotations

from tests.fakes import FakeSlackClient


def test_fake_slack_client_records_calls_in_order():
    client = FakeSlackClient()
    first = client.chat_postMessage(channel="C1", text="hello")
    client.chat_update(channel="C1", ts=first["ts"], text="edited")
    assert [method for method, _ in client.calls] == ["chat.postMessage", "chat.update"]
    assert client.posted_texts() == ["hello"]
