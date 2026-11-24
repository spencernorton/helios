"""format_data caps what the Shared Context pane has to lay out.

The cap is a layout threshold, not a data limit — the entry on the scratchpad
service is untouched. These tests pin the cap on every branch, including the
json.dumps fallback, which is the one a naive implementation forgets.

GTK-free: helios.backend.scratchpad imports only json/os/urllib, so this runs
in the slim CI lane.
"""
from __future__ import annotations

import helios.backend.scratchpad as S


def test_short_string_passes_through_untouched() -> None:
    assert S.format_data("hello") == "hello"


def test_none_is_empty() -> None:
    assert S.format_data(None) == ""


def test_dict_still_pretty_prints() -> None:
    out = S.format_data({"a": 1})
    assert out == '{\n  "a": 1\n}'
    assert "not rendered here" not in out


def test_oversize_string_is_capped_and_says_so() -> None:
    payload = "x" * (200 * 1024)
    out = S.format_data(payload)
    dropped = len(payload) - S.MAX_DISPLAY_CHARS
    assert out.startswith("x" * S.MAX_DISPLAY_CHARS)
    assert f"({dropped} more characters not rendered here" in out
    assert out.endswith("use Copy full entry above)")
    assert len(out) < len(payload)


def test_unserialisable_payload_falls_back_to_str_and_is_still_capped() -> None:
    class Boom:
        def __repr__(self) -> str:
            return "b" * (200 * 1024)

        def __str__(self) -> str:
            return repr(self)

    # A dict with an unhashable-to-JSON KEY, not a set: json.dumps never
    # consults `default=` for keys, so this reaches the except branch rather
    # than being coerced — which is exactly why this test has teeth.
    # so this takes the except branch rather than the encoder's default hook.
    out = S.format_data({Boom(): 1})
    assert "more characters not rendered here" in out
    assert len(out) < 200 * 1024


def test_format_full_data_preserves_oversize_unicode_and_structured_values() -> None:
    payload = {
        "title": "héllo 🌍 日本語",
        "body": "z" * (200 * 1024),
    }
    full = S.format_full_data(payload)
    preview = S.format_data(payload)

    assert "héllo 🌍 日本語" in full
    assert "z" * (200 * 1024) in full
    assert "not rendered here" not in full
    assert len(full) > S.MAX_DISPLAY_CHARS
    assert len(preview) < len(full)
    assert preview.endswith("use Copy full entry above)")
