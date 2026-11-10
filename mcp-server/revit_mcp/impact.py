# -*- coding: UTF-8 -*-
"""
Impact Analysis Module for Revit MCP

Answers "what is connected to this element, and what would actually break if
I changed or removed it" -- vision-doc Phase 3 (causality / impact analysis).
Deferred behind the Milestone 0-3 hard gate until the execution layer itself
was verified; see docs/operation-contracts.md for that verification story.

Two tools, two different levels of truth -- deliberately not collapsed into
one, because they answer different questions:

  analyze_relationships -- static, READ-ONLY inspection: GetDependentElements,
    JoinGeometryUtils, Host/hosted-by, room-boundary membership. Opens no
    transaction at all. Measured live on this project's model (44 walls, 16
    rooms): GetDependentElements(None) ~0.4ms/element, GetJoinedElements
    ~0.4ms/element, GetBoundarySegments ~0.3ms/room -- cheap enough to run
    for an arbitrary batch of ids on demand. This is Revit's own
    *informational* dependency graph, not a guarantee of what a real delete
    would touch (see preview_delete_impact for that).

  preview_delete_impact -- authoritative: performs the actual doc.Delete()
    inside a transaction, reads back the real (possibly cascaded) set of
    element ids Revit decided to remove, then the transaction is ALWAYS
    rolled back, never committed -- the same guaranteed-rollback discipline
    the tracker's own snapshot ops use so a read never touches Undo. This is
    Revit's own delete/cascade logic running for real, not a re-derived
    guess, which is why it is the one to trust for "what would actually be
    affected" -- GetDependentElements is explicitly one-level and
    informational; it does not claim to predict the full cascade closure.

Both are read-only from the caller's point of view. Neither needs
commit_verified: analyze_relationships never opens a Transaction, and
preview_delete_impact's Transaction is never the thing being verified -- its
whole contract is "always ends in RolledBack", checked explicitly below
rather than via the pass/fail semantics commit_verified was built for.
"""
from pyrevit import routes, revit, DB
import json
import logging
import traceback

from utils import get_element_name, get_element_id_value, make_element_id, repair_hebrew_in, suppress_warnings

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPENDENTS = 50


def _describe(elem):
    if elem is None:
        return None
    cat = "Unknown"
    try:
        if elem.Category:
            cat = elem.Category.Name
    except Exception:
        pass
    return {
        "id": get_element_id_value(elem),
        "name": get_element_name(elem),
        "category": cat,
    }


def _describe_id(doc, elid_int):
    """Describe an element by its raw int id, resolving it fresh. Used only
    where the id came from a source (e.g. doc.Delete's return) that isn't
    already holding the Element."""
    try:
        elem = doc.GetElement(make_element_id(elid_int))
    except Exception:
        elem = None
    if elem is None:
        return {"id": elid_int, "name": None, "category": "Unknown (already removed)"}
    return _describe(elem)


def _dependents(doc, el, max_items):
    """GetDependentElements(None): Revit's own informational dependency list,
    grouped by category. Excludes the element's own id -- observed live that
    a wall's dependent list can include entries that are not the wall's own
    intended "children" (e.g. touching neighbours), so this stays a
    descriptive inventory, not a claim about what a delete would remove."""
    try:
        dep_ids = list(el.GetDependentElements(None))
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    self_id = el.Id
    by_category = {}
    total = 0
    truncated = False
    for did in dep_ids:
        if did == self_id:
            continue
        total += 1
        if total > max_items:
            truncated = True
            continue
        dep_elem = doc.GetElement(did)
        cat = "Unknown"
        try:
            if dep_elem is not None and dep_elem.Category:
                cat = dep_elem.Category.Name
        except Exception:
            pass
        by_category.setdefault(cat, []).append(_describe(dep_elem) if dep_elem else {"id": get_element_id_value(did)})
    return {"total": total, "truncated": truncated, "by_category": by_category}


def _joined_with(doc, el):
    """JoinGeometryUtils.GetJoinedElements: not every category supports
    geometry joins (e.g. a Room), so failure here is expected, not an error."""
    try:
        joined = list(DB.JoinGeometryUtils.GetJoinedElements(doc, el))
    except Exception:
        return None
    out = []
    for jid in joined:
        je = doc.GetElement(jid)
        if je is not None:
            out.append(_describe(je))
    return out


def _host_info(el):
    try:
        h = getattr(el, "Host", None)
    except Exception:
        h = None
    return _describe(h) if h is not None else None


