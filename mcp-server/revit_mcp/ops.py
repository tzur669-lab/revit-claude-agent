# -*- coding: UTF-8 -*-
"""
The op dispatcher -- Phase-A's execution vocabulary. Each op provides
capture/run/verify/inverse/probe; the slice loop in plan_exec.py owns every
transaction (Start/commit_verified/RollBack) and calls into these -- an op's
`run` NEVER opens a transaction itself (tests/test_ops_no_transactions.py
is an AST guard for exactly this).

Signature note vs the original design sketch: every function below takes
`args` explicitly, not just `targets` -- capture() genuinely needs it (e.g.
set_parameters must know WHICH parameter names to capture before mutating
anything), so the signature is uniform rather than special-cased per op:

    capture: fn(doc, args, targets) -> before
    run:     fn(doc, args, targets, before) -> result
    verify:  fn(doc, args, targets, before, result) -> verified_dict
    inverse: fn(args, targets, before, result) -> action_dict | None   (PURE -- no doc)
    probe:   fn(doc, args, before) -> "not_applied" | "applied" | "unknown"

`inverse` is called twice in practice, by plan_exec.py:
  - for "absolute" ops, right after capture() and BEFORE the transaction
    commits (result=None at that point -- unused by absolute-op inverses).
  - for "creating" ops, right after run() commits (before is often {} for
    these -- unused; the created id lives in `result`).
This is why inverse always receives both `before` and `result`, even
though any one op's inverse only ever reads one of them.

idempotency classes (see docs/operation-contracts.md for the full
argument): "absolute" ops converge to the same end state no matter how
many times they are (re-)applied, so a crash inside the transaction
window (an unknown action outcome) is always safe to just re-run.
"creating" ops make a duplicate on a second application, so a crash there
MUST be resolved by probe() -- an inconclusive probe blocks rather than
guesses. move_elements is deliberately made absolute (see _move_run's
docstring) even though the underlying Revit API
(ElementTransformUtils.MoveElement) is itself relative -- that rewrite is
this file's central idempotency fix.
"""
from pyrevit import DB
import editing
import building
import placement
import rooms
import transforms
import utils
from refs import resolve_ref
from utils import get_element_id_value, make_element_id

MM_TO_FEET = transforms.MM_TO_FEET
FEET_TO_MM = 304.8

_STORAGE_BY_NAME = {
    "String": DB.StorageType.String,
    "Integer": DB.StorageType.Integer,
    "Double": DB.StorageType.Double,
    "ElementId": DB.StorageType.ElementId,
}


def _mm_to_xyz(mm):
    return DB.XYZ(
        float(mm.get("x", 0)) * MM_TO_FEET,
        float(mm.get("y", 0)) * MM_TO_FEET,
        float(mm.get("z", 0)) * MM_TO_FEET,
    )


def _xyz_to_mm(pt):
    return {"x": pt.X * FEET_TO_MM, "y": pt.Y * FEET_TO_MM, "z": pt.Z * FEET_TO_MM}


def _first_level(doc):
    levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements())
    levels.sort(key=lambda lv: lv.Elevation)
    return levels[0] if levels else None


# ===========================================================================
# move_elements -- ABSOLUTE (rewritten from Revit's own relative API)
# ===========================================================================
#
# ElementTransformUtils.MoveElement is relative: applying it twice moves
# twice. Re-running it blindly on an "unknown action outcome" (a crash
# inside the transaction window) would silently double-move the element --
# exactly the corruption class idempotency classing exists to prevent.
#
# The fix: capture() records each element's point at PROPOSE-adjacent time
# (before any run() has executed). run() computes the ABSOLUTE target as
# before + requested_vector, then applies target - CURRENT as the actual
# translation. Re-running after a partial or complete prior application
# then converges to the same target instead of accumulating -- this is
# what makes "absolute" an honest classification, not just a label.

def _move_capture(doc, args, targets):
    ids = resolve_ref(doc, targets)
    pre = transforms.capture_move_state(doc, ids)  # {int_id: XYZ}
    return dict((str(k), _xyz_to_mm(pt)) for k, pt in pre.items())


