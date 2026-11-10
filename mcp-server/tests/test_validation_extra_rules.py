# -*- coding: utf-8 -*-
"""
Unit tests for revit_mcp/validation.py's _merge_extra_rules (Milestone 5 of
the M1-M5 architecture upgrade) - the vertical slice connecting the new
design-state work to a real Revit route: a project-specific constraint can
be checked per call via the request-scoped `extra_rules` body field, merged
over the loaded rules file by "id", never written to disk.

Deliberately locale-neutral, matching test_validation_helpers.py's own
convention: generic English "id"s, no real jurisdiction's numbers.
"""
from conftest_revit_mcp import import_revit_mcp_module

validation = import_revit_mcp_module("validation")

BASE = [
    {"id": "toilet", "match_keywords": ["wc"], "min_area_sqm": 1.1},
    {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 6.0},
]


def test_no_extra_rules_returns_base_unchanged():
    merged = validation._merge_extra_rules(BASE, None)
    assert merged == BASE


def test_empty_extra_rules_dict_returns_base_unchanged():
    merged = validation._merge_extra_rules(BASE, {})
    assert merged == BASE


def test_extra_rules_with_empty_room_types_returns_base_unchanged():
    merged = validation._merge_extra_rules(BASE, {"room_types": []})
    assert merged == BASE


def test_one_override_replaces_the_matching_base_entry():
    extra = {"room_types": [{"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 8.0}]}
    merged = validation._merge_extra_rules(BASE, extra)
    kitchen = next(rt for rt in merged if rt["id"] == "kitchen")
    assert kitchen["min_area_sqm"] == 8.0
    # toilet is untouched
    toilet = next(rt for rt in merged if rt["id"] == "toilet")
    assert toilet["min_area_sqm"] == 1.1
    assert len(merged) == 2


def test_multiple_overrides_all_apply():
    extra = {"room_types": [
        {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 8.0},
        {"id": "toilet", "match_keywords": ["wc"], "min_area_sqm": 1.5},
    ]}
    merged = validation._merge_extra_rules(BASE, extra)
    values = {rt["id"]: rt["min_area_sqm"] for rt in merged}
    assert values == {"kitchen": 8.0, "toilet": 1.5}


def test_new_project_specific_rule_is_added():
    extra = {"room_types": [{"id": "study", "match_keywords": ["study"], "min_area_sqm": 5.0}]}
    merged = validation._merge_extra_rules(BASE, extra)
    assert len(merged) == 3
    study = next(rt for rt in merged if rt["id"] == "study")
    assert study["min_area_sqm"] == 5.0


def test_duplicate_id_within_extra_rules_itself_last_one_wins():
    extra = {"room_types": [
        {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 7.0},
        {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 9.0},
    ]}
    merged = validation._merge_extra_rules(BASE, extra)
    kitchen = next(rt for rt in merged if rt["id"] == "kitchen")
    assert kitchen["min_area_sqm"] == 9.0
    assert len(merged) == 2  # not 3 - the duplicate id doesn't create two entries


def test_malformed_extra_rules_not_a_dict_degrades_to_base_unchanged():
    merged = validation._merge_extra_rules(BASE, ["not", "a", "dict"])
    assert merged == BASE


def test_malformed_extra_rules_room_types_not_a_list_degrades_to_base_unchanged():
    merged = validation._merge_extra_rules(BASE, {"room_types": "not a list"})
    assert merged == BASE


def test_entry_with_no_id_is_skipped_not_crashed_on():
    extra = {"room_types": [{"match_keywords": ["no id here"], "min_area_sqm": 3.0}]}
    merged = validation._merge_extra_rules(BASE, extra)
    assert merged == BASE  # the id-less entry contributes nothing, but nothing crashes


def test_precedence_extra_rules_wins_over_base_when_both_define_the_same_id():
    extra = {"room_types": [{"id": "toilet", "match_keywords": ["wc"], "min_area_sqm": 999.0}]}
    merged = validation._merge_extra_rules(BASE, extra)
    toilet = next(rt for rt in merged if rt["id"] == "toilet")
    assert toilet["min_area_sqm"] == 999.0


# ---------------------------------------------------------------------------
# Request isolation: extra_rules must never mutate the shared/global rules
# object, and must never leak into a later, unrelated call.
# ---------------------------------------------------------------------------

def test_base_list_object_is_not_mutated():
    original_len = len(BASE)
    original_first = dict(BASE[0])
    extra = {"room_types": [
        {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 8.0},
        {"id": "new_room", "match_keywords": ["new"], "min_area_sqm": 3.0},
    ]}
    validation._merge_extra_rules(BASE, extra)
    assert len(BASE) == original_len  # no entry was appended to the shared list
    assert BASE[0] == original_first  # no entry was mutated in place


def test_extra_rules_argument_is_not_mutated():
    extra = {"room_types": [{"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 8.0}]}
    import copy
    extra_copy = copy.deepcopy(extra)
    validation._merge_extra_rules(BASE, extra)
    assert extra == extra_copy


def test_extra_rules_from_one_call_does_not_leak_into_the_next():
    """Simulates two sequential calls sharing the same base rules object
    (as the real handler does, re-loading `rules` fresh each request but
    plausibly caching in a future optimization) - the first call's
    extra_rules must not still be applied on the second, plain call."""
    extra = {"room_types": [{"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 8.0}]}
    first = validation._merge_extra_rules(BASE, extra)
    assert next(rt for rt in first if rt["id"] == "kitchen")["min_area_sqm"] == 8.0

    second = validation._merge_extra_rules(BASE, None)  # a later call, no extra_rules
    assert next(rt for rt in second if rt["id"] == "kitchen")["min_area_sqm"] == 6.0  # back to base


def test_merge_returns_a_new_list_object_not_the_base_list_itself():
    merged = validation._merge_extra_rules(BASE, None)
    assert merged is not BASE
    assert merged == BASE