def _hosted_elements(doc, target_ids):
    """One collector pass over all FamilyInstances, grouped by host id --
    covers every queried element's hosted content in a single scan rather
    than one scan per element."""
    by_host = {}
    try:
        instances = (
            DB.FilteredElementCollector(doc)
            .OfClass(DB.FamilyInstance)
            .WhereElementIsNotElementType()
        )
    except Exception:
        return by_host
    target_set = set(target_ids)
    for inst in instances:
        try:
            host = getattr(inst, "Host", None)
        except Exception:
            host = None
        if host is None:
            continue
        hid = get_element_id_value(host)
        if hid in target_set:
            by_host.setdefault(hid, []).append(_describe(inst))
    return by_host


def _room_boundary_map(doc):
    """One pass over all Rooms: element_id -> [room descriptions] for every
    element that bounds at least one room. Cheap (measured live: 4.7ms for
    16 rooms / 93 segments on this project) so it is always built fresh
    rather than cached -- there is no snapshot-format dependency here."""
    by_element = {}
    try:
        rooms = (
            DB.FilteredElementCollector(doc)
            .OfCategory(DB.BuiltInCategory.OST_Rooms)
            .WhereElementIsNotElementType()
        )
        opts = DB.SpatialElementBoundaryOptions()
    except Exception:
        return by_element
    for room in rooms:
        try:
            if not room.Location:
                continue
            room_desc = _describe(room)
            loops = room.GetBoundarySegments(opts)
            seen_for_this_room = set()
            for loop in loops:
                for seg in loop:
                    try:
                        seg_id = get_element_id_value(seg.ElementId)
                    except Exception:
                        continue
                    if seg_id in seen_for_this_room:
                        continue
                    seen_for_this_room.add(seg_id)
                    by_element.setdefault(seg_id, []).append(room_desc)
        except Exception:
            continue
    return by_element


def _in_room(doc, el):
    """For a point-located element (furniture, fixtures): the Room its
    LocationPoint currently resolves into, via Document.GetRoomAtPoint.
    Returns None for anything without a usable LocationPoint, or where no
    room resolves at that point -- both are legitimate answers, not errors."""
    try:
        loc = el.Location
        if not isinstance(loc, DB.LocationPoint):
            return None
        room = doc.GetRoomAtPoint(loc.Point)
    except Exception:
        return None
    return _describe(room) if room is not None else None


