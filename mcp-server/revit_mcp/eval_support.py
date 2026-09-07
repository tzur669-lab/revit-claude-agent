# -*- coding: utf-8 -*-
"""
Revit-free core of the success_criteria evaluator system (Phase B, slice 1).

Zero Revit-API dependency, on purpose -- importable identically from CPython
(offline tests, this repo's pytest suite) and IronPython (evaluators.py,
running inside Revit). Same pattern this repo already uses for exactly this
reason: revit_mcp/task_store.py (zero Revit dependency, imported by both
runtimes) and tools/ops_schema.py (ops.py's data-only CPython twin). This is
the one piece of the evaluator system that doesn't need `doc`/`DB` at all --
pulling it out here is what makes real offline unit tests on the comparison
and aggregation logic possible, instead of asserting it by prose.

Result contract (`make_result`): every criterion evaluation produces exactly
one of 8 named statuses, never a bare pass/fail hiding the reason in free
text:

    passed              -- the criterion holds.
    failed              -- the criterion does not hold (a confirmed violation).
    invalid_reference   -- a target id doesn't resolve, or the wrong number
                           of target ids was given.
    wrong_category      -- a target resolved but is not the required kind
                           of element (e.g. room_area_sqm on a non-Room).
    missing_parameter   -- param_value's named parameter was not found.
    unsupported_type    -- the operator does not apply to the actual value's
                           type (e.g. ">=" against a String).
    unsupported_geometry -- distance_mm on an element with no LocationPoint.
    evaluator_error     -- the evaluator itself raised an unexpected
                           exception -- a real bug, never spelled the same
                           way as a legitimate business outcome above.

`ok` is DERIVED from `status` (never set independently), so the two can
never disagree: {"passed": True, "failed": False}, everything else None.
None means "not conclusively checked" -- it is never promoted to True by
`aggregate_design_ok`, matching this project's own "unknown must never be
upgraded to a confident answer" rule (see tracker.attribute()'s identical
philosophy, memory: revit-mcp-verification-and-analysis).
"""

# Intentionally duplicated from revit_mcp/utils.py's PARAM_DOUBLE_TOLERANCE
# (same value, 1e-6) rather than imported -- utils.py does `from pyrevit
# import DB` at module scope, so importing it here would make this module
# Revit-dependent and defeat the entire point of eval_support.py existing.
# Keep this value in sync with utils.py by hand if either ever changes.
PARAM_DOUBLE_TOLERANCE = 1e-6

STATUSES = frozenset([
    "passed", "failed", "invalid_reference", "wrong_category",
    "missing_parameter", "unsupported_type", "unsupported_geometry",
    "evaluator_error",
])

# Every status not listed here maps to ok=None by omission (dict.get default).
_STATUS_OK = {"passed": True, "failed": False}

_COMPARABLE_NUMERIC = (int, float)  # bool is deliberately excluded -- see compare()


def make_result(status, expected, actual, reason=None, used_type_fallback=False):
    """The one, uniform shape every evaluator returns. `ok` is always derived
    from `status`, never passed in independently, so the two fields can
    never contradict each other. `used_type_fallback` defaults False and is
    always present (not just for param_value) so a consumer can rely on the
    key's presence across every evaluator's result without a None-vs-key-
    missing distinction."""
    assert status in STATUSES, "unknown status %r" % (status,)
    return {
        "status": status,
        "ok": _STATUS_OK.get(status),
        "expected": expected,
        "actual": actual,
        "reason": reason,
        "used_type_fallback": bool(used_type_fallback),
    }


def compare(op, actual, expected):
    """True / False, or None if the comparison cannot be made -- NEVER
    guesses. Numeric '==' / '!=' use PARAM_DOUBLE_TOLERANCE, matching
    utils.param_read_matches's own existing Double-comparison convention
    exactly (both sides of a value this project already writes and reads
    back should agree on what "equal" means). Exact equality otherwise, but
    ONLY when actual and expected are the same Python type (strings against
    strings, element-id ints against element-id ints) -- a float actual
    compared against a string expected (or any other type mismatch) returns
    None rather than Python's own permissive `10.0 == "10.0"` (False, not an
    error): a type mismatch means the criterion is malformed for this
    target, not that it definitively fails.

    A bool is deliberately rejected (returns None) even though Python's
    isinstance(True, int) is True -- no evaluator in this system produces a
    bool on purpose, so a stray one is treated as a type mismatch, not
    silently compared as 0/1.

    ">=" / "<=" against two non-numeric operands is not meaningful (there is
    no total order defined here for strings/ids) and returns None -- this is
    the safety net that lets a mismatched criterion (e.g. ">=" against a
    String parameter) fail safely with status "unsupported_type" instead of
    raising or silently passing."""
    try:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return None
        numeric = isinstance(actual, _COMPARABLE_NUMERIC) and isinstance(expected, _COMPARABLE_NUMERIC)
        if op == ">=":
            return (actual >= expected) if numeric else None
        if op == "<=":
            return (actual <= expected) if numeric else None
        if op == "==":
            if numeric:
                return abs(float(actual) - float(expected)) <= PARAM_DOUBLE_TOLERANCE
            if type(actual) is type(expected):
                return actual == expected
            return None  # mismatched non-numeric types, e.g. a float actual vs a str expected
        if op == "!=":
            if numeric:
                return not (abs(float(actual) - float(expected)) <= PARAM_DOUBLE_TOLERANCE)
            if type(actual) is type(expected):
                return actual != expected
            return None
    except Exception:
        return None
    return None


PROBE_REASONS = frozenset([
    "no_capture", "relevant_model_change_ambiguous", "no_relevant_change",
    "single_new_match", "new_elements_none_matching", "multiple_new_candidates",
])