def _move_run(doc, args, targets, before):
    ids = resolve_ref(doc, targets)
    vector_mm = args["vector_mm"]
    requested = _mm_to_xyz(vector_mm)
    touched_ids, touched_uids = [], []
    for eid in ids:
        elem = doc.GetElement(eid)
        if elem is None:
            continue
        iid = get_element_id_value(eid)
        pre_mm = before.get(str(iid))
        current = transforms._location_point(elem)
        if pre_mm is not None and current is not None:
            target = DB.XYZ(
                pre_mm["x"] * MM_TO_FEET + requested.X,
                pre_mm["y"] * MM_TO_FEET + requested.Y,
                pre_mm["z"] * MM_TO_FEET + requested.Z,
            )
            delta = DB.XYZ(target.X - current.X, target.Y - current.Y, target.Z - current.Z)
        else:
            # no captured baseline (element had no LocationPoint/Curve at
            # capture time) -- fall back to applying the requested vector
            # directly, matching the friendly route's own not_checked case.
            delta = requested
        DB.ElementTransformUtils.MoveElement(doc, eid, delta)
        touched_ids.append(iid)
        touched_uids.append(elem.UniqueId)
    return {"touched_ids": touched_ids, "touched_uids": touched_uids, "created_ids": []}


def _move_verify(doc, args, targets, before, result):
    ids = resolve_ref(doc, targets)
    vector_mm = args["vector_mm"]
    translation = _mm_to_xyz(vector_mm)
    pre_points = {}
    for k, mm in before.items():
        pre_points[int(k)] = _mm_to_xyz(mm)
    return transforms.verify_move(doc, ids, translation, pre_points)


def _move_inverse(args, targets, before, result):
    """PURE. Negates the ORIGINAL requested vector -- since the forward op
    is absolute (target = before + vector), applying the negated vector as
    a new absolute move restores the original position exactly, regardless
    of how many times the forward move was (re-)applied."""
    vector_mm = args["vector_mm"]
    neg = {
        "x": -float(vector_mm.get("x", 0)),
        "y": -float(vector_mm.get("y", 0)),
        "z": -float(vector_mm.get("z", 0)),
    }
    return {"op": "move_elements", "targets": targets, "args": {"vector_mm": neg}}


# ===========================================================================
# set_parameters -- ABSOLUTE
# ===========================================================================

def _params_capture(doc, args, targets):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {}
    elem = doc.GetElement(ids[0])  # exactly one element per action
    if elem is None:
        return {}
    allow_type_fallback = bool(args.get("allow_type_fallback", False))
    out = {}
    for pname in (args.get("parameters") or {}).keys():
        param, _owner = utils.resolve_param(doc, elem, pname, allow_type_fallback)
        if param is None:
            out[pname] = None  # not found -- inverse/verify treat this as "nothing to restore"
            continue
        storage, old_val = utils.read_param_typed(param)
        out[pname] = {"storage": str(storage), "old": old_val}
    return out


def _params_run(doc, args, targets, before):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {"touched_ids": [], "touched_uids": [], "created_ids": [], "set": [], "failed": []}
    eid = ids[0]
    elem = doc.GetElement(eid)
    allow_type_fallback = bool(args.get("allow_type_fallback", False))
    set_names, failed = [], []
    for pname, new_value in (args.get("parameters") or {}).items():
        param, _owner = utils.resolve_param(doc, elem, pname, allow_type_fallback)
        if param is None:
            failed.append({"parameter": pname, "reason": "not found"})
            continue
        if param.IsReadOnly:
            failed.append({"parameter": pname, "reason": "read-only"})
            continue
        try:
            utils.set_param_typed(param, new_value)
            set_names.append(pname)
        except Exception as e:
            failed.append({"parameter": pname, "reason": "set failed: {}".format(str(e))})
    return {
        "touched_ids": [get_element_id_value(eid)], "touched_uids": [elem.UniqueId],
        "created_ids": [], "set": set_names, "failed": failed,
    }