def register_impact_routes(api):
    """Register impact-analysis routes with the API."""

    @api.route("/analyze_relationships/", methods=["POST"])
    def analyze_relationships_handler(doc, request):
        """Static, read-only relationship inspection for one or more elements."""
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = {}
            if request and request.data:
                data = json.loads(request.data) if isinstance(request.data, str) else request.data
                data = repair_hebrew_in(data)

            element_ids = data.get("element_ids", [])
            if not element_ids:
                return routes.make_response(
                    data={"error": "No element_ids provided"}, status=400
                )
            max_dependents = int(data.get("max_dependents", DEFAULT_MAX_DEPENDENTS))

            resolved = {}
            not_found = []
            for raw_id in element_ids:
                try:
                    elid = make_element_id(raw_id)
                    elem = doc.GetElement(elid)
                except Exception:
                    elem = None
                if elem is None:
                    not_found.append(raw_id)
                else:
                    resolved[get_element_id_value(elem)] = elem

            hosted_map = _hosted_elements(doc, resolved.keys())
            boundary_map = _room_boundary_map(doc) if resolved else {}

            results = []
            for elid_int, elem in resolved.items():
                cat = "Unknown"
                try:
                    if elem.Category:
                        cat = elem.Category.Name
                except Exception:
                    pass
                results.append({
                    "id": elid_int,
                    "category": cat,
                    "name": get_element_name(elem),
                    "dependents": _dependents(doc, elem, max_dependents),
                    "joined_with": _joined_with(doc, elem),
                    "host": _host_info(elem),
                    "hosted_elements": hosted_map.get(elid_int, []),
                    "bounds_rooms": boundary_map.get(elid_int, []),
                    "in_room": _in_room(doc, elem),
                })

            message = "Analyzed relationships for {} element{}".format(
                len(results), "s" if len(results) != 1 else ""
            )
            if not_found:
                message += " ({} id{} not found)".format(len(not_found), "s" if len(not_found) != 1 else "")

            return routes.make_response(data={
                "status": "success",
                "results": results,
                "not_found": not_found,
                "message": message,
            })

        except Exception as e:
            logger.error("Failed to analyze relationships: {}".format(str(e)))
            return routes.make_response(
                data={"error": str(e), "traceback": traceback.format_exc()}, status=500
            )

    @api.route("/preview_delete_impact/", methods=["POST"])
    def preview_delete_impact_handler(doc, request):
        """Authoritative delete-impact dry run.

        Runs the real doc.Delete() inside a transaction so Revit's own
        cascade logic (hosted elements, dangling dimensions, etc.) executes
        for real, then ALWAYS rolls back -- never commits, regardless of
        outcome. Nothing persists; this is a preview, not a mutation, and it
        deliberately does not go through commit_verified, whose whole
        contract is "did the change stick" -- the opposite of the guarantee
        this handler makes.
        """
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = {}
            if request and request.data:
                data = json.loads(request.data) if isinstance(request.data, str) else request.data
                data = repair_hebrew_in(data)

            element_ids = data.get("element_ids", [])
            if not element_ids:
                return routes.make_response(
                    data={"error": "No element_ids provided"}, status=400
                )

            # Describe every requested element BEFORE anything is deleted --
            # once doc.Delete() runs inside the open transaction, cascaded
            # ids stop resolving via doc.GetElement() immediately, well
            # before RollBack() is ever called. GetDependentElements(None)
            # here is a pre-delete candidate probe only, used to describe
            # ids that the real delete cascade also touches; it is not
            # trusted as the answer itself (see module docstring).
            requested = {}
            not_found = []
            pre_described = {}
            for raw_id in element_ids:
                try:
                    elid = make_element_id(raw_id)
                    elem = doc.GetElement(elid)
                except Exception:
                    elem = None
                if elem is None:
                    not_found.append(raw_id)
                    continue
                int_id = get_element_id_value(elem)
                requested[int_id] = elid
                pre_described[int_id] = _describe(elem)
                try:
                    for dep_id in elem.GetDependentElements(None):
                        dep_int = get_element_id_value(dep_id)
                        if dep_int not in pre_described:
                            dep_elem = doc.GetElement(dep_id)
                            if dep_elem is not None:
                                pre_described[dep_int] = _describe(dep_elem)
                except Exception:
                    pass

            if not requested:
                return routes.make_response(
                    data={"error": "None of the provided element_ids resolved", "not_found": not_found},
                    status=404,
                )

            t = DB.Transaction(doc, "Preview Delete Impact via MCP (dry run - always rolled back)")
            t.Start()
            suppress_warnings(t)

            per_requested = {}
            all_deleted = set()
            delete_errors = []
            tx_status_final = None
            try:
                for int_id, elid in requested.items():
                    try:
                        result = doc.Delete(elid)
                        ids = [get_element_id_value(x) for x in result] if result else [int_id]
                    except Exception as del_err:
                        ids = []
                        delete_errors.append({"id": int_id, "reason": str(del_err)})
                    per_requested[int_id] = ids
                    all_deleted.update(ids)
            finally:
                # The whole point of this handler: no matter what happened
                # above, never commit. RollBack() unconditionally, guarded
                # the same way delete_elements_handler guards its own
                # exception path, so a transaction already ended by the
                # failure preprocessor is not rolled back a second time.
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()
                    tx_status_final = "RolledBack"
                else:
                    tx_status_final = str(t.GetStatus()) if t.HasEnded() else "Unknown"

            cascaded = sorted(i for i in all_deleted if i not in requested)
            affected = []
            for int_id in sorted(all_deleted):
                desc = pre_described.get(int_id) or _describe_id(doc, int_id)
                affected.append(desc)

            message = "Deleting {} requested element{} would actually remove {} element{} total".format(
                len(requested), "s" if len(requested) != 1 else "",
                len(all_deleted), "s" if len(all_deleted) != 1 else "",
            )
            if cascaded:
                message += " ({} cascaded)".format(len(cascaded))

            return routes.make_response(data={
                "status": "success",
                "tx_status": tx_status_final,
                "requested_count": len(requested),
                "requested_ids": list(requested.keys()),
                "would_delete_count": len(all_deleted),
                "would_delete_ids": sorted(all_deleted),
                "cascaded_ids": cascaded,
                "affected": affected,
                "per_requested_element": per_requested,
                "delete_errors": delete_errors,
                "not_found": not_found,
                "message": message,
                "note": "Dry run only - the transaction was rolled back, nothing was actually deleted.",
            })

        except Exception as e:
            logger.error("Failed to preview delete impact: {}".format(str(e)))
            return routes.make_response(
                data={"error": str(e), "traceback": traceback.format_exc()}, status=500
            )

    logger.info("Impact analysis routes registered successfully")
