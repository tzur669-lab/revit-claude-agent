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
    probe:   fn(doc, args, before) -> (outcome, recovered_ids, reason)
             outcome in "not_applied" | "applied" | "unknown"; recovered_ids
             is a list of ints only when outcome == "applied", else None;
             reason is one of eval_support.PROBE_REASONS. This is
             consulted only as Level-3 fallback (plan_exec.py's
             _classify_open_action checks a "commit_result" ledger record
             -- Level-1 authoritative evidence -- first; see Phase B slice
             3 in docs/architecture.md).

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
import eval_support
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


def _room_universe(doc):
    """{str(id): [area_sqft]} for every Room element -- Phase B slice 3's
    universe-diff safety net, applied to create_room too (see _room_probe's
    docstring for the false-'applied' path this closes). An existing
    room's area changing during the crash window (its boundary was edited
    by someone/something else) is a real, relevant change, flagged the
    same conservative way as every other creating op -- not a case
    reserved for the other three ops only."""
    out = {}
    all_rooms = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Rooms) \
        .WhereElementIsNotElementType().ToElements()
    for r in all_rooms:
        rid = str(get_element_id_value(r.Id))
        try:
            out[rid] = [round(float(r.Area), 6)]
        except Exception:
            out[rid] = None
    return out


def _room_capture(doc, args, targets):
    return {"universe": _room_universe(doc)}


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
    """(outcome, recovered_ids, reason) -- Phase B slice 3 (B2 fix): the
    original version of this probe compared only `GetRoomAtPoint`'s
    result before vs. after (current_id == was_id). That cannot
    distinguish "a NEW room was created here" from "a PRE-EXISTING room
    started reaching this point because its boundary changed elsewhere
    during the crash window" -- the second case would have reported
    ("applied", [that pre-existing room's id]), a false positive handing
    a wrong id to a later created_by reference. The universe-diff
    (eval_support.probe_outcome_from_universe) closes this: a pre-existing
    room is already a member of the captured room universe (_room_universe)
    regardless of whether it used to cover this point, so it can never be
    the "new since capture" element `applied` requires. GetRoomAtPoint is
    still used, but only to decide which of the genuinely-new rooms (if
    any) is the one AT this point -- not as the sole signal."""
    if before is None or "universe" not in before:
        return "unknown", None, "no_capture"
    current_universe = _room_universe(doc)
    current_id = _room_at_point(doc, args.get("point_mm"))
    matching = set([str(current_id)]) if current_id is not None else set()
    return eval_support.probe_outcome_from_universe(before.get("universe"), current_universe, matching)


# ===========================================================================
# create_room_separation -- CREATING
# ===========================================================================

def _room_sep_read_curve(elem):
    """(start_mm, end_mm) or None. Room-separation lines returned by
    NewRoomBoundaryLines are ModelCurve-derived; which access path this
    Revit version actually supports for their geometry was an open
    question at plan time (Phase B slice 3) -- measured live, not
    guessed: tries .Location.Curve first (the same access walls already
    use, ops.py's own _wall_curve_capture), falls back to .GeometryCurve.
    Either access failing, or the curve not being a straight DB.Line,
    excludes the element from MATCHING -- it is still counted in the
    universe (see _room_sep_universe), so it can still suppress a false
    'applied' by disappearing or changing, but is never approximated into
    a match."""
    curve = None
    try:
        loc = elem.Location
        if loc is not None and hasattr(loc, "Curve"):
            curve = loc.Curve
    except Exception:
        curve = None
    if curve is None:
        try:
            curve = elem.GeometryCurve
        except Exception:
            curve = None
    if curve is None or not isinstance(curve, DB.Line):
        return None
    return _xyz_to_mm(curve.GetEndPoint(0)), _xyz_to_mm(curve.GetEndPoint(1))


def _room_sep_universe(doc):
    """{str(id): [start_xyz_mm..., end_xyz_mm...]} for every
    OST_RoomSeparationLines element -- Phase B slice 3's universe-diff
    safety net. No type filter: this op sets no type on what it creates."""
    out = {}
    lines = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_RoomSeparationLines) \
        .WhereElementIsNotElementType().ToElements()
    for elem in lines:
        eid = str(get_element_id_value(elem.Id))
        pts = _room_sep_read_curve(elem)
        if pts is None:
            out[eid] = None
            continue
        s, e = pts
        out[eid] = [round(s["x"], 1), round(s["y"], 1), round(s["z"], 1),
                    round(e["x"], 1), round(e["y"], 1), round(e["z"], 1)]
    return out


