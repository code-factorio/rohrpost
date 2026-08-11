"""Sync providers (spec §8.5).

A provider adapts a remote tracker to a flat ``{field: value}`` map in the local
vocabulary, so the sync orchestrator (:mod:`rohrpost.sync`) is provider-agnostic
and only deals with the three-way merge. GitHub is built first — simplest auth,
fastest feedback.
"""

from __future__ import annotations

from typing import Any, Protocol


class Provider(Protocol):
    """The contract every sync provider satisfies.

    ``fetch`` returns the remote item as ``{local_field: value}``; ``push``
    writes ``{local_field: value}`` back and returns the resulting remote fields.
    """

    remote: str

    def fetch(self, ref: str) -> dict[str, Any]: ...

    def push(self, ref: str, fields: dict[str, Any]) -> dict[str, Any]: ...


__all__ = ["Provider"]
