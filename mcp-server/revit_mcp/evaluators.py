# -*- coding: UTF-8 -*-
"""
Phase B, slice 1: executes a plan's declared success_criteria against real,
post-execution model state. Read-only -- no evaluator opens a transaction,
mutates the document, or touches selection/view state
(tests/test_evaluators_no_transactions.py is an AST guard for exactly this,
the same pattern tests/test_ops_no_transactions.py already uses for ops.py).

Called exactly once per task, from plan_exec.py's execute_plan_handler,
immediately after _run_slice() returns outcome "completed" and its
TransactionGroup has already Assimilate()d -- `doc` is the fully-
regenerated, final committed state at that point, the same assumption every
other post-commit verify() in this codebase already makes. Never per-slice:
success_criteria describe the plan's END STATE, not one budgeted chunk of
it. The existing "task already finished" short-circuit at the top of
execute_plan_handler means this naturally runs exactly once -- no
idempotency work needed here.

Each evaluator is `(doc, criterion, created_by_action) -> eval_support.
make_result(...)`, one of 8 named statuses (see eval_support.py's own
docstring for the full vocabulary and why). element_exists/element_absent
are thin wrappers around the ALREADY-SHIPPED utils.verify_elements_exist
and editing.verify_delete (the latter's `method` is already the literal
string "element_absent") -- translated into the 8-status contract, not
reimplemented. room_area_sqm, param_value, and distance_mm are new.

Phase B, slice 2: a criterion's target(s) can now also reference an
element a plan's OWN action created (`_resolve_criterion_targets`, below)
-- see that function's docstring and docs/architecture.md's Phase B
section for the full design (the `created_by` target kind, the plural
`targets` field for composed references, and why the result-status
vocabulary stayed unchanged rather than growing new statuses for this).

Known, disclosed limitations (see the approved plan, not solved here):
  - param_value performs NO unit conversion -- the criterion's "value" must
    already be in Revit's raw internal units, identically to
    set_parameters' own existing contract (see docs/operation-contracts.md
    and utils.read_param_native's docstring).
  - distance_mm only supports elements with a real DB.LocationPoint
    (point-placed families, Rooms). A bounding-box-center fallback was
    deliberately rejected during design review: a bbox is an axis-aligned
    artifact with no consistent physical meaning across categories (an
    L-shaped room's own bbox center can sit outside the room), so an
    element without a LocationPoint (walls, curve-based elements) returns
    status "unsupported_geometry", never an approximated number.
  - a `created_by` reference to a creating action that was resumed via
    probe-skip after a crash resolves to `invalid_reference` UNLESS that
    op's probe could conclusively recover the created id (today: only
    create_room's probe can, via GetRoomAtPoint -- the other three
    creating ops have no membership test at all yet, a pre-existing,
    disclosed Phase A limitation).
"""
from pyrevit import DB

import editing
import eval_support
import utils
from refs import resolve_ref
from utils import get_element_id_value, make_element_id, resolve_param, read_param_native

FEET_TO_MM = 304.8  # matches ops.FEET_TO_MM exactly; not imported from ops
                     # to avoid a two-module import cycle (ops.py does not
                     # import evaluators.py, and should not need to).

_OST_ROOMS_CATEGORY_ID = None  # resolved lazily -- DB is only real inside Revit


def _ost_rooms_category_id():
    global _OST_ROOMS_CATEGORY_ID
    if _OST_ROOMS_CATEGORY_ID is None:
        _OST_ROOMS_CATEGORY_ID = get_element_id_value(DB.ElementId(DB.BuiltInCategory.OST_Rooms))
    return _OST_ROOMS_CATEGORY_ID


def _is_room(elem):
    """Category check, NOT isinstance -- matches this project's own already-
    used convention (validation.py/impact.py collect rooms by
    OfCategory(OST_Rooms), never by isinstance). Uses get_element_id_value,
    NOT raw .IntegerValue, per this project's own already-learned Revit
    2024-2027 rule (the exact bug Phase A's transforms.py extraction had to
    fix at three sites)."""
    try:
        cat = elem.Category
        return cat is not None and get_element_id_value(cat.Id) == _ost_rooms_category_id()
    except Exception:
        return False


def _location_point(elem):
    """The element's DB.LocationPoint.Point, or None if it has none (a
    curve-based element, or no Location at all). Never guesses a
    substitute -- see module docstring on why a bbox-center fallback was
    rejected."""
    try:
        loc = elem.Location
    except Exception:
        return None
    if not isinstance(loc, DB.LocationPoint):
        return None
    try:
        return loc.Point
    except Exception:
        return None


