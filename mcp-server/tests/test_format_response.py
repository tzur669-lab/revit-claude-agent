# -*- coding: utf-8 -*-
"""
Unit tests for format_response() (tools/utils.py) - Milestone 1's fix for
the single largest gap the M1-M5 architecture upgrade found: a successful
response used to surface exactly one key (message > output > result > data
> the status=="active" branch > an else-dump) and silently drop every
other key. That made validate_design / analyze_relationships /
preview_delete_impact effectively unusable - their entire "results"
payload never reached the caller - and made execute_revit_code's
tx_status/verified (its only evidence anything happened) invisible on
every successful call.

These tests prove:
  - the headline precedence order is fixed and does not depend on dict
    insertion order, even when a response carries several headline
    candidates at once;
  - every key NOT chosen as the headline survives into "=== DATA ===";
  - the verification suffix is now attached regardless of which headline
    key won (previously only the "message" branch got it);
  - truncation is machine-detectable and never lets a cut-off list look
    complete;
  - Hebrew survives both in the headline and inside nested JSON data;
  - a non-200 response that now arrives as a structured dict (main.py's
    _revit_call change) is never misread as success, and the TRACKER_OK
    protocol string still survives in what the tool returns;
  - every branch this file did NOT change (get_revit_status's custom
    formatter, the else-dump branch place_family depends on, plain string
    passthrough) is unaffected.
"""
import os
import sys

# tools/ is a proper package (has __init__.py) but tests/ has no
# __init__.py, so pytest's default import mode only prepends tests/ itself
# onto sys.path, not its parent - mcp-server/ needs to be added explicitly
# to import "tools.utils" by its real package path. tools/__init__.py has
# no top-level side-effect imports, so this stays a lightweight import.
_MCP_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MCP_SERVER_DIR not in sys.path:
    sys.path.insert(0, _MCP_SERVER_DIR)

from tools.utils import format_response, MAX_DATA_CHARS


# ---------------------------------------------------------------------------
# Headline precedence - deterministic, not dict-order-dependent
# ---------------------------------------------------------------------------

def test_headline_precedence_message_wins_over_everything():
    # Deliberately inserted in an order that would pick "data" first if the
    # implementation iterated response.keys() instead of the fixed order.
    response = {
        "data": "should not win",
        "result": "should not win either",
        "output": "not this one",
        "message": "THE HEADLINE",
        "status": "success",
    }
    out = format_response(response)
    assert out.startswith("THE HEADLINE")


def test_headline_precedence_output_wins_when_no_message():
    response = {"data": "no", "result": "no", "output": "THE HEADLINE", "status": "success"}
    out = format_response(response)
    assert out.startswith("THE HEADLINE")


def test_headline_precedence_result_wins_when_no_message_or_output():
    response = {"data": "no", "result": "THE HEADLINE", "status": "success"}
    out = format_response(response)
    assert out.startswith("THE HEADLINE")


def test_headline_precedence_data_wins_when_nothing_else_present():
    response = {"data": {"a": 1}, "status": "success"}
    out = format_response(response)
    assert out.startswith(str({"a": 1}))


def test_losers_move_into_data_block_not_discarded():
    """The core regression test for the bug: every key that did NOT win
    the headline must still be visible in the response somewhere."""
    response = {
        "message": "headline",
        "output": "lost-before,-must-survive-now",
        "result": "also-must-survive",
        "status": "success",
    }
    out = format_response(response)
    assert out.startswith("headline")
    assert "=== DATA ===" in out
    assert "lost-before,-must-survive-now" in out
    assert "also-must-survive" in out


# ---------------------------------------------------------------------------
# The actual reported defect: validate_design / analyze_relationships /
# preview_delete_impact drop their entire payload
# ---------------------------------------------------------------------------

def test_validate_design_shaped_response_surfaces_results():
    response = {
        "status": "success",
        "message": "Checked 16 rooms: 3 violations, 1 warning",
        "results": [
            {"room_id": 101, "room_name": "Room A", "findings": [{"kind": "violation"}]},
        ],
        "violation_count": 3,
        "warning_count": 1,
    }
    out = format_response(response)
    assert out.startswith("Checked 16 rooms: 3 violations, 1 warning")
    assert "=== DATA ===" in out
    assert "results" in out
    assert "room_id" in out
    assert "101" in out
    assert "violation_count" in out


