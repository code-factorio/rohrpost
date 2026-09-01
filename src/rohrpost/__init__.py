"""rohrpost: a git-native ticket system for agentic coding workflows.

The repository is the source of truth; SaaS trackers (GitHub, Jira, GitLab,
Linear) are projections that get synced. The event log is truth; tickets are a
fold over it. One write path: every mutation goes through ``rp`` (see
:mod:`rohrpost.cli`).

See the project README and ``docs/spec/ROHRPOST-SPEC.md`` for the full design.
"""

from rohrpost.events import Event, Op, decode_line, encode
from rohrpost.exceptions import (
    ConfigError,
    IdError,
    RohrpostError,
    StoreError,
    TicketError,
    TicketNotFoundError,
    UsageError,
)
from rohrpost.fold import Comment, Ticket
from rohrpost.ids import (
    is_valid_ticket_id,
    is_valid_ulid,
    new_ticket_id,
    new_ulid,
    normalize_id,
    render_id,
)

__all__ = [
    "Comment",
    "ConfigError",
    "Event",
    "IdError",
    "Op",
    "RohrpostError",
    "StoreError",
    "Ticket",
    "TicketError",
    "TicketNotFoundError",
    "UsageError",
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
