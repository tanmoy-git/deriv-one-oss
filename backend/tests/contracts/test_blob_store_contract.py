"""Behaviour every BlobStore must have, run against each implementation."""

from __future__ import annotations

import pytest

from providers.blobs import BlobStoreError
from tests.fakes import InMemoryBlobStore


@pytest.fixture(params=["local", "memory"])
def store(request):
    if request.param == "local":
        return request.getfixturevalue("local_blob_store")
    return InMemoryBlobStore()


def test_put_then_get_round_trips(store):
    store.put("a/b.txt", b"hello")
    assert store.get("a/b.txt") == b"hello"
    assert store.exists("a/b.txt")


def test_put_replaces_existing_content(store):
    store.put("k", b"one")
    store.put("k", b"two")
    assert store.get("k") == b"two"


def test_get_of_absent_key_raises_key_error(store):
    with pytest.raises(KeyError):
        store.get("missing")


def test_delete_of_absent_key_is_not_an_error(store):
    store.delete("missing")
    assert not store.exists("missing")


def test_list_filters_by_prefix_and_is_sorted(store):
    for key in ("chats/2", "chats/1", "other/1"):
        store.put(key, b"x")
    assert [b.key for b in store.list("chats")] == ["chats/1", "chats/2"]


def test_delete_prefix_returns_count(store):
    for key in ("tmp/1", "tmp/2", "keep/1"):
        store.put(key, b"x")
    assert store.delete_prefix("tmp") == 2
    assert [b.key for b in store.list()] == ["keep/1"]


@pytest.mark.parametrize("key", ["", "/", "../etc/passwd", "a/../../b"])
def test_keys_that_escape_the_store_are_rejected(store, key):
    with pytest.raises(BlobStoreError):
        store.put(key, b"x")


def test_text_helpers_round_trip(store):
    store.put_text("note.txt", "héllo")
    assert store.get_text("note.txt") == "héllo"