def test_preview_delete_impact_shaped_response_surfaces_ids():
    response = {
        "status": "success",
        "message": "Would delete 3 elements (2 cascaded)",
        "would_delete_ids": [1, 2, 3],
        "cascaded_ids": [4, 5],
        "affected": {"walls": 2},
        "per_requested_element": [{"id": 1, "category": "Walls"}],
    }
    out = format_response(response)
    assert "would_delete_ids" in out
    for value in ("1", "2", "3", "4", "5"):
        assert value in out
    assert "per_requested_element" in out


# ---------------------------------------------------------------------------
# Verification suffix now applies to every branch, not just "message"
# ---------------------------------------------------------------------------

def test_verification_suffix_attached_to_output_branch():
    """execute_revit_code's success payload only ever carries "output", never
    "message" - previously that meant tx_status/verified never reached the
    caller at all. This is the fix."""
    response = {
        "status": "success",
        "output": "printed stuff",
        "tx_status": "Committed",
        "tx_ok": True,
        "verified": {
            "ok": None,
            "status": "not_checked",
            "reason": "execute_revit_code runs arbitrary code with no fixed contract",
        },
    }
    out = format_response(response)
    assert out.startswith("printed stuff")
    assert "tx_status: Committed" in out
    assert "verified.ok: None" in out
    assert "reason: execute_revit_code runs arbitrary code" in out
    # tx_status/tx_ok/code_executed are rendered by the suffix (or are pure
    # noise) - they must not ALSO be duplicated into the DATA block.
    assert "=== DATA ===" not in out


def test_verified_ok_false_appears_in_both_suffix_and_data_block():
    response = {
        "status": "success",
        "message": "created 2 of 3 elements",
        "tx_status": "Committed",
        "verified": {
            "ok": False,
            "method": "element_category",
            "expected": {"count": 3},
            "actual": {"count_ok": 2},
            "failures": [{"id": 99, "reason": "does not resolve after commit"}],
        },
    }
    out = format_response(response)
    assert "verified.ok: False" in out
    assert "failures: 1" in out
    assert "=== DATA ===" in out
    # The per-element failure detail belongs in the data block since the
    # suffix only ever gives a count, not the reasons.
    assert "does not resolve after commit" in out


def test_verified_ok_true_not_duplicated_into_data_block():
    response = {
        "status": "success",
        "message": "created 3 elements",
        "tx_status": "Committed",
        "verified": {"ok": True, "method": "element_category",
                     "expected": {"count": 3}, "actual": {"count_ok": 3}},
        "created_ids": [1, 2, 3],
    }
    out = format_response(response)
    assert "verified.ok: True" in out
    assert "=== DATA ===" in out
    assert "created_ids" in out
    # The passing verified dict itself should not be re-dumped in DATA.
    assert '"method"' not in out


# ---------------------------------------------------------------------------
# Truncation must be machine-detectable, never silently incomplete
# ---------------------------------------------------------------------------

def test_truncation_marks_itself_explicitly():
    big_list = list(range(5000))
    response = {"status": "success", "message": "big", "huge_field": big_list}
    out = format_response(response)
    assert "DATA_TRUNCATED: true" in out
    assert "huge_field" in out  # key name survives even though the body is cut
    assert "list_lengths" in out
    assert "5000" in out  # the TRUE length is reported, not just what's shown
    assert "INCOMPLETE" in out


def test_small_data_block_has_no_truncation_marker():
    response = {"status": "success", "message": "small", "field": [1, 2, 3]}
    out = format_response(response)
    assert "DATA_TRUNCATED" not in out


def test_truncation_lists_every_key_present_even_when_cut():
    response = {
        "status": "success",
        "message": "big",
        "small_field": "x",
        "huge_field": "y" * (MAX_DATA_CHARS * 2),
    }
    out = format_response(response)
    assert "DATA_TRUNCATED: true" in out
    assert "small_field" in out
    assert "huge_field" in out


# ---------------------------------------------------------------------------
# Hebrew must survive, in the headline and inside nested JSON data
# ---------------------------------------------------------------------------