def probe_outcome_from_universe(before_map, current_map, matching_ids):
    """(outcome, recovered_ids, reason) -- Phase B slice 3's crash-resume
    identity rule for the 3 creating ops with no Revit built-in
    point/curve lookup of their own (create_room_separation,
    create_line_based_element, place_family), and the fixed version of
    create_room's own probe (see docs/architecture.md's Phase B slice 3
    section for why the original single-id `current_id == was_id` test
    was not safe to generalize: it cannot tell a newly CREATED element
    from a pre-existing one that was independently MODIFIED into
    matching, since only elements that already matched were ever in the
    'before' picture).

    before_map / current_map are {str(element_id): fingerprint} snapshots
    of the SAME type-scoped universe (every element of the relevant
    category+type, not just ones matching the request) taken at capture
    time and probe time respectively. Fingerprints are already quantized
    identically by the caller (ops.py) -- this function does exact `!=`,
    never a tolerance comparison, so the physical tolerance decision is
    made exactly once, at the point the fingerprint is built.
    matching_ids is the subset of current_map's keys whose CURRENT
    fingerprint independently satisfies the op's own operation-controlled
    identity test against what THIS action actually requested.

    This is a bounded heuristic, not proof of causation (see
    docs/architecture.md's residual-risk note) -- 'applied' is returned
    only when the universe shows EXACTLY one relevant change and that
    change is consistent with the request:

      before_map is None                                  -> unknown, "no_capture"
      an id present in before_map is missing from current  -> unknown, "relevant_model_change_ambiguous"
      an id present in both with a DIFFERENT fingerprint    -> unknown, "relevant_model_change_ambiguous"
      (only once the universe is otherwise UNCHANGED do we look at what's new)
      no new ids                                          -> not_applied, "no_relevant_change"
      exactly one new id, and it is in matching_ids        -> applied, [that id], "single_new_match"
      exactly one new id, not in matching_ids              -> unknown, "new_elements_none_matching"
      more than one new id                                -> unknown, "multiple_new_candidates"

    Never guesses: 'applied' and a recovered id are established by the
    same single fact and are never returned apart. A false 'not_applied'
    is exactly as unacceptable as a false 'applied' here -- both are
    guarded by requiring the *entire* survivor set to be byte-identical
    before 'no new ids' is even considered a conclusion rather than a
    coincidence of an otherwise-changed universe."""
    if before_map is None:
        return "unknown", None, "no_capture"
    before_ids = set(before_map.keys())
    current_ids = set(current_map.keys())
    if not before_ids.issubset(current_ids):
        return "unknown", None, "relevant_model_change_ambiguous"
    for eid in before_ids:
        if current_map.get(eid) != before_map.get(eid):
            return "unknown", None, "relevant_model_change_ambiguous"
    added = current_ids - before_ids
    if not added:
        return "not_applied", None, "no_relevant_change"
    if len(added) > 1:
        return "unknown", None, "multiple_new_candidates"
    only_new = next(iter(added))
    if only_new in (matching_ids or ()):
        return "applied", [only_new], "single_new_match"
    return "unknown", None, "new_elements_none_matching"


def endpoints_match(a_start_mm, a_end_mm, b_start_mm, b_end_mm, tolerance_mm):
    """True if the two segments coincide within tolerance_mm, checked in
    BOTH point orderings -- a curve and its reverse describe the same
    wall/room-separation-line geometrically. mm dicts: {"x":.., "y":..,
    "z":..}, a missing axis defaults to 0 (matching ops.py's own existing
    _wall_curve_verify convention). Unlike that function (which checks
    one fixed, requested orientation on purpose), this is for matching an
    UNORDERED pair, so both directions are accepted."""
    def _close(p, q):
        return abs(p.get("x", 0) - q.get("x", 0)) <= tolerance_mm \
            and abs(p.get("y", 0) - q.get("y", 0)) <= tolerance_mm \
            and abs(p.get("z", 0) - q.get("z", 0)) <= tolerance_mm
    same_dir = _close(a_start_mm, b_start_mm) and _close(a_end_mm, b_end_mm)
    reversed_dir = _close(a_start_mm, b_end_mm) and _close(a_end_mm, b_start_mm)
    return same_dir or reversed_dir


def point_match(a_mm, b_mm, tolerance_mm, axes="xyz"):
    """True if point a is within tolerance_mm of point b on the given
    axes only. axes="z" is place_family's hosted-instance case, mirroring
    placement.verify_place_family's own existing rule: a hosted instance
    legitimately projects X/Y onto its host, so only Z is a meaningful
    identity check there."""
    for axis in axes:
        if abs(a_mm.get(axis, 0) - b_mm.get(axis, 0)) > tolerance_mm:
            return False
    return True


def aggregate_design_ok(results):
    """[criterion-result-dict, ...] -> True / False / None. Pure, and unit-
    tested directly here -- fold_progress() in task_store.py calls this same
    function rather than re-deriving the rule inline, so there is exactly
    one place the aggregation policy lives, nothing to drift apart.

    Rule, in priority order:
      - no results at all (nothing declared, or not yet evaluated) -> None.
      - any result with status == "failed" present -> False. A confirmed
        violation is definitive and DOMINATES an unrelated unknown -- a plan
        with one failed criterion and one criterion that could not be
        checked is reported False, not None, because the parts we COULD
        check already prove the design goal was not met.
      - else, any status other than "passed" present (including
        "evaluator_error") -> None. Never promoted to True by other
        criteria passing -- an unresolved criterion means the overall
        picture is incomplete, not that it succeeded.
      - only if every result is "passed" -> True.
    """
    if not results:
        return None
    statuses = [r.get("status") for r in results]
    if "failed" in statuses:
        return False
    if any(s != "passed" for s in statuses):
        return None
    return True
