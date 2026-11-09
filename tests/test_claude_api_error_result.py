"""A Claude turn the API refused must not read as a successful reply.

Measured on the live wire (claude 2.1.310, 2026-10-26) against an account
at its weekly limit: the CLI emits a `rate_limit_event` (status rejected),
a synthetic assistant record carrying the limit prose, and then a terminal

    {"type": "result", "subtype": "success", "is_error": true,
     "api_error": "usage_limit_reached", "api_error_status": 429,
     "terminal_reason": "api_error",
     "result": "You've hit your weekly limit · resets 3am (America/Denver)",
     "usage": {"input_tokens": 0, "output_tokens": 0, ...}}

`subtype` says success while `is_error`/`api_error` say the turn never ran.
Helios keyed terminal accounting off `subtype` alone and never rendered the
synthetic assistant record on the live path (canonical `assistant` records
are ignored in favour of stream deltas, and a refused turn produces none),
so the send appeared to complete instantly with no reply at all, and the
Work attempt was booked as a zero-token success.
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402


def _driver() -> ClaudeCliDriver:
    drv = ClaudeCliDriver.__new__(ClaudeCliDriver)
    ClaudeCliDriver.__init__(drv, cwd="/tmp")
    return drv


def _api_error_result(**extra) -> dict:
    rec = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "api_error": "usage_limit_reached",
        "api_error_status": 429,
        "terminal_reason": "api_error",
        "result": "You've hit your weekly limit · resets 3am (America/Denver)",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    rec.update(extra)
    return rec


def _wire(drv: ClaudeCliDriver) -> dict:
    seen: dict = {"closed": [], "errors": [], "results": []}
    drv._confirm_uncertain_delivery = lambda: None
    drv._finish_execution_with_evidence = lambda ev: (
        seen["closed"].append(ev),
        True,
    )[1]
    drv._flush_user_queue = lambda: None
    drv.connect("error", lambda _d, msg: seen["errors"].append(msg))
    drv.connect("result", lambda _d, obj: seen["results"].append(obj))
    return seen


def test_an_api_error_result_closes_the_attempt_as_failed() -> None:
    drv = _driver()
    drv._busy = True
    seen = _wire(drv)

    drv._dispatch_record(_api_error_result())

    assert [ev.status for ev in seen["closed"]] == ["failed"]
    assert drv._busy is False


def test_an_api_error_result_surfaces_its_prose_as_an_error() -> None:
    drv = _driver()
    drv._busy = True
    seen = _wire(drv)

    drv._dispatch_record(_api_error_result())

    assert seen["errors"] == [
        "You've hit your weekly limit · resets 3am (America/Denver)"
    ]
    # The ordinary result signal still fires so the window can clear busy
    # state and refresh the session list for the finished turn.
    assert len(seen["results"]) == 1


def test_an_api_error_without_prose_still_surfaces_a_reason() -> None:
    drv = _driver()
    drv._busy = True
    seen = _wire(drv)

    drv._dispatch_record(_api_error_result(result=""))

    assert seen["errors"], "a refused turn with no prose went silent"
    assert "usage_limit_reached" in seen["errors"][0]


def test_a_plain_success_result_stays_a_silent_completion() -> None:
    drv = _driver()
    drv._busy = True
    seen = _wire(drv)

    drv._dispatch_record(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )

    assert [ev.status for ev in seen["closed"]] == ["completed"]
    assert seen["errors"] == []
    assert len(seen["results"]) == 1