def _room_sep_matching_ids(doc, added_ids, args):
    """Subset of added_ids (new-since-capture id strings) whose curve
    endpoints match this action's requested curve_mm, bidirectionally,
    within _CURVE_TOLERANCE_MM. Deliberately NOT filtered by level/view:
    the created line's elevation already IS curve_mm's own z coordinate,
    so a level filter would add no discriminating power beyond what the
    endpoint match already provides (audited, not overlooked -- see
    docs/architecture.md's Phase B slice 3 section)."""
    curve_mm = args.get("curve_mm") or {}
    want_start, want_end = curve_mm.get("start") or {}, curve_mm.get("end") or {}
    out = set()
    for eid in added_ids:
        try:
            elem = doc.GetElement(make_element_id(eid))
        except Exception:
            continue
        if elem is None:
            continue
        pts = _room_sep_read_curve(elem)
        if pts is None:
            continue
        s, e = pts
        if eval_support.endpoints_match(s, e, want_start, want_end, _CURVE_TOLERANCE_MM):
            out.add(eid)
    return out


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
    return {"universe": _room_sep_universe(doc)}


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
    """(outcome, recovered_ids, reason) -- Phase B slice 3: room
    separation lines have no Revit built-in point/curve lookup the way
    Rooms have GetRoomAtPoint, so identity here rests entirely on the
    universe-diff (eval_support.probe_outcome_from_universe): every
    OST_RoomSeparationLines element is captured at propose-adjacent time;
    'applied' is reported only when exactly one NEW line appears whose
    endpoints match this action's own curve_mm -- never from a bare
    curve-scan match alone, which could not tell a new line from a
    pre-existing one edited into coinciding endpoints."""
    if before is None or "universe" not in before:
        return "unknown", None, "no_capture"
    current_universe = _room_sep_universe(doc)
    before_universe = before.get("universe") or {}
    added = set(current_universe.keys()) - set(before_universe.keys())
    matching = _room_sep_matching_ids(doc, added, args) if added else set()
    return eval_support.probe_outcome_from_universe(before_universe, current_universe, matching)


# ===========================================================================
# create_line_based_element -- CREATING, walls only (see building.py's
# apply_create_wall docstring for why this op is narrower than the
# friendly batch route)
# ===========================================================================

def _resolve_wall_level_id(doc, args):
    """The SAME level-resolution _line_elem_run uses below, as a plain
    int id instead of an ElementId -- capture/probe MUST agree with run()
    on which level a wall means, or a legitimate default (no explicit
    level_id given) would look like a level MISMATCH between the captured
    universe and a freshly created wall. Returns None only if the model
    has no level at all."""
    level_id = args.get("level_id")
    if level_id is not None:
        return int(level_id)
    lvl = _first_level(doc)
    return get_element_id_value(lvl.Id) if lvl else None


_HEIGHT_TOLERANCE_MM = 1.0


def _wall_read_identity(w):
    """(start_mm, end_mm, level_id, height_mm) or None if unreadable, or
    not hosted on a straight DB.Line -- the one place a wall's
    identity-relevant attributes are read, shared by both the universe
    fingerprint (_wall_universe) and the match-against-request check
    (_wall_matching_ids) so the two can never disagree about what a given
    wall's own attributes are."""
    try:
        loc = w.Location
        if loc is None or not hasattr(loc, "Curve") or not isinstance(loc.Curve, DB.Line):
            return None
        curve = loc.Curve
        start_mm, end_mm = _xyz_to_mm(curve.GetEndPoint(0)), _xyz_to_mm(curve.GetEndPoint(1))
        base_param = w.get_Parameter(DB.BuiltInParameter.WALL_BASE_CONSTRAINT)
        level_id = get_element_id_value(base_param.AsElementId()) if base_param else None
        height_param = w.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        height_mm = (height_param.AsDouble() * FEET_TO_MM) if height_param else None
        return start_mm, end_mm, level_id, height_mm
    except Exception:
        return None


