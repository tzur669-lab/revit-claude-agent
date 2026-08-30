# -*- coding: UTF-8 -*-
"""
Transforms Module for Revit MCP
Handles move, copy, rotate, and mirror operations on elements
"""

from utils import get_element_id_value, make_element_id, suppress_warnings, repair_hebrew_in, commit_verified
from pyrevit import routes, revit, DB
from System.Collections.Generic import List
import json
import math
import traceback
import logging

logger = logging.getLogger(__name__)

MM_TO_FEET = 1.0 / 304.8

# Provisional - see docs/operation-contracts.md. Not a documented Revit
# constant; a starting tolerance to be refined from measured behaviour.
LOCATION_TOLERANCE_FT = 1e-6


def _location_point(elem):
    """A representative point for displacement verification, or None if this
    element's Location doesn't expose one (e.g. LocationPoint- or
    LocationCurve-less elements) - callers must treat None as not_checked,
    never as a silent pass."""
    try:
        loc = elem.Location
    except Exception:
        return None
    if loc is None:
        return None
    if hasattr(loc, "Point"):
        return loc.Point
    if hasattr(loc, "Curve"):
        try:
            return loc.Curve.GetEndPoint(0)
        except Exception:
            return None
    return None


def _xyz_to_mm(pt):
    return {"x": pt.X / MM_TO_FEET, "y": pt.Y / MM_TO_FEET, "z": pt.Z / MM_TO_FEET}