def _params_verify(doc, args, targets, before, result):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {"ok": None, "status": "not_checked", "reason": "no target resolved"}
    elem_after = doc.GetElement(ids[0])
    to_verify = result.get("set") or []
    allow_type_fallback = bool(args.get("allow_type_fallback", False))
    failures, checked = [], 0
    for pname in to_verify:
        param, _owner = utils.resolve_param(doc, elem_after, pname, allow_type_fallback)
        if param is None:
            failures.append({"parameter": pname, "reason": "not found on re-read"})
            continue
        cap = before.get(pname)
        if not cap:
            continue
        checked += 1
        storage = _STORAGE_BY_NAME.get(cap["storage"])
        intended = (args.get("parameters") or {}).get(pname)
        ok, actual_str = utils.param_read_matches(param, storage, intended)
        if not ok:
            failures.append({"parameter": pname, "expected": str(intended), "actual": actual_str})
    verified = {
        "ok": (len(failures) == 0) if checked else None,
        "method": "param_read_back",
        "expected": {"count": len(to_verify)},
        "actual": {"count_ok": checked - len(failures)},
    }
    if not checked:
        verified["status"] = "not_checked"
        verified["reason"] = "No parameter was successfully set"
    if failures:
        verified["failures"] = failures[:50]
    return verified


def _params_inverse(args, targets, before, result):
    """PURE. Restores each captured old value. A parameter not found at
    capture time (cap is None) has nothing to restore -- omitted, not
    defaulted to empty, since "" is itself a real, distinct value."""
    restore = {}
    for pname, cap in (before or {}).items():
        if cap is None:
            continue
        restore[pname] = cap["old"]
    if not restore:
        return None
    return {
        "op": "set_parameters", "targets": targets,
        "args": {"parameters": restore, "allow_type_fallback": bool(args.get("allow_type_fallback", False))},
    }


# ===========================================================================
# set_wall_curve -- ABSOLUTE, approximate fidelity
# ===========================================================================
# Measured live (docs/operation-contracts.md): a hosted door's cut position
# falling outside a reassigned wall curve can Commit() as Committed with an
# EMPTY failure-message list and the curve reading back byte-identical --
# the proof verification_ok must exist separately from execution_ok.

def _wall_curve_capture(doc, args, targets):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {}
    elem = doc.GetElement(ids[0])
    if elem is None:
        return {}
    loc = elem.Location
    if loc is None or not hasattr(loc, "Curve"):
        return {}
    curve = loc.Curve
    return {"start_mm": _xyz_to_mm(curve.GetEndPoint(0)), "end_mm": _xyz_to_mm(curve.GetEndPoint(1))}


def _wall_curve_run(doc, args, targets, before):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {"touched_ids": [], "touched_uids": [], "created_ids": []}
    eid = ids[0]
    elem = doc.GetElement(eid)
    new_curve = DB.Line.CreateBound(_mm_to_xyz(args["start_mm"]), _mm_to_xyz(args["end_mm"]))
    elem.Location.Curve = new_curve
    return {"touched_ids": [get_element_id_value(eid)], "touched_uids": [elem.UniqueId], "created_ids": []}


# Looser than transforms.py's LOCATION_TOLERANCE_FT (1e-6 ft) on purpose --
# this op's own fidelity is "approximate", not "exact"; 1mm still catches
# the measured silent-no-op case (curve reading back byte-identical).
_CURVE_TOLERANCE_MM = 1.0