def _resolve_one_ref(doc, ref, created_by_action):
    """(ids_or_None, reason_or_None) for ONE single-source reference --
    either the whole `target` field (kind "element": may be a batch of
    many ids, unchanged since slice 1; kind "created_by": always exactly
    one) or one entry of the plural `targets` list. Never raises, never
    guesses -- an unresolvable created_by reference gets a reason specific
    enough to read directly (which action, why), matching this project's
    "no success without evidence" rule rather than a bare invalid_reference
    with no detail."""
    if not isinstance(ref, dict):
        return None, "target is not an object"
    kind = ref.get("kind")
    if kind == "element":
        try:
            return resolve_ref(doc, ref), None
        except Exception as e:
            return None, "target did not resolve: %s" % e
    if kind == "created_by":
        action_i = ref.get("action_i")
        ids = (created_by_action or {}).get(action_i)
        if not ids:
            return None, (
                "action %r produced no output - either it never ran, or its creating op's "
                "probe could not recover an id after a crash-resume skip" % (action_i,)
            )
        index = ref.get("index", 0)
        if index >= len(ids):
            return None, ("index %d out of range: action %r produced %d output(s)"
                           % (index, action_i, len(ids)))
        try:
            return [make_element_id(ids[index])], None
        except Exception as e:
            return None, "could not build an element reference: %s" % e
    return None, "unsupported target kind: %r" % (kind,)


def _resolve_criterion_targets(doc, criterion, created_by_action):
    """([DB.ElementId, ...], reason_or_None). The ONE place every evaluator
    resolves its target(s) through -- centralizing this here (rather than
    each evaluator branching on `target` vs `targets` vs "element" vs
    "created_by" itself) is what makes it possible to add a new target
    kind or a new composed shape without touching all 5 evaluators' own
    logic again.

    Handles both the singular `target` (unchanged since slice 1 for kind
    "element" -- can be a batch of many ids; new for kind "created_by" --
    always exactly one id) and the new plural `targets` (a list of
    single-source refs, each contributing exactly one id, concatenated in
    order) -- needed for a composed reference like an existing element
    paired with one a plan action creates, which a single `target` dict
    cannot express. plan_schema.py already guarantees a criterion has
    exactly one of the two fields; this function trusts that (a malformed
    criterion that somehow reaches here with neither is treated as
    resolving to nothing, the same honest answer as any other
    invalid_reference)."""
    if "targets" in criterion:
        all_ids = []
        for ref in (criterion.get("targets") or []):
            ids, reason = _resolve_one_ref(doc, ref, created_by_action)
            if ids is None:
                return [], reason
            all_ids.extend(ids)
        return all_ids, None

    ids, reason = _resolve_one_ref(doc, criterion.get("target"), created_by_action)
    if ids is None:
        return [], reason
    return ids, None


# ===========================================================================
# element_exists / element_absent -- thin wrappers over already-shipped code
# ===========================================================================

def _eval_element_exists(doc, criterion, created_by_action):
    ids, reason = _resolve_criterion_targets(doc, criterion, created_by_action)
    if not ids:
        return eval_support.make_result("invalid_reference", None, None,
                                         reason or "target resolved to zero elements")
    raw = utils.verify_elements_exist(doc, [get_element_id_value(e) for e in ids])
    status = "passed" if raw.get("ok") else "failed"
    return eval_support.make_result(status, raw.get("expected"), raw.get("actual"), raw.get("reason"))


def _eval_element_absent(doc, criterion, created_by_action):
    ids, reason = _resolve_criterion_targets(doc, criterion, created_by_action)
    if not ids:
        return eval_support.make_result("invalid_reference", None, None,
                                         reason or "target resolved to zero elements")
    # editing.verify_delete already proves this works for an element deleted
    # earlier in the SAME plan: it calls doc.GetElement(eid) itself and
    # treats None as absence -- exactly the mechanism a mid-plan delete
    # needs, already live-tested in Phase A.
    raw = editing.verify_delete(doc, ids)
    status = "passed" if raw.get("ok") else "failed"
    result_reason = None if raw.get("ok") else "one or more target elements still exist"
    return eval_support.make_result(status, raw.get("expected"), raw.get("actual"), result_reason)


# ===========================================================================
# room_area_sqm
# ===========================================================================

def _eval_room_area_sqm(doc, criterion, created_by_action):
    ids, reason = _resolve_criterion_targets(doc, criterion, created_by_action)
    if len(ids) != 1:
        return eval_support.make_result(
            "invalid_reference", criterion.get("value"), None,
            reason or ("room_area_sqm needs exactly 1 target element, got %d" % len(ids)))

    room = doc.GetElement(ids[0])
    if room is None:
        return eval_support.make_result("invalid_reference", criterion.get("value"), None,
                                         "target element no longer exists")
    if not _is_room(room):
        return eval_support.make_result("wrong_category", criterion.get("value"), None,
                                         "target is not a Room")

    area_param = room.get_Parameter(DB.BuiltInParameter.ROOM_AREA)
    if area_param is None or not area_param.HasValue:
        return eval_support.make_result("missing_parameter", criterion.get("value"), None,
                                         "ROOM_AREA parameter unavailable")

    actual_sqm = area_param.AsDouble() * utils.SQFT_TO_SQM
    ok = eval_support.compare(criterion.get("op"), actual_sqm, criterion.get("value"))
    if ok is None:
        return eval_support.make_result("unsupported_type", criterion.get("value"), actual_sqm,
                                         "comparison not applicable for this operator/value")
    return eval_support.make_result("passed" if ok else "failed", criterion.get("value"), actual_sqm)


