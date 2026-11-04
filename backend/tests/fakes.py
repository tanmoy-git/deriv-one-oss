"""In-memory stand-ins for provider interfaces, for unit tests.

Each fake implements the same abstract base class as the real provider and is
run through the same contract tests (tests/contracts/), so a test that passes
against a fake is exercising the documented behaviour, not an accident of the
fake.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from providers.blobs import BlobInfo, BlobStore, _normalise_key
from providers.secrets import SecretBackend, SecretBackendError, _prefixed


class InMemorySecretBackend(SecretBackend):
    """Versioned secrets in a dict. References look like ``mem:<name>:<n>``."""

    def __init__(self) -> None:
        self._versions: dict[str, list[str]] = {}

    def put(self, name: str, value: str) -> str:
        name = _prefixed(self.validate_name(name))
        versions = self._versions.setdefault(name, [])
        versions.append(value)
        return f"mem:{name}:{len(versions)}"

    def resolve(self, ref: str) -> str:
        try:
            scheme, name, version = ref.split(":")
            index = int(version) - 1
        except ValueError as exc:
            raise SecretBackendError("malformed secret reference") from exc
        if scheme != "mem" or index < 0:
            raise SecretBackendError("malformed secret reference")
        try:
            return self._versions[name][index]
        except (KeyError, IndexError) as exc:
            raise KeyError(ref) from exc

    def latest_ref(self, name: str) -> str:
        name = _prefixed(self.validate_name(name))
        versions = self._versions.get(name)
        if not versions:
            raise KeyError(name)
        return f"mem:{name}:{len(versions)}"

    def delete(self, name: str) -> None:
        self._versions.pop(_prefixed(self.validate_name(name)), None)


class InMemoryBlobStore(BlobStore):
    """Blobs in a dict, with the same key rules as LocalBlobStore."""

    def __init__(self) -> None:
        self._blobs: dict[str, tuple[bytes, float]] = {}

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self._blobs[_normalise_key(key)] = (bytes(data), time.time())

    def get(self, key: str) -> bytes:
        try:
            return self._blobs[_normalise_key(key)][0]
        except KeyError as exc:
            raise KeyError(key) from exc

    def delete(self, key: str) -> None:
        self._blobs.pop(_normalise_key(key), None)

    def exists(self, key: str) -> bool:
        return _normalise_key(key) in self._blobs

    def list(self, prefix: str = "") -> list[BlobInfo]:
        cleaned = (prefix or "").strip().strip("/")
        return sorted(
            (
                BlobInfo(key=k, size=len(data), modified_at=mtime)
                for k, (data, mtime) in self._blobs.items()
                if k.startswith(cleaned)
            ),
            key=lambda b: b.key,
        )


@dataclass
class FakeSlackClient:
    """Records Slack Web API calls instead of making them.

    Covers the calls the backend makes most (chat_postMessage, chat_update,
    reactions_add). ``calls`` holds ``(method, kwargs)`` in order.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _ts: int = 0

    def _record(self, method: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        self._ts += 1
        return {"ok": True, "channel": kwargs.get("channel"), "ts": f"{self._ts}.000000"}

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802 - Slack SDK name
        return self._record("chat.postMessage", **kwargs)

    def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("chat.update", **kwargs)

    def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("reactions.add", **kwargs)

    def posted_texts(self) -> list[str]:
        return [kw.get("text", "") for method, kw in self.calls if method == "chat.postMessage"]