def test_hebrew_in_headline_survives():
    response = {"status": "success", "message": "נוצרו 3 קירות בהצלחה"}
    out = format_response(response)
    assert "נוצרו 3 קירות בהצלחה" in out


def test_hebrew_inside_nested_data_survives_not_escaped():
    response = {
        "status": "success",
        "message": "done",
        "rooms": [{"room_name": "חדר שינה ראשי"}],
    }
    out = format_response(response)
    assert "חדר שינה ראשי" in out
    # ensure_ascii=False means this must NOT show up as a \uXXXX escape.
    assert "\\u" not in out


# ---------------------------------------------------------------------------
# _http_status: a non-200 structured dict must never be read as success
# ---------------------------------------------------------------------------

def test_http_status_500_is_always_error_even_with_success_shaped_status():
    response = {"status": "success", "message": "looks fine", "_http_status": 500}
    out = format_response(response)
    assert "=== ERROR DETAILS ===" in out
    assert "HTTP Status: 500" in out


def test_http_status_200_does_not_force_error():
    response = {"status": "success", "message": "fine", "_http_status": 200}
    out = format_response(response)
    assert "=== ERROR DETAILS ===" not in out
    assert out.startswith("fine")


def test_http_status_never_leaks_into_data_block_or_additional_response_data():
    response = {"status": "success", "message": "fine", "_http_status": 200, "extra": 1}
    out = format_response(response)
    assert "_http_status" not in out


def test_tracker_ok_substring_survives_through_error_formatting():
    """The revit-session skill's protocol: a tracker checkpoint ends by
    raising TrackerOK, which code_execution.py's exception handler turns
    into a structured 500 body with error="TrackerOK: TRACKER_OK|<run_id>".
    Whatever text format_response ultimately produces, that literal
    substring must still be present and findable - the skill greps for it
    and must never retry a TRACKER_OK 500."""
    response = {
        "status": "error",
        "error": "TrackerOK: TRACKER_OK|r-20260901-120000-abcd",
        "error_type": "TrackerOK",
        "traceback": "Traceback (most recent call last):\n...",
        "code_attempted": "execfile(...); main(...)",
        "_http_status": 500,
    }
    out = format_response(response)
    assert "TRACKER_OK|r-20260901-120000-abcd" in out


# ---------------------------------------------------------------------------
# Branches this milestone did NOT change must be unaffected
# ---------------------------------------------------------------------------

def test_status_active_branch_unchanged():
    response = {
        "status": "active",
        "health": "ok",
        "api_name": "revit_mcp",
        "document_title": "test.rvt",
        "revit_available": True,
    }
    out = format_response(response)
    assert out.startswith("=== REVIT STATUS ===")
    assert "Status: active" in out
    assert "Health: ok" in out
    assert "API: revit_mcp" in out


def test_else_dump_branch_unchanged_for_structured_payload_with_no_wrapper_key():
    """place_family's success payload has no message/output/result/data key
    at all - it must keep going through the pre-existing else-dump
    formatter, not the new headline/DATA-block path."""
    response = {"element_id": 12345, "family_name": "Chair", "location": {"x": 1, "y": 2}}
    out = format_response(response)
    assert "=== DATA ===" not in out
    assert "Element Id: 12345" in out
    assert "Family Name: Chair" in out


def test_string_passthrough_unchanged():
    assert format_response("Error: 500 - something broke") == "Error: 500 - something broke"


def test_error_branch_still_shows_traceback_and_additional_data():
    response = {
        "status": "error",
        "error": "boom",
        "traceback": "Traceback...\nValueError: boom",
        "some_extra_field": "still visible",
    }
    out = format_response(response)
    assert "=== ERROR DETAILS ===" in out
    assert "Error: boom" in out
    assert "=== TRACEBACK ===" in out
    assert "=== ADDITIONAL RESPONSE DATA ===" in out
    assert "still visible" in out


def test_json_serialization_falls_back_gracefully_for_unserializable_values():
    class Weird(object):
        def __str__(self):
            return "weird-repr"

    response = {"status": "success", "message": "ok", "thing": Weird()}
    out = format_response(response)
    assert "weird-repr" in out
