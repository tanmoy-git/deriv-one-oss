"""Behaviour every SecretBackend must have, run against each implementation."""

from __future__ import annotations

import pytest

from providers.secrets import SecretBackendError
from tests.fakes import InMemorySecretBackend


@pytest.fixture(params=["local", "memory"])
def backend(request):
    if request.param == "local":
        return request.getfixturevalue("local_secret_backend")
    return InMemorySecretBackend()


def test_put_then_resolve_round_trips(backend):
    ref = backend.put("api-key", "s3cret-value")
    assert backend.resolve(ref) == "s3cret-value"


def test_put_again_creates_a_new_version_and_old_ref_still_resolves(backend):
    first = backend.put("api-key", "v1")
    second = backend.put("api-key", "v2")
    assert first != second
    assert backend.resolve(second) == "v2"
    assert backend.resolve(first) == "v1"
    assert backend.latest_ref("api-key") == second


def test_delete_removes_every_version(backend):
    ref = backend.put("api-key", "v1")
    backend.delete("api-key")
    assert not backend.exists("api-key")
    with pytest.raises(KeyError):
        backend.resolve(ref)


def test_delete_of_absent_name_is_not_an_error(backend):
    backend.delete("never-stored")


def test_latest_ref_of_absent_name_raises_key_error(backend):
    with pytest.raises(KeyError):
        backend.latest_ref("never-stored")


@pytest.mark.parametrize("name", ["", "../escape", "has space", "a/b"])
def test_unsafe_names_are_rejected(backend, name):
    with pytest.raises(SecretBackendError):
        backend.put(name, "value")


def test_malformed_reference_is_rejected_without_echoing_it(backend):
    with pytest.raises((SecretBackendError, KeyError)) as exc:
        backend.resolve("not-a-reference")
    assert "s3cret" not in str(exc.value)