def _wall_universe(doc, type_id):
    """{str(id): [start_xyz_mm..., end_xyz_mm..., level_id, height_mm]}
    for every Wall whose GetTypeId() == type_id -- Phase B slice 3's
    universe-diff safety net, scoped by type (narrowest collector
    available; never a global scan). A wall this reads as unreadable /
    non-straight is still counted (fingerprint None), so it can still
    suppress a false 'applied' by disappearing or changing, but is never
    matched against a request (see _wall_matching_ids)."""
    out = {}
    walls = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Walls) \
        .WhereElementIsNotElementType().ToElements()
    for w in walls:
        try:
            if get_element_id_value(w.GetTypeId()) != type_id:
                continue
        except Exception:
            continue
        wid = str(get_element_id_value(w.Id))
        identity = _wall_read_identity(w)
        if identity is None:
            out[wid] = None
            continue
        start_mm, end_mm, level_id, height_mm = identity
        out[wid] = [round(start_mm["x"], 1), round(start_mm["y"], 1), round(start_mm["z"], 1),
                    round(end_mm["x"], 1), round(end_mm["y"], 1), round(end_mm["z"], 1),
                    level_id, (round(height_mm, 1) if height_mm is not None else None)]
    return out


def _wall_matching_ids(doc, added_ids, args):
    """Subset of added_ids (new-since-capture id strings, already known
    to be the same wall type as the request, since _wall_universe is
    type-scoped) whose curve/level/height match every operation-controlled
    input of this action -- type_id (implicit), curve endpoints
    (bidirectional, _CURVE_TOLERANCE_MM), resolved level_id, height_mm
    (_HEIGHT_TOLERANCE_MM). offset/is_structural are excluded: _line_elem_run
    hardcodes both (0.0, False) for every call, so they carry zero
    discriminating power."""
    curve_mm = args.get("curve_mm") or {}
    want_start, want_end = curve_mm.get("start") or {}, curve_mm.get("end") or {}
    want_level = _resolve_wall_level_id(doc, args)
    want_height_mm = float(args.get("height_mm", 3000))
    out = set()
    for wid in added_ids:
        try:
            w = doc.GetElement(make_element_id(wid))
        except Exception:
            continue
        if w is None:
            continue
        identity = _wall_read_identity(w)
        if identity is None:
            continue
        start_mm, end_mm, level_id, height_mm = identity
        if not eval_support.endpoints_match(start_mm, end_mm, want_start, want_end, _CURVE_TOLERANCE_MM):
            continue
        if want_level is not None and level_id != want_level:
            continue
        if height_mm is None or abs(height_mm - want_height_mm) > _HEIGHT_TOLERANCE_MM:
            continue
        out.add(wid)
    return out


def _line_elem_capture(doc, args, targets):
    type_id = int(args["type_id"])
    return {"type_id": type_id, "universe": _wall_universe(doc, type_id)}


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
    """(outcome, recovered_ids, reason) -- Phase B slice 3: identity rests
    on the type-scoped universe-diff (eval_support.probe_outcome_from_universe).
    'applied' only when exactly one NEW wall of this type appears whose
    curve/level/height match this action's own request -- an existing
    wall of the same type independently edited into a matching curve is
    already in the captured universe and can never be reported as new."""
    if before is None or "universe" not in before:
        return "unknown", None, "no_capture"
    type_id = before.get("type_id")
    if type_id is None:
        return "unknown", None, "no_capture"
    current_universe = _wall_universe(doc, type_id)
    before_universe = before.get("universe") or {}
    added = set(current_universe.keys()) - set(before_universe.keys())
    matching = _wall_matching_ids(doc, added, args) if added else set()
    return eval_support.probe_outcome_from_universe(before_universe, current_universe, matching)


# ===========================================================================
# place_family -- CREATING
# ===========================================================================

