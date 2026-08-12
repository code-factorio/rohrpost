"""Byte-exact body round-trip tests (decision experiment E5).

Inline ticket bodies live *inside* a JSON string value in the append-only
``log.jsonl``. That makes the ``body`` field the single place where every
escaping concern in rohrpost concentrates: quotes, backslashes, ``\r``/``\n``,
NUL, the JSON-hostile U+2028/U+2029 separators, the full Unicode range (emoji,
CJK, RTL, combining marks, ZWJ sequences), and bodies that are themselves valid
JSON event lines. This module proves the msgspec codec round-trips all of them
byte-identically through the real create/read path.

A round-trip failure here is a codec bug to FIX — it is not evidence for moving
bodies out of the log into a sidecar. The only body this file expects to lose
is a whitespace-only one, and that drop is documented, intentional behaviour
which is pinned in its own test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from rohrpost import api, paths, store

# ---------------------------------------------------------------------------
# Adversarial corpus. Each entry is a body that is hostile to a JSON string
# field; the codec must return it byte-for-byte from api.show_ticket(...).body.
# Invisible/unparseable code points are written as escapes so the source stays
# clean (a literal NUL byte would make the module unparseable).
# ---------------------------------------------------------------------------
_MARKDOWN = """Here is some code:

```python
def greet(name: str) -> str:
    return f"hi {name}"
```

- one
- two"""

# Quotes, backslashes and CR/LF variants exercise JSON string escaping.
_JSON_ESCAPES = 'a"b\\c\r\nd\reof'

# Emoji plus a ZWJ family sequence: byte length != char length.
_UNICODE_EMOJI_ZWJ = "party \U0001f389 with a family: \U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"

# CJK ideographs (multi-byte in UTF-8, single code point per glyph).
_UNICODE_CJK = "Chinese 中文 Korean 한국어 Japanese 日本語 and テスト"

# Right-to-left script (Arabic); directionality must not survive a round trip.
_UNICODE_RTL = "RTL Arabic: السلام عليكم"

# Combining marks (NFD): base + combining char that must not be normalised apart.
_UNICODE_COMBINING = "café (e + U+0301) and niño (n + U+0303)"

# Legal in JSON, hostile to naive JS consumers; must survive untouched.
_JSON_HOSTILE_SEPARATORS = "nul=\x00 ls=" + chr(0x2028) + " ps=" + chr(0x2029) + " end"

# A body that is ITSELF two valid JSON event lines joined by newlines. A naive
# codec that concatenated the body raw into the log would smuggle a second
# (decodable) event line; the JSON string codec must escape the quotes and the
# newlines so the whole body stays inside one physical log line.
_INJECTION_LINE_A = (
    '{"id":"01AAAAAAAAAAAAAAAAAAAAAAAAAA","ts":"2026-08-11T09:00:00.000Z",'
    '"ticket":"smuggle","op":"set","actor":"evil","set":{"status":"done"}}'
)
_INJECTION_LINE_B = (
    '{"id":"02BBBBBBBBBBBBBBBBBBBBBBBBBB","ts":"2026-08-11T09:00:00.001Z",'
    '"ticket":"smuggle","op":"comment","actor":"evil","text":"pwned"}'
)
_INJECTION_BODY = _INJECTION_LINE_A + "\n" + _INJECTION_LINE_B + "\n"

# ~64 KB of mixed content: newlines, quotes, backslash, non-ASCII, control bytes.
_LONG_MIXED = 'line\nq" \\ é\U0001f389\t\r' * 4000

# Trailing whitespace and a trailing newline must not be silently stripped.
_TRAILING_WHITESPACE = "body that ends with trailing spaces   \n"


def _create_with_body(rohrpost_dir: Path, body: str, title: str = "body probe") -> str:
    """Create a ticket carrying *body* and return its bare id."""
    result = api.create_ticket(rohrpost_dir, title, body=body, actor="user/probe")
    return result.ticket.id


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_MARKDOWN, id="markdown_code_fence"),
        pytest.param(_JSON_ESCAPES, id="json_string_escapes"),
        pytest.param(_UNICODE_EMOJI_ZWJ, id="unicode_emoji_zwj"),
        pytest.param(_UNICODE_CJK, id="unicode_cjk"),
        pytest.param(_UNICODE_RTL, id="unicode_rtl"),
        pytest.param(_UNICODE_COMBINING, id="unicode_combining_marks"),
        pytest.param(_JSON_HOSTILE_SEPARATORS, id="json_hostile_separators"),
        pytest.param(_INJECTION_BODY, id="json_event_line_injection"),
        pytest.param(_LONG_MIXED, id="long_mixed_64kb"),
        pytest.param(_TRAILING_WHITESPACE, id="trailing_whitespace_and_newline"),
    ],
)
def test_corpus_body_round_trips_byte_identically(tmp_repo: Path, body: str) -> None:
    """Each adversarial body returns from show_ticket exactly as written."""
    ticket_id = _create_with_body(tmp_repo, body)
    assert api.show_ticket(tmp_repo, ticket_id).body == body


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@given(body=st.text(min_size=1, max_size=20000))
def test_arbitrary_text_body_round_trips(tmp_repo: Path, body: str) -> None:
    """Any non-whitespace-only text body round-trips through create + show.

    The function-scoped ``tmp_repo`` is reused across hypothesis examples; that
    is safe because each example allocates a distinct ticket id and only reads
    its own body back, so accumulated prior events cannot contaminate the fold.
    """
    assume(body.strip())  # whitespace-only bodies are intentionally dropped.
    ticket_id = _create_with_body(tmp_repo, body)
    assert api.show_ticket(tmp_repo, ticket_id).body == body


def test_whitespace_only_body_is_dropped(tmp_repo: Path) -> None:
    """A whitespace-only body is documented to never reach the log (body -> None)."""
    ticket_id = _create_with_body(tmp_repo, "  \n\t \r  ")
    assert api.show_ticket(tmp_repo, ticket_id).body is None


def test_json_event_injection_body_stays_one_log_line(tmp_repo: Path) -> None:
    """A body that is two valid JSON event lines must not smuggle a second line.

    The body is independently decodable as two ``Event`` lines joined by newlines
    (see ``_INJECTION_LINE_A``/``_INJECTION_LINE_B``), so a codec that wrote it
    raw would inject a spurious second event. The escaping codec must keep the
    whole body inside the single physical line of the create event.
    """
    assert "\n" in _INJECTION_BODY  # the body genuinely contains line breaks

    ticket_id = _create_with_body(tmp_repo, _INJECTION_BODY, title="injection probe")
    assert api.show_ticket(tmp_repo, ticket_id).body == _INJECTION_BODY

    # The body's internal newlines were escaped: exactly one physical log line.
    raw = paths.log_path(tmp_repo).read_text(encoding="utf-8")
    non_blank_physical_lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert len(non_blank_physical_lines) == 1

    # And decoding the log yields exactly one event for this ticket id.
    events = store.read_events(tmp_repo)
    matching = [e for e in events if e.ticket == ticket_id]
    assert len(matching) == 1