# ===========================================================================
# param_value -- NO unit conversion; see module docstring
# ===========================================================================

def _eval_param_value(doc, criterion, created_by_action):
    ids, reason = _resolve_criterion_targets(doc, criterion, created_by_action)
    if len(ids) != 1:
        return eval_support.make_result(
            "invalid_reference", criterion.get("value"), None,
            reason or ("param_value needs exactly 1 target element, got %d" % len(ids)))

    elem = doc.GetElement(ids[0])
    if elem is None:
        return eval_support.make_result("invalid_reference", criterion.get("value"), None,
                                         "target element no longer exists")

    allow_fallback = bool(criterion.get("allow_type_fallback", False))
    param, owner = resolve_param(doc, elem, criterion.get("parameter_name"), allow_fallback)
    used_fallback = (owner is not None and owner is not elem)
    if param is None:
        return eval_support.make_result(
            "missing_parameter", criterion.get("value"), None,
            "parameter %r not found" % (criterion.get("parameter_name"),), used_fallback)

    storage, native = read_param_native(param)
    if storage is None:
        return eval_support.make_result("evaluator_error", criterion.get("value"), None,
                                         "could not read parameter value", used_fallback)

    ok = eval_support.compare(criterion.get("op"), native, criterion.get("value"))
    if ok is None:
        return eval_support.make_result(
            "unsupported_type", criterion.get("value"), native,
            "operator %r not applicable to this parameter's storage type (%s)"
            % (criterion.get("op"), storage), used_fallback)
    return eval_support.make_result("passed" if ok else "failed", criterion.get("value"), native,
                                     None, used_fallback)


# ===========================================================================
# distance_mm -- LocationPoint only, no bbox-center fallback (see docstring)
# ===========================================================================

def _eval_distance_mm(doc, criterion, created_by_action):
    ids, reason = _resolve_criterion_targets(doc, criterion, created_by_action)
    if len(ids) != 2:
        return eval_support.make_result(
            "invalid_reference", criterion.get("value"), None,
            reason or ("distance_mm needs exactly 2 target elements, got %d" % len(ids)))

    elems = [doc.GetElement(i) for i in ids]
    if any(e is None for e in elems):
        return eval_support.make_result("invalid_reference", criterion.get("value"), None,
                                         "one or both target elements no longer exist")

    points = []
    for e in elems:
        pt = _location_point(e)
        if pt is None:
            return eval_support.make_result(
                "unsupported_geometry", criterion.get("value"), None,
                "element %s has no LocationPoint - curve-based elements (e.g. walls) are "
                "not supported by distance_mm" % get_element_id_value(e))
        points.append(pt)

    actual_mm = points[0].DistanceTo(points[1]) * FEET_TO_MM
    ok = eval_support.compare(criterion.get("op"), actual_mm, criterion.get("value"))
    if ok is None:
        return eval_support.make_result("unsupported_type", criterion.get("value"), actual_mm,
                                         "comparison not applicable for this operator/value")
    return eval_support.make_result("passed" if ok else "failed", criterion.get("value"), actual_mm)


EVALUATOR_FUNCS = {
    "element_exists": _eval_element_exists,
    "element_absent": _eval_element_absent,
    "room_area_sqm": _eval_room_area_sqm,
    "param_value": _eval_param_value,
    "distance_mm": _eval_distance_mm,
}


def evaluate_success_criteria(doc, plan, created_by_action=None):
    """[{**criterion, "k": idx, **result}, ...] in declared order. NEVER
    raises -- an evaluator that raises is caught here and becomes status
    "evaluator_error", distinguishable from every legitimate business
    outcome above (see eval_support.py's docstring on why this matters:
    an infrastructure failure must never be spelled the same way as "this
    target doesn't exist").

    created_by_action ({action_i: [created_id, ...]}, from
    task_store.created_ids_by_action) resolves any "created_by" target --
    defaults to {} (every such reference then honestly resolves to
    invalid_reference rather than raising, for a caller that doesn't pass
    one)."""
    created_by_action = created_by_action or {}
    out = []
    for k, criterion in enumerate(plan.get("success_criteria") or []):
        ev_name = criterion.get("evaluator") if isinstance(criterion, dict) else None
        fn = EVALUATOR_FUNCS.get(ev_name)
        try:
            if fn is None:
                result = eval_support.make_result(
                    "evaluator_error", criterion.get("value") if isinstance(criterion, dict) else None,
                    None, "unknown evaluator %r" % (ev_name,))
            else:
                result = fn(doc, criterion, created_by_action)
        except Exception as e:
            result = eval_support.make_result(
                "evaluator_error", criterion.get("value") if isinstance(criterion, dict) else None,
                None, "evaluator raised: %s" % str(e))
        out.append(dict(
            k=k, evaluator=ev_name,
            op=criterion.get("op") if isinstance(criterion, dict) else None,
            target=criterion.get("target") if isinstance(criterion, dict) else None,
            targets=criterion.get("targets") if isinstance(criterion, dict) else None,
            **result
        ))
    return out
