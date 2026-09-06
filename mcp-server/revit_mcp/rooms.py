# -*- coding: UTF-8 -*-
"""
Rooms Module for Revit MCP
Handles room creation and room separation lines
"""

from utils import get_element_name, get_element_id_value, make_element_id, suppress_warnings, repair_hebrew_in, commit_verified, verify_created_elements
from pyrevit import routes, revit, DB
from System.Collections.Generic import List
import json
import traceback
import logging

logger = logging.getLogger(__name__)

MM_TO_FEET = 1.0 / 304.8


# ---------------------------------------------------------------------------
# create_room / create_room_separation cores -- transaction-agnostic,
# extracted from the two routes below with no behaviour change, shared with
# revit_mcp/ops.py's dispatcher.
# ---------------------------------------------------------------------------

def resolve_level(doc, level_name):
    """(level_elem_or_None, available_names_or_None) -- the route's own
    linear scan, unchanged. available_names is only populated on a miss."""
    levels = (
        DB.FilteredElementCollector(doc)
        .OfCategory(DB.BuiltInCategory.OST_Levels)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    for lv in levels:
        if get_element_name(lv) == level_name:
            return lv, None
    return None, [get_element_name(lv) for lv in levels]


def apply_create_room(doc, level, phase, location_mm, name, number):
    """doc.Create.NewRoom + optional Name/Number Set. Assumes an open,
    caller-owned transaction; never commits, never rolls back. Returns the
    Room, or None when Revit refused (no enclosed area) -- returns None
    rather than raising, so each caller handles it in its own idiom: the
    route rolls back + 500; the dispatcher rolls back only its own inner
    transaction and applies group policy."""
    if location_mm:
        x = float(location_mm.get("x", 0)) * MM_TO_FEET
        y = float(location_mm.get("y", 0)) * MM_TO_FEET
        point = DB.UV(x, y)
        room = doc.Create.NewRoom(level, point)
    else:
        room = doc.Create.NewRoom(phase)

    if not room:
        return None

    if name:
        name_param = room.LookupParameter("Name")
        if name_param and not name_param.IsReadOnly:
            name_param.Set(name)
    if number:
        number_param = room.LookupParameter("Number")
        if number_param and not number_param.IsReadOnly:
            number_param.Set(number)
    return room


def read_room_facts(doc, room_id):
    """{"area_sqm","name","number"} -- must be called AFTER commit (a
    freshly re-resolved element, per the measured rule that a pre-commit
    object handle can go stale once the transaction closes)."""
    room_after = doc.GetElement(DB.ElementId(room_id))
    area = 0.0
    try:
        area_param = room_after.LookupParameter("Area")
        if area_param and area_param.HasValue:
            area = round(area_param.AsDouble() * 0.092903, 2)  # sq ft to sq m
    except Exception:
        pass
    name = ""
    try:
        name_p = room_after.LookupParameter("Name")
        if name_p:
            name = name_p.AsString() or ""
    except Exception:
        pass
    number = ""
    try:
        number_p = room_after.LookupParameter("Number")
        if number_p:
            number = number_p.AsString() or ""
    except Exception:
        pass
    return {"area_sqm": area, "name": name, "number": number}


def verify_room(doc, room_id, area_sqm):
    """The room_area verified dict -- unchanged: category check AND a real
    enclosed boundary with positive area. Catches the measured trap where
    an unplaced/unenclosed Room (Area == 0) still resolves and passes a
    naive existence check."""
    cat_verified = verify_created_elements(doc, [(room_id, int(DB.BuiltInCategory.OST_Rooms))])
    boundary_ok = area_sqm > 0.0
    verified = {
        "ok": bool(cat_verified["ok"]) and boundary_ok,
        "method": "room_area",
        "expected": {"area_sqm_gt": 0},
        "actual": {"area_sqm": area_sqm, "category_check": cat_verified["ok"]},
    }
    if not boundary_ok:
        verified["reason"] = "Room has zero area - unplaced or not enclosed by boundaries"
    if cat_verified.get("failures"):
        verified["failures"] = cat_verified["failures"]
    return verified


def apply_create_room_separation(doc, sketch_plane, curve_array, target_view):
    """doc.Create.NewRoomBoundaryLines. Assumes an open, caller-owned
    transaction; never commits, never rolls back. Returns [element_id_int]."""
    created_ids = []
    separator = doc.Create.NewRoomBoundaryLines(sketch_plane, curve_array, target_view)
    if separator:
        for elem in separator:
            created_ids.append(get_element_id_value(elem))
    return created_ids


def verify_room_separation(doc, created_ids):
    return verify_created_elements(
        doc, [(cid, int(DB.BuiltInCategory.OST_RoomSeparationLines)) for cid in created_ids]
    )


def register_room_routes(api):
    """Register all room routes with the API"""

    @api.route("/create_room/", methods=["POST"])
    def create_room_handler(doc, request):
        """Create a room at a specified level."""
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)

            level_name = data.get("level_name")
            if not level_name:
                return routes.make_response(
                    data={"error": "level_name is required"}, status=400
                )

            # Find the level
            target_level, available = resolve_level(doc, level_name)

            if not target_level:
                return routes.make_response(
                    data={
                        "error": "Level '{}' not found".format(level_name),
                        "available_levels": available,
                    },
                    status=404,
                )

            # Get the phase — rooms require a phase
            phases = doc.Phases
            if not phases or phases.Size == 0:
                return routes.make_response(
                    data={"error": "No phases found in the project"},
                    status=500,
                )
            phase = phases.get_Item(phases.Size - 1)

            t = DB.Transaction(doc, "Create Room via MCP")
            t.Start()
            suppress_warnings(t)

            try:
                room_name = data.get("name")
                room_number = data.get("number")
                room = apply_create_room(doc, target_level, phase, data.get("location"), room_name, room_number)

                if not room:
                    t.RollBack()
                    return routes.make_response(
                        data={"error": "Failed to create room — no enclosed area found at the specified location. Add walls or room separation lines first."},
                        status=500,
                    )

                tx_ok, tx_status = commit_verified(t)
                if tx_ok is False:
                    return routes.make_response(
                        data={
                            "status": "error",
                            "tx_status": tx_status,
                            "tx_ok": tx_ok,
                            "error": "Transaction did not commit (tx_status={}) - no room was created.".format(tx_status),
                        },
                        status=500,
                    )

                room_id = get_element_id_value(room)
                facts = read_room_facts(doc, room_id)
                area = facts["area_sqm"]
                actual_name = facts["name"]
                actual_number = facts["number"]

                # Level 2: the room must resolve, carry OST_Rooms, AND have a
                # real enclosed boundary with positive area - "created" a
                # Room element that is unplaced/unenclosed (Area == 0) is the
                # measured room-creation trap this project's own RULES.md
                # room-definition domain exists to catch, and it passes a
                # naive "does the id resolve?" check.
                verified = verify_room(doc, room_id, area)

                return routes.make_response(
                    data={
                        "status": "success",
                        "room_id": room_id,
                        "name": actual_name,
                        "number": actual_number,
                        "level": level_name,
                        "area": area,
                        "tx_status": tx_status,
                        "tx_ok": tx_ok,
                        "verified": verified,
                        "message": "Room '{}' created on level '{}'".format(
                            actual_name or actual_number or "Unnamed", level_name
                        ),
                    }
                )

            except Exception as tx_error:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                raise tx_error

        except Exception as e:
            logger.error("Failed to create room: {}".format(str(e)))
            error_trace = traceback.format_exc()
            return routes.make_response(
                data={"error": str(e), "traceback": error_trace}, status=500
            )

    @api.route("/create_room_separation/", methods=["POST"])
    def create_room_separation_handler(doc, request):
        """Create room separation lines."""
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)

            lines = data.get("lines", [])
            if not lines:
                return routes.make_response(
                    data={"error": "No lines provided"}, status=400
                )

            # Find the view to create lines in
            view_name = data.get("view_name")
            level_name = data.get("level_name")

            target_view = None
            if view_name:
                views = (
                    DB.FilteredElementCollector(doc)
                    .OfClass(DB.ViewPlan)
                    .WhereElementIsNotElementType()
                    .ToElements()
                )
                for v in views:
                    if get_element_name(v) == view_name:
                        target_view = v
                        break
                if not target_view:
                    return routes.make_response(
                        data={"error": "View '{}' not found".format(view_name)},
                        status=404,
                    )
            else:
                # Use active view if it's a plan view
                active_view = doc.ActiveView
                if hasattr(active_view, "ViewType") and active_view.ViewType in [
                    DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan, DB.ViewType.AreaPlan
                ]:
                    target_view = active_view
                else:
                    return routes.make_response(
                        data={"error": "Active view is not a plan view — specify a view_name or switch to a plan view"},
                        status=400,
                    )

            # Find sketch plane from level
            sketch_plane = None
            if level_name:
                levels = (
                    DB.FilteredElementCollector(doc)
                    .OfCategory(DB.BuiltInCategory.OST_Levels)
                    .WhereElementIsNotElementType()
                    .ToElements()
                )
                for lv in levels:
                    if get_element_name(lv) == level_name:
                        sketch_plane = lv
                        break

            t = DB.Transaction(doc, "Create Room Separation Lines via MCP")
            t.Start()
            suppress_warnings(t)

            try:
                curve_array = DB.CurveArray()

                for line_def in lines:
                    sp = line_def.get("start_point", {})
                    ep = line_def.get("end_point", {})

                    start = DB.XYZ(
                        float(sp.get("x", 0)) * MM_TO_FEET,
                        float(sp.get("y", 0)) * MM_TO_FEET,
                        float(sp.get("z", 0)) * MM_TO_FEET,
                    )
                    end = DB.XYZ(
                        float(ep.get("x", 0)) * MM_TO_FEET,
                        float(ep.get("y", 0)) * MM_TO_FEET,
                        float(ep.get("z", 0)) * MM_TO_FEET,
                    )

                    line = DB.Line.CreateBound(start, end)
                    curve_array.Append(line)

                # Create room separation lines
                sp_plane = target_view.SketchPlane
                if not sp_plane:
                    # Create a sketch plane from the view's level
                    level_id = target_view.GenLevel.Id if target_view.GenLevel else None
                    if level_id:
                        level_elem = doc.GetElement(level_id)
                        sp_plane = DB.SketchPlane.Create(doc, level_id)

                created_ids = apply_create_room_separation(doc, sp_plane, curve_array, target_view)

                tx_ok, tx_status = commit_verified(t)
                if tx_ok is False:
                    return routes.make_response(
                        data={
                            "status": "error",
                            "tx_status": tx_status,
                            "tx_ok": tx_ok,
                            "error": "Transaction did not commit (tx_status={}) - no separation lines were created.".format(tx_status),
                        },
                        status=500,
                    )

                verified = verify_room_separation(doc, created_ids)

                return routes.make_response(
                    data={
                        "status": "success",
                        "line_count": len(created_ids),
                        "line_ids": created_ids,
                        "tx_status": tx_status,
                        "tx_ok": tx_ok,
                        "verified": verified,
                        "message": "Created {} room separation line{}".format(
                            len(created_ids),
                            "s" if len(created_ids) != 1 else ""
                        ),
                    }
                )

            except Exception as tx_error:
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                raise tx_error

        except Exception as e:
            logger.error("Failed to create room separation: {}".format(str(e)))
            error_trace = traceback.format_exc()
            return routes.make_response(
                data={"error": str(e), "traceback": error_trace}, status=500
            )

    logger.info("Room routes registered successfully")