def _wall_curve_verify(doc, args, targets, before, result):
    ids = resolve_ref(doc, targets)
    if not ids:
        return {"ok": None, "status": "not_checked", "reason": "no target resolved"}
    elem_after = doc.GetElement(ids[0])
    loc = elem_after.Location if elem_after else None
    if loc is None or not hasattr(loc, "Curve"):
        return {"ok": None, "status": "not_checked", "reason": "element has no LocationCurve after commit"}
    curve = loc.Curve
    actual_start = _xyz_to_mm(curve.GetEndPoint(0))
    actual_end = _xyz_to_mm(curve.GetEndPoint(1))
    expected_start, expected_end = args["start_mm"], args["end_mm"]

    def _close(a, e):
        return abs(a.get("x", 0) - e.get("x", 0)) <= _CURVE_TOLERANCE_MM \
            and abs(a.get("y", 0) - e.get("y", 0)) <= _CURVE_TOLERANCE_MM \
            and abs(a.get("z", 0) - e.get("z", 0)) <= _CURVE_TOLERANCE_MM

    ok = _close(actual_start, expected_start) and _close(actual_end, expected_end)
    verified = {
        "ok": ok, "method": "curve_endpoints",
        "expected": {"start_mm": expected_start, "end_mm": expected_end},
        "actual": {"start_mm": actual_start, "end_mm": actual_end},
    }
    if not ok:
        verified["reason"] = (
            "curve endpoints do not match within {}mm -- this is the "
            "documented silent-failure case (Commit=Committed, empty "
            "failure list, curve unchanged)".format(_CURVE_TOLERANCE_MM)
        )
    return verified


def _wall_curve_inverse(args, targets, before, result):
    if not before:
        return None
    return {"op": "set_wall_curve", "targets": targets,
            "args": {"start_mm": before["start_mm"], "end_mm": before["end_mm"]}}


# ===========================================================================
# delete_elements -- ABSOLUTE, irreversible
# ===========================================================================

def _delete_capture(doc, args, targets):
    ids = resolve_ref(doc, targets)
    out = {}
    for eid in ids:
        elem = doc.GetElement(eid)
        iid = get_element_id_value(eid)
        out[str(iid)] = {"uid": (elem.UniqueId if elem is not None else None), "existed": elem is not None}
    return out


def _delete_run(doc, args, targets, before):
    ids = resolve_ref(doc, targets)
    deleted_ids, cascaded_ids = editing.apply_delete(doc, ids)
    touched_uids = []
    for i in deleted_ids:
        cap = before.get(str(i))
        if cap and cap.get("uid"):
            touched_uids.append(cap["uid"])
    return {"touched_ids": deleted_ids, "touched_uids": touched_uids,
            "created_ids": [], "cascaded_ids": cascaded_ids}


def _delete_verify(doc, args, targets, before, result):
    ids = resolve_ref(doc, targets)
    return editing.verify_delete(doc, ids)


def _delete_inverse(args, targets, before, result):
    """Irreversible by construction: Revit cannot recreate an element from
    an id. fidelity="irreversible" on this op is what forces every plan
    containing it to disclose partial reversibility before approval."""
    return None


# ===========================================================================
# create_room -- CREATING (needs probe)
# ===========================================================================

def _room_at_point(doc, point_mm):
    if not point_mm:
        return None
    pt = _mm_to_xyz(point_mm)
    try:
        room = doc.GetRoomAtPoint(pt)
    except Exception:
        return None
    return get_element_id_value(room.Id) if room is not None else None


def _room_capture(doc, args, targets):
    return {"existing_room_id": _room_at_point(doc, args.get("point_mm"))}


def _room_run(doc, args, targets, before):
    level = doc.GetElement(make_element_id(args["level_id"]))
    phases = doc.Phases
    phase = phases.get_Item(phases.Size - 1)
    room = rooms.apply_create_room(doc, level, phase, args.get("point_mm"), args.get("name"), args.get("number"))
    if room is None:
        return {"touched_ids": [], "touched_uids": [], "created_ids": [], "failed": True}
    return {"touched_ids": [], "touched_uids": [], "created_ids": [get_element_id_value(room)], "failed": False}


