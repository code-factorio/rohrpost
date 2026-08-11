"""rohrpost: a git-native ticket system for agentic coding workflows.

The repository is the source of truth; SaaS trackers (GitHub, Jira, GitLab,
Linear) are projections that get synced. This package exposes the primitives
that implement the append-only event log (:mod:`rohrpost.events`) and the id
schemes it depends on (:mod:`rohrpost.ids`). The binary is ``rp`` (see
:mod:`rohrpost.cli`).

See the project README and ``docs/spec/ROHRPOST-SPEC.md`` for the full design.
"""

from rohrpost.events import Event, Op, decode_line, encode
from rohrpost.exceptions import IdError, RohrpostError, StoreError
from rohrpost.ids import (
    is_valid_ticket_id,
    is_valid_ulid,
    new_ticket_id,
    new_ulid,
    normalize_id,
    render_id,
)

__all__ = [
    "Event",
    "IdError",
    "Op",
    "RohrpostError",
    "StoreError",
    "decode_line",
    "encode",
    "is_valid_ticket_id",
    "is_valid_ulid",
    "new_ticket_id",
    "new_ulid",
    "normalize_id",
    "render_id",
]

__version__ = "0.1.0"