def register_transform_routes(api):
    """Register all transform routes with the API"""

    @api.route("/transform_elements/", methods=["POST"])
    def transform_elements_handler(doc, request):
        """Move, copy, rotate, or mirror elements."""
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)

            element_ids = data.get("element_ids", [])
            operation = data.get("operation")

            if not element_ids:
                return routes.make_response(
                    data={"error": "element_ids is required and must not be empty"},
                    status=400,
                )
            if not operation:
                return routes.make_response(
                    data={"error": "operation is required (move, copy, rotate, mirror)"},
                    status=400,
                )
            if operation not in ("move", "copy", "rotate", "mirror"):
                return routes.make_response(
                    data={"error": "Invalid operation '{}'. Use: move, copy, rotate, mirror".format(operation)},
                    status=400,
                )

            # Validate and collect elements
            elem_id_list = []
            for eid in element_ids:
                elem_id = make_element_id(eid)
                elem = doc.GetElement(elem_id)
                if not elem:
                    return routes.make_response(
                        data={"error": "Element {} not found".format(eid)},
                        status=404,
                    )
                # Check if pinned
                if hasattr(elem, "Pinned") and elem.Pinned:
                    return routes.make_response(
                        data={"error": "Element {} is pinned — unpin it first using modify_element before transforming.".format(eid)},
                        status=500,
                    )
                elem_id_list.append(elem_id)

            t = DB.Transaction(doc, "Transform Elements via MCP")
            t.Start()
            suppress_warnings(t)

            try:
                new_element_ids = []
                pre_move_points = {}  # eid.IntegerValue -> XYZ, "move" only

                if operation == "move":
                    vector = data.get("vector")
                    if not vector:
                        t.RollBack()
                        return routes.make_response(
                            data={"error": "vector is required for move operation"},
                            status=400,
                        )
                    translation = DB.XYZ(
                        float(vector.get("x", 0)) * MM_TO_FEET,
                        float(vector.get("y", 0)) * MM_TO_FEET,
                        float(vector.get("z", 0)) * MM_TO_FEET,
                    )
                    # Captured before the move so the post-condition below can
                    # compare actual displacement to what was requested -
                    # Commit() returning Committed does not mean the element
                    # actually moved (measured: a hosted element's host wall
                    # can silently reject a curve change with zero failure
                    # messages and Commit() still Committed).
                    for eid in elem_id_list:
                        elem = doc.GetElement(eid)
                        pt = _location_point(elem)
                        if pt is not None:
                            pre_move_points[eid.IntegerValue] = pt
                    for eid in elem_id_list:
                        DB.ElementTransformUtils.MoveElement(doc, eid, translation)

                elif operation == "copy":
                    vector = data.get("vector")
                    if not vector:
                        t.RollBack()
                        return routes.make_response(
                            data={"error": "vector is required for copy operation"},
                            status=400,
                        )
                    translation = DB.XYZ(
                        float(vector.get("x", 0)) * MM_TO_FEET,
                        float(vector.get("y", 0)) * MM_TO_FEET,
                        float(vector.get("z", 0)) * MM_TO_FEET,
                    )
                    for eid in elem_id_list:
                        copied = DB.ElementTransformUtils.CopyElement(
                            doc, eid, translation
                        )
                        if copied:
                            for cid in copied:
                                new_element_ids.append(get_element_id_value(cid))

                elif operation == "rotate":
                    axis_point = data.get("axis_point")
                    angle = data.get("angle")
                    if not axis_point:
                        t.RollBack()
                        return routes.make_response(
                            data={"error": "axis_point is required for rotate operation"},
                            status=400,
                        )
                    if angle is None:
                        t.RollBack()
                        return routes.make_response(
                            data={"error": "angle is required for rotate operation"},
                            status=400,
                        )

                    center = DB.XYZ(
                        float(axis_point.get("x", 0)) * MM_TO_FEET,
                        float(axis_point.get("y", 0)) * MM_TO_FEET,
                        float(axis_point.get("z", 0)) * MM_TO_FEET,
                    )
                    # Create vertical axis line through the point
                    axis_line = DB.Line.CreateBound(
                        center, DB.XYZ(center.X, center.Y, center.Z + 1.0)
                    )
                    angle_rad = float(angle) * math.pi / 180.0

                    for eid in elem_id_list:
                        DB.ElementTransformUtils.RotateElement(
                            doc, eid, axis_line, angle_rad
                        )

                elif operation == "mirror":
                    mirror_plane = data.get("mirror_plane")
                    if not mirror_plane:
                        t.RollBack()
                        return routes.make_response(
                            data={"error": "mirror_plane is required for mirror operation"},
                            status=400,
                        )

                    origin = mirror_plane.get("origin", {})
                    normal = mirror_plane.get("normal", {})

                    plane_origin = DB.XYZ(
                        float(origin.get("x", 0)) * MM_TO_FEET,
                        float(origin.get("y", 0)) * MM_TO_FEET,
                        float(origin.get("z", 0)) * MM_TO_FEET,
                    )
                    plane_normal = DB.XYZ(
                        float(normal.get("x", 0)),
                        float(normal.get("y", 1)),
                        float(normal.get("z", 0)),
                    ).Normalize()

                    plane = DB.Plane.CreateByNormalAndOrigin(plane_normal, plane_origin)

                    for eid in elem_id_list:
                        DB.ElementTransformUtils.MirrorElement(doc, eid, plane)

                # --- Level 1: did Revit actually commit? ---
                # commit_verified() is a low-level helper - it returns the
                # outcome only, and does not decide what response this
                # handler sends. Committed means the database accepted the
                # change; it does NOT mean the operation achieved its intent
                # (see the level-2 post-condition below).
                tx_ok, tx_status = commit_verified(t)

                if not tx_ok:
                    return routes.make_response(
                        data={
                            "status": "error",
                            "operation": operation,
                            "tx_status": tx_status,
                            "tx_ok": tx_ok,
                            "error": "Transaction did not commit (tx_status={}) - the model is unchanged.".format(tx_status),
                        },
                        status=500,
                    )

                # --- Level 2: did this operation achieve its own contract? ---
                # See docs/operation-contracts.md. Only "move" and "copy" get
                # a real post-condition here; "rotate"/"mirror" are honestly
                # not_checked rather than backed by a shaky generic check.
                if operation == "move" and pre_move_points:
                    checked = 0
                    failures = []
                    for eid in elem_id_list:
                        pre = pre_move_points.get(eid.IntegerValue)
                        if pre is None:
                            continue
                        elem = doc.GetElement(eid)
                        post = _location_point(elem)
                        if post is None:
                            continue
                        checked += 1
                        actual_disp = DB.XYZ(post.X - pre.X, post.Y - pre.Y, post.Z - pre.Z)
                        residual = (actual_disp - translation).GetLength()
                        if residual > LOCATION_TOLERANCE_FT:
                            failures.append({
                                "id": eid.IntegerValue,
                                "expected_mm": _xyz_to_mm(translation),
                                "actual_mm": _xyz_to_mm(actual_disp),
                            })
                    verified = {
                        "ok": (len(failures) == 0) if checked else None,
                        "method": "location_displacement",
                        "expected": {"vector_mm": _xyz_to_mm(translation), "count": len(elem_id_list)},
                        "actual": {"count_checked": checked, "count_ok": checked - len(failures)},
                    }
                    if not checked:
                        verified["status"] = "not_checked"
                        verified["reason"] = "No element in this batch exposes LocationPoint/LocationCurve"
                    if failures:
                        verified["failures"] = failures[:50]
                elif operation == "copy" and new_element_ids:
                    missing = [nid for nid in new_element_ids if doc.GetElement(make_element_id(nid)) is None]
                    verified = {
                        "ok": len(missing) == 0,
                        "method": "element_exists",
                        "expected": {"count": len(new_element_ids)},
                        "actual": {"count_ok": len(new_element_ids) - len(missing)},
                    }
                    if missing:
                        verified["failures"] = [{"id": nid} for nid in missing[:50]]
                else:
                    verified = {
                        "ok": None,
                        "status": "not_checked",
                        "reason": "No per-element geometric post-condition implemented yet for '{}' in this milestone; transaction-level verification (tx_status) still applies.".format(operation),
                    }

                result = {
                    "status": "success",
                    "operation": operation,
                    "count": len(elem_id_list),
                    "tx_status": tx_status,
                    "tx_ok": tx_ok,
                    "verified": verified,
                    "message": "{} {} element{}".format(
                        {"move": "Moved", "copy": "Copied", "rotate": "Rotated", "mirror": "Mirrored"}.get(operation, operation),
                        len(elem_id_list),
                        "s" if len(elem_id_list) != 1 else "",
                    ),
                }
                if operation == "copy" and new_element_ids:
                    result["new_element_ids"] = new_element_ids

                return routes.make_response(data=result)

            except Exception as tx_error:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                raise tx_error

        except Exception as e:
            logger.error("Failed to transform elements: {}".format(str(e)))
            error_trace = traceback.format_exc()
            return routes.make_response(
                data={"error": str(e), "traceback": error_trace}, status=500
            )

    logger.info("Transform routes registered successfully")
