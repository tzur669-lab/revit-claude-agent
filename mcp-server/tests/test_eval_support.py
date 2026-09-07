# -*- coding: utf-8 -*-
"""
Offline tests for revit_mcp/eval_support.py -- zero Revit dependency (see
that module's own docstring), imported directly exactly as
revit_mcp/evaluators.py (IronPython) and this test (CPython) both do.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from revit_mcp import eval_support as es  # noqa: E402


# ---------------------------------------------------------------------------
# make_result -- ok is always derived from status, never independent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected_ok", [
    ("passed", True),
    ("failed", False),
    ("invalid_reference", None),
    ("wrong_category", None),
    ("missing_parameter", None),
    ("unsupported_type", None),
    ("unsupported_geometry", None),
    ("evaluator_error", None),
])
def test_make_result_ok_is_derived_from_every_status(status, expected_ok):
    r = es.make_result(status, expected=1, actual=1)
    assert r["ok"] is expected_ok
    assert r["status"] == status


def test_make_result_used_type_fallback_defaults_false_and_is_always_present():
    r = es.make_result("passed", 1, 1)
    assert r["used_type_fallback"] is False
    assert "used_type_fallback" in es.make_result("evaluator_error", None, None)


def test_make_result_rejects_unknown_status():
    with pytest.raises(AssertionError):
        es.make_result("not_a_real_status", 1, 1)


# ---------------------------------------------------------------------------
# compare -- numeric ops, tolerance, type-mismatch safety net, bool guard
# ---------------------------------------------------------------------------

def test_compare_numeric_gte_lte():
    assert es.compare(">=", 12.0, 10.0) is True
    assert es.compare(">=", 8.0, 10.0) is False
    assert es.compare("<=", 8.0, 10.0) is True
    assert es.compare("<=", 12.0, 10.0) is False


def test_compare_numeric_eq_uses_tolerance_boundary():
    tol = es.PARAM_DOUBLE_TOLERANCE
    assert es.compare("==", 10.0, 10.0) is True
    assert es.compare("==", 10.0 + tol, 10.0) is True  # exactly at the boundary
    assert es.compare("==", 10.0 + tol * 10, 10.0) is False
    assert es.compare("!=", 10.0 + tol * 10, 10.0) is True
    assert es.compare("!=", 10.0, 10.0) is False


def test_compare_int_and_float_mix_is_numeric():
    assert es.compare("==", 10, 10.0) is True
    assert es.compare(">=", 10, 9) is True


def test_compare_string_eq_ne_exact():
    assert es.compare("==", "Kitchen", "Kitchen") is True
    assert es.compare("==", "Kitchen", "Bedroom") is False
    assert es.compare("!=", "Kitchen", "Bedroom") is True
    assert es.compare("!=", "Kitchen", "Kitchen") is False


def test_compare_string_gte_is_unsupported_type():
    """The safety net: an operator that doesn't apply to the value's actual
    type returns None (-> status: unsupported_type at the call site),
    rather than raising or silently comparing lexicographically."""
    assert es.compare(">=", "Kitchen", "Bedroom") is None
    assert es.compare("<=", "Kitchen", "Bedroom") is None


def test_compare_numeric_vs_string_mismatch_is_none():
    assert es.compare("==", 10.0, "10.0") is None
    assert es.compare(">=", 10.0, "10.0") is None


def test_compare_bool_operands_are_rejected_even_though_bool_is_an_int():
    """isinstance(True, int) is True in Python -- compare() must not let a
    stray bool silently pass as a numeric 0/1 comparison."""
    assert es.compare("==", True, 1) is None
    assert es.compare(">=", False, 0) is None


def test_compare_unknown_operator_is_none():
    assert es.compare("~=", 1, 1) is None


def test_compare_never_raises_on_incomparable_types():
    assert es.compare(">=", None, 1) is None
    assert es.compare("==", [1, 2], [1, 2]) is True  # exact equality still works for equal lists
    assert es.compare(">=", object(), object()) is None


# ---------------------------------------------------------------------------
# aggregate_design_ok -- priority-ordered, deterministic
# ---------------------------------------------------------------------------

def _r(status):
    return es.make_result(status, expected=1, actual=1)


def test_aggregate_no_results_is_none():
    assert es.aggregate_design_ok([]) is None


def test_aggregate_all_passed_is_true():
    assert es.aggregate_design_ok([_r("passed"), _r("passed")]) is True


def test_aggregate_one_failed_is_false():
    assert es.aggregate_design_ok([_r("passed"), _r("failed")]) is False


def test_aggregate_failed_dominates_unknown():
    """A failed criterion plus an unrelated unevaluable one is False, not
    None -- the parts we COULD check already prove the design goal was not
    met, so it must not be softened to 'unknown' by an unrelated failure to
    evaluate something else."""
    assert es.aggregate_design_ok([_r("failed"), _r("invalid_reference")]) is False


def test_aggregate_unknown_among_passes_is_none_never_promoted_to_true():
    assert es.aggregate_design_ok([_r("passed"), _r("evaluator_error")]) is None
    assert es.aggregate_design_ok([_r("passed"), _r("missing_parameter")]) is None


@pytest.mark.parametrize("status", sorted(es.STATUSES - {"passed", "failed"}))
def test_aggregate_every_non_terminal_status_alone_is_none(status):
    assert es.aggregate_design_ok([_r(status)]) is None


# ---------------------------------------------------------------------------
# probe_outcome_from_universe -- Phase B slice 3's crash-resume identity
# rule. Never guesses: a false "applied" and a false "not_applied" are
# both unacceptable; only a conservative "unknown" is safe when the
# universe shows anything other than exactly one clean appearance.
# ---------------------------------------------------------------------------

def test_probe_no_capture_is_unknown():
    assert es.probe_outcome_from_universe(None, {"1": [0]}, set()) == ("unknown", None, "no_capture")


def test_probe_equal_universes_is_not_applied():
    before = {"1": [10, 20]}
    current = {"1": [10, 20]}
    assert es.probe_outcome_from_universe(before, current, set()) == ("not_applied", None, "no_relevant_change")


def test_probe_both_empty_is_not_applied():
    assert es.probe_outcome_from_universe({}, {}, set()) == ("not_applied", None, "no_relevant_change")


def test_probe_one_new_matching_is_applied_with_exact_id():
    before = {"1": [10, 20]}
    current = {"1": [10, 20], "2": [30, 40]}
    outcome, recovered, reason = es.probe_outcome_from_universe(before, current, {"2"})
    assert (outcome, recovered, reason) == ("applied", ["2"], "single_new_match")


def test_probe_one_new_not_matching_is_unknown():
    before = {"1": [10, 20]}
    current = {"1": [10, 20], "2": [30, 40]}
    assert es.probe_outcome_from_universe(before, current, set()) == (
        "unknown", None, "new_elements_none_matching")


def test_probe_two_new_matching_is_unknown_multiple_candidates():
    before = {}
    current = {"1": [10], "2": [20]}
    assert es.probe_outcome_from_universe(before, current, {"1", "2"}) == (
        "unknown", None, "multiple_new_candidates")


def test_probe_two_new_one_matching_is_still_multiple_candidates():
    """len(added) > 1 is checked before matching_ids -- a second,
    unrelated new element must not be waved through just because only one
    of the two new ids happens to match the request."""
    before = {}
    current = {"1": [10], "2": [20]}
    assert es.probe_outcome_from_universe(before, current, {"1"}) == (
        "unknown", None, "multiple_new_candidates")


def test_probe_pre_existing_element_deleted_is_unknown_even_with_one_new_match():
    """B1 regression guard: a captured element vanishing must force
    unknown even though a genuinely new, matching element also appeared --
    'exactly one thing changed' is violated by the disappearance alone."""
    before = {"1": [10, 20]}
    current = {"2": [30, 40]}
    assert es.probe_outcome_from_universe(before, current, {"2"}) == (
        "unknown", None, "relevant_model_change_ambiguous")


def test_probe_pre_existing_element_modified_is_unknown():
    """B1 regression guard: a surviving element whose fingerprint changed
    (it was independently modified, not newly created) must never be
    reported as 'no relevant change' -- it must force unknown even when
    NO new id appeared at all."""
    before = {"1": [10, 20]}
    current = {"1": [99, 99]}
    assert es.probe_outcome_from_universe(before, current, set()) == (
        "unknown", None, "relevant_model_change_ambiguous")


def test_probe_pre_existing_element_modified_into_a_match_is_not_reported_applied():
    """The precise B1 scenario: an existing element (not in 'matching_ids'
    at capture time, since it didn't match then) is modified so its id
    would now satisfy the request. It is still a SURVIVOR (present both
    before and after under the same id), never treated as new, so it can
    never be handed back as a recovered id."""
    before = {"1": [10, 20]}
    current = {"1": [99, 99]}
    assert es.probe_outcome_from_universe(before, current, {"1"}) == (
        "unknown", None, "relevant_model_change_ambiguous")


def test_probe_matching_id_that_is_not_new_does_not_count():
    """matching_ids is only ever consulted for ids that are actually new
    (added); an id already present in 'before' being passed in
    matching_ids by mistake must not manufacture an 'applied' out of a
    truly unchanged universe."""
    before = {"1": [10, 20]}
    current = {"1": [10, 20]}
    assert es.probe_outcome_from_universe(before, current, {"1"}) == (
        "not_applied", None, "no_relevant_change")


def test_probe_determinism_same_inputs_twice():
    before = {"1": [10, 20]}
    current = {"1": [10, 20], "2": [30, 40]}
    r1 = es.probe_outcome_from_universe(before, current, {"2"})
    r2 = es.probe_outcome_from_universe(before, current, {"2"})
    assert r1 == r2


def test_probe_reasons_frozenset_matches_every_actual_output():
    """Drift guard: one representative call per branch, collected and
    compared against the full published PROBE_REASONS set in BOTH
    directions -- a renamed/added/removed reason string in the function
    breaks this test rather than silently diverging from what's
    documented as the complete set."""
    seen = set()
    seen.add(es.probe_outcome_from_universe(None, {}, set())[2])
    seen.add(es.probe_outcome_from_universe({}, {}, set())[2])
    seen.add(es.probe_outcome_from_universe({"1": [1]}, {"1": [1], "2": [2]}, {"2"})[2])
    seen.add(es.probe_outcome_from_universe({"1": [1]}, {"1": [1], "2": [2]}, set())[2])
    seen.add(es.probe_outcome_from_universe({}, {"1": [1], "2": [2]}, {"1", "2"})[2])
    seen.add(es.probe_outcome_from_universe({"1": [1]}, {}, set())[2])
    assert seen == es.PROBE_REASONS


# ---------------------------------------------------------------------------
# endpoints_match -- bidirectional curve-endpoint matching with tolerance
# ---------------------------------------------------------------------------

def _pt(x, y, z=0):
    return {"x": x, "y": y, "z": z}


def test_endpoints_match_exact_same_direction():
    assert es.endpoints_match(_pt(0, 0), _pt(10, 0), _pt(0, 0), _pt(10, 0), 1.0) is True


def test_endpoints_match_reversed_direction():
    assert es.endpoints_match(_pt(0, 0), _pt(10, 0), _pt(10, 0), _pt(0, 0), 1.0) is True


def test_endpoints_match_within_tolerance():
    assert es.endpoints_match(_pt(0.5, 0), _pt(10, 0.5), _pt(0, 0), _pt(10, 0), 1.0) is True


def test_endpoints_match_just_outside_tolerance_on_x():
    assert es.endpoints_match(_pt(1.5, 0), _pt(10, 0), _pt(0, 0), _pt(10, 0), 1.0) is False


def test_endpoints_match_just_outside_tolerance_on_y():
    assert es.endpoints_match(_pt(0, 1.5), _pt(10, 0), _pt(0, 0), _pt(10, 0), 1.0) is False


def test_endpoints_match_just_outside_tolerance_on_z():
    assert es.endpoints_match(_pt(0, 0, 1.5), _pt(10, 0), _pt(0, 0), _pt(10, 0), 1.0) is False


def test_endpoints_match_missing_axis_defaults_to_zero():
    assert es.endpoints_match({"x": 0, "y": 0}, {"x": 10, "y": 0}, _pt(0, 0), _pt(10, 0), 1.0) is True


def test_endpoints_match_totally_different_curve_is_false():
    assert es.endpoints_match(_pt(0, 0), _pt(10, 0), _pt(500, 500), _pt(600, 600), 1.0) is False


# ---------------------------------------------------------------------------
# point_match -- axis-scoped tolerance (the hosted-instance z-only case)
# ---------------------------------------------------------------------------

def test_point_match_xyz_within_tolerance():
    assert es.point_match(_pt(1, 1, 1), _pt(0, 0, 0), 2.0, "xyz") is True


def test_point_match_xyz_just_outside_tolerance():
    assert es.point_match(_pt(3, 0, 0), _pt(0, 0, 0), 2.0, "xyz") is False


def test_point_match_z_only_ignores_xy_drift():
    """The hosted-instance case: a family instance hosted on a wall
    legitimately projects X/Y onto the host, so only Z should be checked."""
    assert es.point_match(_pt(500, 500, 1), _pt(0, 0, 0), 2.0, "z") is True


def test_point_match_z_only_still_rejects_z_drift():
    assert es.point_match(_pt(0, 0, 5), _pt(0, 0, 0), 2.0, "z") is False
