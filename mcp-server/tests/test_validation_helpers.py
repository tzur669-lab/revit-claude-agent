# -*- coding: utf-8 -*-
"""
Unit tests for the pure-Python helpers in revit_mcp/validation.py:
_match_room_type, _check_room_type, _check_extended (its non-DB branches),
and _load_rules. These touch no DB.* symbol at all except where noted, so
they are tested directly with plain data - no fake Revit environment needed
beyond the module import itself.

Deliberately locale-neutral: the fixtures below use a generic
"protected_room" example with English keywords, not any real jurisdiction's
room-type names or numbers - this engine has no jurisdiction of its own
(see validation.py's module docstring), and neither should its own test
suite. The project's actual (Israeli, private) rules file is proven
separately, live, against the real model - see docs/operation-contracts.md
and docs/architecture.md's Measured traps.

_check_extended's DB-touching branches (_bounding_wall_thicknesses_mm,
ceiling height via LookupParameter, volume via BuiltInParameter.ROOM_VOLUME)
call DB.SpatialElementBoundaryOptions etc. directly and were instead proven
live against the real model - building a collector/parameter shim just to
re-prove what live testing already proved would be exactly the general
Revit-API-shim overreach this project's mocking strategy avoids.
"""
import json

from conftest_revit_mcp import import_revit_mcp_module

validation = import_revit_mcp_module("validation")


ROOM_TYPES = [
    {"id": "toilet", "match_keywords": ["wc", "restroom"], "min_area_sqm": 1.1, "min_width_m": 0.80},
    {"id": "kitchen", "match_keywords": ["kitchen"], "min_area_sqm": 6.0, "min_width_m": 1.70},
    {
        "id": "protected_room",
        "match_keywords": ["shelter", "protected"],
        "extended_checks": {
            "net_area_sqm": 10.0,
            "relief_net_area_sqm": 6.0,
            "wall_thickness_mm": 350,
            "ceiling_height_m": [2.40, 2.70],
            "volume_cum": 20.0,
            "width_m": 1.50,
        },
    },
    {"id": "small_room", "match_keywords": ["room"], "min_area_sqm": 6.0, "min_width_m": 2.00},
]


# ---------------------------------------------------------------------------
# _match_room_type
# ---------------------------------------------------------------------------

def test_match_room_type_first_match_wins():
    """A generic keyword ('room') would also match a kitchen name if it came
    first - ordering in the rules file, not the matcher, is what keeps
    'kitchen' from being misclassified as 'small_room'. This test guards
    the matcher's own contract: first match in the list wins, full stop."""
    result = validation._match_room_type("Kitchen", ROOM_TYPES)
    assert result["id"] == "kitchen"


def test_match_room_type_no_match_returns_none():
    result = validation._match_room_type("Office", ROOM_TYPES)
    assert result is None


def test_match_room_type_empty_name_returns_none():
    assert validation._match_room_type("", ROOM_TYPES) is None
    assert validation._match_room_type(None, ROOM_TYPES) is None


def test_match_room_type_is_case_insensitive():
    """Hebrew has no case distinction, so this only matters for a
    Latin-script rules file - 'KITCHEN', 'Kitchen' and 'kitchen' must all
    match the same rule rather than silently depending on typed casing."""
    assert validation._match_room_type("KITCHEN 2", ROOM_TYPES)["id"] == "kitchen"
    assert validation._match_room_type("kitchen 2", ROOM_TYPES)["id"] == "kitchen"


def test_match_room_type_ordering_lets_a_more_specific_rule_win():
    """A name matching both 'protected_room' and the generic 'small_room'
    catch-all must resolve to the more specific rule, because it is listed
    first - this is exactly what lets a room get its extended_checks
    instead of only the generic area minimum. Order in the rules file is
    the whole mechanism; the matcher itself has no notion of specificity."""
    result = validation._match_room_type("Shelter Room 1", ROOM_TYPES)
    assert result["id"] == "protected_room"


# ---------------------------------------------------------------------------
# _check_room_type
# ---------------------------------------------------------------------------

def test_check_room_type_pass():
    rt, findings = validation._check_room_type("Kitchen", 12.58, ROOM_TYPES)
    assert rt["id"] == "kitchen"
    kinds = [f["kind"] for f in findings]
    assert "assumption" in kinds
    assert "pass" in kinds
    assert "violation" not in kinds


def test_check_room_type_violation_reports_measured_and_required():
    rt, findings = validation._check_room_type("Kitchen", 3.0, ROOM_TYPES)
    violation = next(f for f in findings if f["kind"] == "violation")
    assert violation["measured"] == 3.0
    assert violation["required"] == 6.0