def _place_read_identity(inst):
    """(point_mm, host_id_or_None, level_id_or_None) or None if the
    instance has no LocationPoint -- shared by the universe fingerprint
    and the match-against-request check so the two can never disagree."""
    try:
        loc = inst.Location
        pt = loc.Point if loc is not None and hasattr(loc, "Point") else None
        if pt is None:
            return None
        point_mm = _xyz_to_mm(pt)
    except Exception:
        return None
    host_id = None
    try:
        host = inst.Host
        if host is not None:
            host_id = get_element_id_value(host.Id)
    except Exception:
        host_id = None
    level_id = None
    try:
        level_eid = inst.LevelId
        if level_eid is not None and level_eid != DB.ElementId.InvalidElementId:
            level_id = get_element_id_value(level_eid)
    except Exception:
        level_id = None
    return point_mm, host_id, level_id


def _place_universe(doc, symbol_id):
    """{str(id): [x_mm, y_mm, z_mm, host_id, level_id]} for every
    FamilyInstance whose Symbol.Id == symbol_id -- Phase B slice 3's
    universe-diff safety net, scoped by symbol (narrowest collector
    available for family instances; there is no OST_* category common to
    every placeable family, and Symbol.Id is exactly what this action
    itself controls)."""
    out = {}
    instances = DB.FilteredElementCollector(doc).OfClass(DB.FamilyInstance).ToElements()
    for inst in instances:
        try:
            sym = inst.Symbol
            if sym is None or get_element_id_value(sym.Id) != symbol_id:
                continue
        except Exception:
            continue
        iid = str(get_element_id_value(inst.Id))
        identity = _place_read_identity(inst)
        if identity is None:
            out[iid] = None
            continue
        point_mm, host_id, level_id = identity
        out[iid] = [round(point_mm["x"], 1), round(point_mm["y"], 1), round(point_mm["z"], 1),
                    host_id, level_id]
    return out


def _place_matching_ids(doc, added_ids, args):
    """Subset of added_ids (new-since-capture id strings, already known
    to be this symbol, since _place_universe is symbol-scoped) whose
    position/host/level match every operation-controlled input of this
    action -- symbol_id (implicit), point (placement.PLACEMENT_TOLERANCE_FT,
    the SAME tolerance placement.verify_place_family already uses for the
    identical question "is this instance at the requested point", z-only
    when hosted since a hosted instance legitimately projects X/Y onto its
    host), host_id when the action supplied one, level_id when supplied
    AND readable. Rotation/facing/placement type are excluded: _place_run
    never sets them -- Revit picks a default -- so they are not
    operation-controlled inputs to match against."""
    want_point = args.get("point_mm") or {}
    want_host = args.get("host_id")
    want_level = args.get("level_id")
    tol_mm = placement.PLACEMENT_TOLERANCE_FT * FEET_TO_MM
    axes = "z" if want_host is not None else "xyz"
    out = set()
    for iid in added_ids:
        try:
            inst = doc.GetElement(make_element_id(iid))
        except Exception:
            continue
        if inst is None:
            continue
        identity = _place_read_identity(inst)
        if identity is None:
            continue
        point_mm, host_id, level_id = identity
        if not eval_support.point_match(point_mm, want_point, tol_mm, axes):
            continue
        if want_host is not None and host_id != int(want_host):
            continue
        if want_level is not None and level_id is not None and level_id != int(want_level):
            continue
        out.add(iid)
    return out


def _place_capture(doc, args, targets):
    symbol_id = int(args["symbol_id"])
    return {"symbol_id": symbol_id, "universe": _place_universe(doc, symbol_id)}


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
    """(outcome, recovered_ids, reason) -- Phase B slice 3: identity rests
    on the symbol-scoped universe-diff (eval_support.probe_outcome_from_universe).
    'applied' only when exactly one NEW instance of this symbol appears
    whose position/host/level match this action's own request -- an
    existing instance of the same symbol independently moved into a
    matching position is already in the captured universe and can never
    be reported as new."""
    if before is None or "universe" not in before:
        return "unknown", None, "no_capture"
    symbol_id = before.get("symbol_id")
    if symbol_id is None:
        return "unknown", None, "no_capture"
    current_universe = _place_universe(doc, symbol_id)
    before_universe = before.get("universe") or {}
    added = set(current_universe.keys()) - set(before_universe.keys())
    matching = _place_matching_ids(doc, added, args) if added else set()
    return eval_support.probe_outcome_from_universe(before_universe, current_universe, matching)


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