def _room_verify(doc, args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return {"ok": False, "method": "room_area", "reason": "Failed to create room - no enclosed area found"}
    room_id = created[0]
    facts = rooms.read_room_facts(doc, room_id)
    return rooms.verify_room(doc, room_id, facts["area_sqm"])


def _room_inverse(args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return None
    return {"op": "delete_elements", "targets": {"kind": "element", "ids": created}, "args": {}}


def _room_probe(doc, args, before):
    """(outcome, recovered_ids) -- applied: a room now exists at the point
    that did not at capture time, and recovered_ids is [that room's id],
    since GetRoomAtPoint already resolved the real element as part of
    making this determination (Phase B slice 2: this used to discard that
    id, forcing a later created_by reference to a probe-skipped create_room
    into invalid_reference even though the room was right there). not_applied:
    same as capture time, recovered_ids is None. unknown: anything else
    (including no point_mm to test at all) -- never guessed, recovered_ids
    is None."""
    if before is None:
        return "unknown", None
    current_id = _room_at_point(doc, args.get("point_mm"))
    was_id = before.get("existing_room_id")
    if current_id == was_id:
        return "not_applied", None
    if current_id is not None and was_id is None:
        return "applied", [current_id]
    return "unknown", None


# ===========================================================================
# create_room_separation -- CREATING (probe always unknown -- see docstring)
# ===========================================================================

def _resolve_view_for_level(doc, level_id):
    """First ViewPlan whose GenLevel matches level_id; falls back to any
    ViewPlan. NewRoomBoundaryLines requires a View -- this replaces the
    friendly route's view_name/active-view resolution with something a
    headless plan op (no UI, no "active view") can use deterministically."""
    views = list(DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).WhereElementIsNotElementType().ToElements())
    for v in views:
        try:
            if v.GenLevel is not None and get_element_id_value(v.GenLevel.Id) == level_id:
                return v
        except Exception:
            continue
    return views[0] if views else None


def _room_sep_capture(doc, args, targets):
    return {}


def _room_sep_run(doc, args, targets, before):
    level_id = args.get("level_id")
    view = _resolve_view_for_level(doc, level_id)
    if view is None:
        return {"touched_ids": [], "touched_uids": [], "created_ids": [], "failed": True,
                "reason": "no plan view found"}
    curve_mm = args["curve_mm"]
    line = DB.Line.CreateBound(_mm_to_xyz(curve_mm["start"]), _mm_to_xyz(curve_mm["end"]))
    curve_array = DB.CurveArray()
    curve_array.Append(line)
    sp_plane = view.SketchPlane
    if not sp_plane and view.GenLevel:
        sp_plane = DB.SketchPlane.Create(doc, view.GenLevel.Id)
    created_ids = rooms.apply_create_room_separation(doc, sp_plane, curve_array, view)
    return {"touched_ids": [], "touched_uids": [], "created_ids": created_ids, "failed": len(created_ids) == 0}


def _room_sep_verify(doc, args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return {"ok": False, "method": "element_category", "reason": "no room separation line was created"}
    return rooms.verify_room_separation(doc, created)


def _room_sep_inverse(args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return None
    return {"op": "delete_elements", "targets": {"kind": "element", "ids": created}, "args": {}}


def _room_sep_probe(doc, args, before):
    """(outcome, recovered_ids) -- room separation lines have no natural
    point-membership test the way Rooms do (GetRoomAtPoint). A conclusive
    probe would need a near-duplicate-curve scan, not implemented in Phase
    A -- always ("unknown", None), so a crash in this op's transaction
    window always blocks rather than guesses whether a duplicate line
    would result, and a later created_by reference to this action's output
    after such a resume is honestly invalid_reference, not a guess."""
    return "unknown", None


# ===========================================================================
# create_line_based_element -- CREATING, walls only (see building.py's
# apply_create_wall docstring for why this op is narrower than the
# friendly batch route)
# ===========================================================================

def _line_elem_capture(doc, args, targets):
    return {}


def _line_elem_run(doc, args, targets, before):
    curve_mm = args["curve_mm"]
    line = DB.Line.CreateBound(_mm_to_xyz(curve_mm["start"]), _mm_to_xyz(curve_mm["end"]))
    type_id = make_element_id(args["type_id"])
    level_id = args.get("level_id")
    if level_id is not None:
        level_eid = make_element_id(level_id)
    else:
        lvl = _first_level(doc)
        level_eid = lvl.Id if lvl else None
    height_feet = float(args.get("height_mm", 3000)) / 304.8
    wall = building.apply_create_wall(doc, line, type_id, level_eid, height_feet, 0.0, False)
    return {"touched_ids": [], "touched_uids": [], "created_ids": [get_element_id_value(wall)]}


def _line_elem_verify(doc, args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return {"ok": False, "method": "element_category", "reason": "no wall was created"}
    return building.verify_line_based_elements(doc, [(created[0], int(DB.BuiltInCategory.OST_Walls))])


def _line_elem_inverse(args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return None
    return {"op": "delete_elements", "targets": {"kind": "element", "ids": created}, "args": {}}


def _line_elem_probe(doc, args, before):
    """(outcome, recovered_ids) -- no natural membership test for 'a wall
    already exists on this curve' without a geometry scan -- not
    implemented in Phase A. Always ("unknown", None)."""
    return "unknown", None


# ===========================================================================
# place_family -- CREATING
# ===========================================================================

def _place_capture(doc, args, targets):
    return {}


def _place_run(doc, args, targets, before):
    symbol = doc.GetElement(make_element_id(args["symbol_id"]))
    point = _mm_to_xyz(args["point_mm"])
    level = doc.GetElement(make_element_id(args["level_id"])) if args.get("level_id") is not None else None
    host = doc.GetElement(make_element_id(args["host_id"])) if args.get("host_id") is not None else None
    instance = placement.apply_place_family(doc, symbol, point, level=level, host=host)
    return {"touched_ids": [], "touched_uids": [], "created_ids": [get_element_id_value(instance)]}


def _place_verify(doc, args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return {"ok": False, "method": "element_exists", "reason": "no instance was created"}
    point = _mm_to_xyz(args["point_mm"])
    return placement.verify_place_family(doc, created[0], point, args.get("host_id"))


def _place_inverse(args, targets, before, result):
    created = result.get("created_ids") or []
    if not created:
        return None
    return {"op": "delete_elements", "targets": {"kind": "element", "ids": created}, "args": {}}


def _place_probe(doc, args, before):
    """(outcome, recovered_ids) -- no natural membership test for 'this
    family is already placed here' without a spatial scan -- not
    implemented in Phase A. Always ("unknown", None)."""
    return "unknown", None


# ===========================================================================
# registry
# ===========================================================================

OPS = {
    "move_elements": {
        "capture": _move_capture, "run": _move_run, "verify": _move_verify,
        "inverse": _move_inverse, "probe": None,
        "idempotency": "absolute", "fidelity": "exact", "risk": "modifying",
    },
    "set_parameters": {
        "capture": _params_capture, "run": _params_run, "verify": _params_verify,
        "inverse": _params_inverse, "probe": None,
        "idempotency": "absolute", "fidelity": "exact", "risk": "modifying",
    },
    "set_wall_curve": {
        "capture": _wall_curve_capture, "run": _wall_curve_run, "verify": _wall_curve_verify,
        "inverse": _wall_curve_inverse, "probe": None,
        "idempotency": "absolute", "fidelity": "approximate", "risk": "modifying",
    },
    "delete_elements": {
        "capture": _delete_capture, "run": _delete_run, "verify": _delete_verify,
        "inverse": _delete_inverse, "probe": None,
        "idempotency": "absolute", "fidelity": "irreversible", "risk": "destructive",
    },
    "create_room": {
        "capture": _room_capture, "run": _room_run, "verify": _room_verify,
        "inverse": _room_inverse, "probe": _room_probe,
        "idempotency": "creating", "fidelity": "compensatable", "risk": "additive",
    },
    "create_room_separation": {
        "capture": _room_sep_capture, "run": _room_sep_run, "verify": _room_sep_verify,
        "inverse": _room_sep_inverse, "probe": _room_sep_probe,
        "idempotency": "creating", "fidelity": "compensatable", "risk": "additive",
    },
    "create_line_based_element": {
        "capture": _line_elem_capture, "run": _line_elem_run, "verify": _line_elem_verify,
        "inverse": _line_elem_inverse, "probe": _line_elem_probe,
        "idempotency": "creating", "fidelity": "compensatable", "risk": "additive",
    },
    "place_family": {
        "capture": _place_capture, "run": _place_run, "verify": _place_verify,
        "inverse": _place_inverse, "probe": _place_probe,
        "idempotency": "creating", "fidelity": "compensatable", "risk": "additive",
    },
}