def test_check_room_type_no_match_is_not_checked_not_silently_passed():
    """The central rule this whole engine exists to keep: a room type this
    ruleset doesn't recognize must report not_checked, never a fabricated
    pass just because nothing failed."""
    rt, findings = validation._check_room_type("Office", 8.0, ROOM_TYPES)
    assert rt is None
    assert len(findings) == 1
    assert findings[0]["kind"] == "not_checked"


def test_check_room_type_width_is_always_not_checked():
    """Width appears in the rule but this engine never claims to check it -
    every matched room type with a min_width_m must carry an explicit
    not_checked finding for it, not silence."""
    rt, findings = validation._check_room_type("Kitchen", 12.58, ROOM_TYPES)
    width_findings = [f for f in findings if f["category"] == "room_type_minimum_width"]
    assert len(width_findings) == 1
    assert width_findings[0]["kind"] == "not_checked"


def test_check_room_type_protected_room_has_no_generic_area_check():
    """protected_room defines only extended_checks, no min_area_sqm - the
    generic room-type-minimum check must not invent one; area for this room
    type is entirely _check_extended's job (net_area_sqm)."""
    rt, findings = validation._check_room_type("Shelter", 12.0, ROOM_TYPES)
    assert rt["id"] == "protected_room"
    categories = [f["category"] for f in findings]
    assert "room_type_minimum" not in [c for c, f in zip(categories, findings) if f["kind"] in ("pass", "violation")]


# ---------------------------------------------------------------------------
# _check_extended - non-DB branches only (net area / relief / width message)
# ---------------------------------------------------------------------------
# wall_thickness_mm / ceiling_height_m / volume_cum are deliberately absent
# from this fixture: those branches call _bounding_wall_thicknesses_mm,
# room.LookupParameter, and room.get_Parameter(DB.BuiltInParameter.ROOM_VOLUME)
# directly - the last of which needs a BuiltInParameter this project's
# narrow fake DB does not (and should not) provide. Proven live instead
# against the real model - see docs/operation-contracts.md.

EXTENDED_ROOM_TYPE = {
    "id": "protected_room",
    "match_keywords": ["shelter", "protected"],
    "extended_checks": {
        "net_area_sqm": 10.0,
        "relief_net_area_sqm": 6.0,
        "width_m": 1.50,
    },
}


def test_check_extended_no_extended_checks_is_a_silent_no_op():
    """A room type with no extended_checks at all (e.g. 'kitchen' above)
    must contribute nothing - not an empty finding, not a crash."""
    rt = {"id": "kitchen"}
    assert validation._check_extended(None, None, 12.0, rt) == []


def test_check_extended_area_pass():
    findings = validation._check_extended(None, None, 10.5, EXTENDED_ROOM_TYPE)
    area_findings = [f for f in findings if f["category"] == "protected_room_area" and f["kind"] in ("pass", "violation", "warning")]
    assert len(area_findings) == 1
    assert area_findings[0]["kind"] == "pass"
    assert area_findings[0]["measured"] == 10.5


def test_check_extended_area_warning_within_relief():
    findings = validation._check_extended(None, None, 8.0, EXTENDED_ROOM_TYPE)
    area_findings = [f for f in findings if f["category"] == "protected_room_area" and f["kind"] in ("pass", "violation", "warning")]
    assert area_findings[0]["kind"] == "warning"
    assert "relief" in area_findings[0]["message"]


def test_check_extended_area_violation_below_relief():
    findings = validation._check_extended(None, None, 3.0, EXTENDED_ROOM_TYPE)
    area_findings = [f for f in findings if f["category"] == "protected_room_area" and f["kind"] in ("pass", "violation", "warning")]
    assert area_findings[0]["kind"] == "violation"


def test_check_extended_category_names_are_built_from_rule_id():
    """Every finding's category is prefixed with the rule's own id, not a
    hardcoded jurisdiction-specific name - a rules file that calls this
    room type something else entirely gets matching category names for
    free, with no code change."""
    rt = dict(EXTENDED_ROOM_TYPE)
    rt["id"] = "storm_shelter"
    findings = validation._check_extended(None, None, 10.5, rt)
    assert any(f["category"] == "storm_shelter_area" for f in findings)
    assert any(f["category"] == "storm_shelter_width" for f in findings)


# ---------------------------------------------------------------------------
# _load_rules
# ---------------------------------------------------------------------------

def test_load_rules_missing_file_reports_honest_error():
    rules, err = validation._load_rules("Z:\\definitely\\does\\not\\exist.json")
    assert rules is None
    assert "not found" in err


def test_load_rules_valid_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps({"source": "test", "room_types": []}), encoding="utf-8")
    rules, err = validation._load_rules(str(p))
    assert err is None
    assert rules["source"] == "test"


def test_load_rules_malformed_json_reports_honest_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    rules, err = validation._load_rules(str(p))
    assert rules is None
    assert "could not be parsed" in err
