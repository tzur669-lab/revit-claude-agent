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
