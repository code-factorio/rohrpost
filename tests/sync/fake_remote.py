"""Deterministic, strict in-memory provider used by sync scenario tests."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Iterator

from rohrpost.exceptions import RemoteItemNotFoundError


class FakeRemoteError(RuntimeError):
    """Injected fake-provider failure."""


def iter_clock() -> Iterator[str]:
    """Yield sortable deterministic watermarks."""
    for tick in itertools.count(1):
        yield f"{tick:020d}"


class FakeRemote:
    """In-memory tracker. Deterministic, rewindable, crashable.

    ``items`` and ``calls`` use remote-shaped names. The provider methods map
    those values to/from Rohrpost's local vocabulary, exercising the same
    config boundary as a real provider adapter.
    """

    remote: str

    def __init__(
        self,
        name: str = "fake",
        *,
        fields: dict[str, object] | None = None,
    ) -> None:
        self.name = name
        self.remote = name
        self.fields = fields or {}
        self.items: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self._fail_at: int | None = None
        self._call_count = 0
        self._clock = iter_clock()
        self._updated: dict[str, str] = {}

    def fetch(self, ref: str) -> dict[str, object]:
        self._request("fetch", ref, {})
        if ref not in self.items:
            raise RemoteItemNotFoundError(f"{self.name} item {ref} no longer exists")
        return self._to_local(self.items[ref])

    def push(self, ref: str, fields: dict[str, object]) -> dict[str, object]:
        payload = self._to_remote(fields)
        self._request("push", ref, payload)
        if ref not in self.items:
            raise RemoteItemNotFoundError(f"{self.name} item {ref} no longer exists")
        self.items[ref].update(copy.deepcopy(payload))
        self._updated[ref] = next(self._clock)
        return self._to_local(self.items[ref])

    def changed_since(self, watermark: str) -> list[str]:
        self._request("changed_since", watermark, {})
        return sorted(ref for ref, updated in self._updated.items() if updated > watermark)

    def edit(self, ref: str, **fields: object) -> None:
        """Apply an out-of-band, remote-shaped mutation without recording a call."""
        if ref not in self.items:
            raise RemoteItemNotFoundError(f"{self.name} item {ref} no longer exists")
        self.items[ref].update(copy.deepcopy(fields))
        self._updated[ref] = next(self._clock)

    def delete(self, ref: str) -> None:
        """Simulate a human deleting a remote item."""
        self.items.pop(ref, None)
        self._updated.pop(ref, None)

    def fail_after(self, n: int | None) -> None:
        """Raise on the nth subsequent provider call; ``None`` disables failure."""
        if n is not None and n < 1:
            raise ValueError("failure call must be >= 1")
        self._fail_at = n
        self._call_count = 0

    def freeze(self) -> dict[str, dict[str, object]]:
        """Return an independent snapshot suitable for before/after assertions."""
        return copy.deepcopy(self.items)

    def seed(self, ref: str, fields: dict[str, object]) -> None:
        """Create an item from local-vocabulary fields without recording a call."""
        self.items[ref] = self._to_remote(fields)
        self._updated[ref] = next(self._clock)

    def local_item(self, ref: str) -> dict[str, object]:
        """Inspect an item in local vocabulary without recording a provider call."""
        if ref not in self.items:
            raise RemoteItemNotFoundError(f"{self.name} item {ref} no longer exists")
        return self._to_local(self.items[ref])

    def _request(self, verb: str, ref: str, payload: dict[str, object]) -> None:
        self.calls.append((verb, ref, copy.deepcopy(payload)))
        self._call_count += 1
        if self._call_count == self._fail_at:
            raise FakeRemoteError(f"injected failure on call {self._call_count}: {verb} {ref}")

    def _to_remote(self, local: dict[str, object]) -> dict[str, object]:
        remote: dict[str, object] = {}
        for local_name, mapping in self.fields.items():
            if local_name not in local:
                continue
            value = local[local_name]
            if isinstance(mapping, str):
                remote[mapping] = copy.deepcopy(value)
            elif isinstance(mapping, dict):
                remote[local_name] = copy.deepcopy(mapping.get(str(value), value))
        return remote

    def _to_local(self, remote: dict[str, object]) -> dict[str, object]:
        local: dict[str, object] = {}
        for local_name, mapping in self.fields.items():
            if isinstance(mapping, str) and mapping in remote:
                local[local_name] = copy.deepcopy(remote[mapping])
            elif isinstance(mapping, dict) and local_name in remote:
                value = remote[local_name]
                local[local_name] = next(
                    (name for name, mapped in mapping.items() if mapped == value), value
                )
        return local
