# -*- coding: UTF-8 -*-
"""
Design Validation Module for Revit MCP

Checks rooms against EXTERNAL, LOCALE-SUPPLIED design standards and reports
FACT vs ASSUMPTION vs PASS vs VIOLATION vs WARNING as distinct things, never
collapsed into a single pass/fail.

This engine has no jurisdiction of its own. Every regulatory concept - which
room types exist, their minimum area, and any room type that needs extra
checks beyond area (a storm shelter, a protected space, a wet room, whatever
a given building code calls out by name) - is data, supplied at runtime from
a rules file this repo does not ship. Two building codes can point at two
completely different rules files without this file changing at all; a
project in one country is not implicitly checked against another country's
numbers.

Deliberate separation of engine (this file, public) from rule VALUES
(private JSON, loaded from disk, path given by the caller or defaulting to a
per-user config location - see DEFAULT_RULES_PATH below, never a value this
repo commits). A missing rules file is reported honestly (this tool cannot
do its job without one) rather than falling back to invented defaults, and
this file contains no area/width/thickness number of its own to fall back
to even if it wanted to.

Rules file shape (see docs/operation-contracts.md for the full description
and a worked, numberless example):
  room_types: [ {id, match_keywords, min_area_sqm?, min_width_m?,
                 extended_checks?: {net_area_sqm?, relief_net_area_sqm?,
                                    wall_thickness_mm?, ceiling_height_m?,
                                    volume_cum?, width_m?}} ]
`extended_checks` is how a locale's rules file opts a specific room type
into the deeper checks (bounding-wall thickness, ceiling height, volume) -
nothing in this file hardcodes which room type that applies to, or why.

What "confidence" means in a finding:
  fact       - read directly from the model (Room.Area, Wall.Width, ...)
  assumption - inferred, not read (which rules-file room-type a free-text
               room name corresponds to). Every check built on an
               assumption carries it explicitly; a violation derived from a
               wrong assumed room-type is still reported as a violation,
               but the assumption is named so a human can override it.
  violation  - a fact fails a rule's threshold
  warning    - a fact is in a grey zone the rule itself calls out via its
               own optional `relief_net_area_sqm` (a lower figure some
               codes allow under a separate approval this engine has no way
               to confirm) - never silently treated as a pass
  not_checked - a rule this engine does not implement yet (see module
               docstring's "known limitations"), named honestly rather than
               silently skipped

Known limitations (all "not_checked", never silently passed):
  - room WIDTH (typically measured at a fixed height above floor, between
    finishes, in the kind of regulation this engine was built against) is
    not checked - it needs real cross-section geometry analysis, not a
    bounding-box approximation that would silently misreport an L-shaped or
    furnished room
  - window-area-vs-floor-area rules - need window-to-room association this
    engine does not build
  - kitchen work-triangle - needs fixture identification and pairwise
    distances, a separate feature, not a room-level check
"""
from pyrevit import routes, revit, DB
import json
import logging
import os
import traceback

from utils import get_element_id_value, make_element_id, repair_hebrew_in, normalize_string

logger = logging.getLogger(__name__)

SQFT_TO_SQM = 0.0929
CUFT_TO_CUM = 0.0283168
MM_PER_FT = 304.8

# Per-user, per-machine config - deliberately outside this repo. Point
# rules_path at whatever your own jurisdiction's rules file is; nothing
# here assumes a specific country.
DEFAULT_RULES_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "revit-design-rules", "room_standards.json"
)


def _load_rules(rules_path):
    """Read the rules file as raw bytes, not text - text-mode open() decodes
    using the interpreter's default codepage, an unnecessary extra variable.

    Measured live 2026-08-31: IronPython 2.7's json.loads does NOT UTF-8
    auto-detect a raw byte string the way CPython's does - it produced the
    exact one-Unicode-codepoint-per-UTF-8-byte mojibake that
    repair_hebrew_text/repair_hebrew_in already exist to reverse for
    request bodies elsewhere in this project (see utils.py), just from a
    file read instead of a request parse. Same fix applies here: decode the
    bytes as UTF-8 explicitly before handing them to json.loads, so the
    mojibake never has a chance to happen, rather than parsing corrupted
    text and repairing it after the fact.
    """
    if not os.path.isfile(rules_path):
        return None, "Rules file not found at {}".format(rules_path)
    try:
        with open(rules_path, "rb") as f:
            raw_bytes = f.read()
        raw = raw_bytes.decode("utf-8")
        return json.loads(raw), None
    except Exception as e:
        return None, "Rules file at {} could not be parsed: {}".format(rules_path, str(e))


def _merge_extra_rules(base_room_types, extra_rules):
    """Merge a request-scoped extra_rules body field over the loaded rules
    file's room_types, by "id": an id present in both is overridden by
    extra_rules' version; an id only in extra_rules is added; an id only in
    base_room_types is kept unchanged.

    Vertical slice for the M1-M5 architecture upgrade's design-state work
    (Milestone 5): lets a project-specific constraint (recorded, say, in
    that project's design_state.json) be checked per call with no
    file-path plumbing - and, just as important, with NO jurisdiction data
    of any kind added to this repo. extra_rules carries only whatever
    numbers the CALLER supplies at request time.

    Returns a NEW list. NEVER mutates base_room_types or extra_rules -
    accidentally mutating the shared, module-level rules dict this
    request's caller loaded would leak into a later, unrelated request
    that never asked for extra_rules at all. extra_rules is entirely
    request-scoped: nothing here writes it to disk, ever.

    extra_rules must be shaped {"room_types": [...]} - the same shape as
    the rules file itself. A missing/malformed extra_rules (not a dict, no
    "room_types" list, or empty) degrades to "no extra rules applied"
    rather than raising - this is an optional, best-effort convenience,
    not a required part of the request. An entry in either list with no
    "id" is skipped (nothing to merge it by). Two entries with the same id
    WITHIN extra_rules itself: the later one in the list wins, matching
    ordinary dict-overwrite semantics - deterministic, not an error."""
    merged = list(base_room_types)
    if not isinstance(extra_rules, dict):
        return merged
    extra_types = extra_rules.get("room_types")
    if not isinstance(extra_types, list) or not extra_types:
        return merged

    by_id = {}
    order = []
    for rt in merged:
        rid = rt.get("id") if isinstance(rt, dict) else None
        if rid is None:
            continue
        by_id[rid] = rt
        order.append(rid)
    for rt in extra_types:
        rid = rt.get("id") if isinstance(rt, dict) else None
        if rid is None:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = rt
    return [by_id[rid] for rid in order]


def _match_room_type(name, room_types):
    """Case-insensitive substring match. Hebrew has no case distinction, so
    .lower() is a no-op there; for a Latin-script rules file (English,
    German, ...) it means "Kitchen"/"kitchen"/"KITCHEN" all match the same
    rule instead of silently depending on whichever casing happened to be
    typed in the room name."""
    if not name:
        return None
    name_lower = name.lower()
    for rt in room_types:
        for kw in rt.get("match_keywords", []):
            if kw.lower() in name_lower:
                return rt
    return None


def _bounding_wall_thicknesses_mm(doc, room):
    """Direct re-use of the boundary-walk impact.py's _room_boundary_map uses,
    scoped to one room since validation checks rooms individually rather than
    in the batch-of-arbitrary-elements shape analyze_relationships does."""
    thicknesses = []
    try:
        opts = DB.SpatialElementBoundaryOptions()
        loops = room.GetBoundarySegments(opts)
    except Exception:
        return thicknesses
    seen = set()
    for loop in loops:
        for seg in loop:
            try:
                wid = get_element_id_value(seg.ElementId)
            except Exception:
                continue
            if wid in seen:
                continue
            seen.add(wid)
            w = doc.GetElement(make_element_id(wid))
            if w is None:
                continue
            try:
                thicknesses.append({"wall_id": wid, "thickness_mm": round(w.Width * MM_PER_FT, 1)})
            except Exception:
                continue
    return thicknesses


def _check_room_type(name, area_sqm, room_types):
    """Generic room-type-minimum check. `rt` (the matched rule) is returned
    alongside the findings so the caller can also run _check_extended on it
    without re-matching - one match, two independent sets of checks."""
    rt = _match_room_type(name, room_types)
    if rt is None:
        return None, [{
            "kind": "not_checked", "category": "room_type_minimum",
            "message": "No room-type rule matched this name - not a room type this ruleset covers, or the name doesn't contain a recognized keyword.",
        }]
    findings = [{
        "kind": "assumption", "category": "room_type_minimum", "rule_id": rt["id"],
        "message": "Room name matched keyword rule '{}' - room-type is inferred from the free-text name, not read as a model fact.".format(rt["id"]),
    }]
    min_area = rt.get("min_area_sqm")
    if min_area is not None:
        if area_sqm + 1e-6 < min_area:
            findings.append({
                "kind": "violation", "category": "room_type_minimum", "rule_id": rt["id"],
                "message": "Area {:.2f} sqm is below the {:.1f} sqm minimum for room type '{}'.".format(area_sqm, min_area, rt["id"]),
                "measured": round(area_sqm, 2), "required": min_area,
            })
        else:
            findings.append({
                "kind": "pass", "category": "room_type_minimum", "rule_id": rt["id"],
                "measured": round(area_sqm, 2), "required": min_area,
            })
    if rt.get("min_width_m") is not None:
        findings.append({
            "kind": "not_checked", "category": "room_type_minimum_width", "rule_id": rt["id"],
            "message": "Minimum width ({:.2f} m) is not checked by this engine - see module known limitations.".format(rt["min_width_m"]),
        })
    return rt, findings


def _check_extended(doc, room, area_sqm, rt):
    """Generic version of a "this room type needs more than an area check"
    rule. Nothing below names or assumes any specific room type or
    jurisdiction - every threshold comes from rt["extended_checks"], and
    every category name is built from rt["id"], the rules file's own label
    for whichever room type opted into this. A rules file for one building
    code might use this for a protected/safe room; another might use it for
    a wet room, a storm shelter, or nothing at all - this function does not
    know or care which."""
    checks = rt.get("extended_checks")
    if not checks:
        return []

    rid = rt["id"]
    findings = [{
        "kind": "assumption", "category": rid,
        "message": "Room name matched the '{}' rule's extended-checks keywords.".format(rid),
    }]

    net_area = checks.get("net_area_sqm")
    relief = checks.get("relief_net_area_sqm")
    if net_area is not None:
        if area_sqm + 1e-6 >= net_area:
            findings.append({"kind": "pass", "category": "{}_area".format(rid), "measured": round(area_sqm, 2), "required": net_area})
        elif relief is not None and area_sqm + 1e-6 >= relief:
            findings.append({
                "kind": "warning", "category": "{}_area".format(rid),
                "message": "Area {:.2f} sqm is below the standard {:.1f} sqm minimum but meets the '{}' rule's own relief figure ({:.1f} sqm) - relief requires an approval this engine cannot confirm.".format(area_sqm, net_area, rid, relief),
                "measured": round(area_sqm, 2), "required": net_area,
            })
        else:
            findings.append({
                "kind": "violation", "category": "{}_area".format(rid),
                "message": "Area {:.2f} sqm is below both the {:.1f} sqm minimum and the {:.1f} sqm relief figure.".format(area_sqm, net_area, relief or net_area),
                "measured": round(area_sqm, 2), "required": net_area,
            })
        findings.append({
            "kind": "not_checked", "category": "{}_area".format(rid),
            "message": "This engine reports Room.Area as-is; whether it equals 'net area, excluding walls' as this rule's net_area_sqm intends depends on the project's Area Boundary Location setting.",
        })

    min_thick = checks.get("wall_thickness_mm")
    if min_thick is not None:
        walls = _bounding_wall_thicknesses_mm(doc, room)
        if not walls:
            findings.append({"kind": "not_checked", "category": "{}_wall_thickness".format(rid), "message": "No bounding walls resolved for this room."})
        else:
            thin = [w for w in walls if w["thickness_mm"] + 1e-6 < min_thick]
            if thin:
                findings.append({
                    "kind": "violation", "category": "{}_wall_thickness".format(rid),
                    "message": "{} of {} bounding wall(s) are thinner than the {}mm minimum.".format(len(thin), len(walls), min_thick),
                    "measured": walls, "required": min_thick,
                })
            else:
                findings.append({"kind": "pass", "category": "{}_wall_thickness".format(rid), "measured": walls, "required": min_thick})

    min_h, max_h = None, None
    height_range = checks.get("ceiling_height_m")
    if height_range:
        min_h, max_h = height_range[0], height_range[1]
    if min_h is not None or max_h is not None:
        height_param = room.LookupParameter("Unbounded Height")
        if height_param is None or not height_param.HasValue:
            findings.append({"kind": "not_checked", "category": "{}_ceiling_height".format(rid), "message": "'Unbounded Height' parameter not available on this room."})
        else:
            height_m = height_param.AsDouble() * MM_PER_FT / 1000.0
            out_of_range = (min_h is not None and height_m + 1e-6 < min_h) or (max_h is not None and height_m - 1e-6 > max_h)
            f = {
                "kind": "violation" if out_of_range else "pass",
                "category": "{}_ceiling_height".format(rid),
                "measured": round(height_m, 2), "required": [min_h, max_h],
            }
            if out_of_range:
                f["message"] = "Height {:.2f} m is outside the {:.2f}-{:.2f} m range. Approximated from the room's 'Unbounded Height' parameter, not independently confirmed against a modeled Ceiling element.".format(height_m, min_h or 0, max_h or 0)
            findings.append(f)

    min_vol = checks.get("volume_cum")
    if min_vol is not None:
        vol_param = room.get_Parameter(DB.BuiltInParameter.ROOM_VOLUME)
        if vol_param is None or not vol_param.HasValue:
            findings.append({"kind": "not_checked", "category": "{}_volume".format(rid), "message": "Volume computation is not enabled for this model (Area and Volume Computations setting), or has no value."})
        else:
            vol_cum = vol_param.AsDouble() * CUFT_TO_CUM
            findings.append({
                "kind": "violation" if vol_cum + 1e-6 < min_vol else "pass",
                "category": "{}_volume".format(rid),
                "measured": round(vol_cum, 2), "required": min_vol,
            })

    if checks.get("width_m") is not None:
        findings.append({
            "kind": "not_checked", "category": "{}_width".format(rid),
            "message": "Minimum width ({:.2f} m) is not checked - see module known limitations.".format(checks["width_m"]),
        })

    return findings


def register_validation_routes(api):
    """Register design-validation routes with the API."""

    @api.route("/validate_design/", methods=["POST"])
    def validate_design_handler(doc, request):
        """Check rooms against external design standards, read-only."""
        try:
            if not doc:
                return routes.make_response(
                    data={"error": "No active Revit document"}, status=503
                )

            data = {}
            if request and request.data:
                data = json.loads(request.data) if isinstance(request.data, str) else request.data
                data = repair_hebrew_in(data)

            rules_path = data.get("rules_path") or DEFAULT_RULES_PATH
            room_ids = data.get("room_ids")
            extra_rules = data.get("extra_rules")

            rules, err = _load_rules(rules_path)
            if err:
                return routes.make_response(
                    data={
                        "error": err,
                        "hint": "Design-standard rule values are per-user, per-jurisdiction config, not part of this repo - pass rules_path explicitly, or create one at the default path (see docs/operation-contracts.md for the schema).",
                        "default_path": DEFAULT_RULES_PATH,
                    },
                    status=404,
                )

            # extra_rules is request-scoped only - merged into a NEW list,
            # never written back into `rules` (the loaded rules-file dict)
            # or to disk. See _merge_extra_rules's own docstring.
            room_types = _merge_extra_rules(rules.get("room_types", []), extra_rules)

            if room_ids:
                rooms = []
                not_found = []
                for rid in room_ids:
                    r = doc.GetElement(make_element_id(rid))
                    if r is None:
                        not_found.append(rid)
                    else:
                        rooms.append(r)
            else:
                rooms = list(
                    DB.FilteredElementCollector(doc)
                    .OfCategory(DB.BuiltInCategory.OST_Rooms)
                    .WhereElementIsNotElementType()
                )
                not_found = []

            results = []
            violation_count = 0
            warning_count = 0
            for room in rooms:
                try:
                    if not room.Location:
                        continue
                    name_param = room.get_Parameter(DB.BuiltInParameter.ROOM_NAME)
                    name = normalize_string(name_param.AsString() if name_param else "")
                    area_param = room.get_Parameter(DB.BuiltInParameter.ROOM_AREA)
                    area_sqm = (area_param.AsDouble() * SQFT_TO_SQM) if (area_param and area_param.HasValue) else 0.0

                    findings = [{"kind": "fact", "category": "room", "message": "name={}, area_sqm={:.2f}".format(name, area_sqm)}]
                    rt, type_findings = _check_room_type(name, area_sqm, room_types)
                    findings.extend(type_findings)
                    if rt is not None:
                        findings.extend(_check_extended(doc, room, area_sqm, rt))

                    for f in findings:
                        if f["kind"] == "violation":
                            violation_count += 1
                        elif f["kind"] == "warning":
                            warning_count += 1

                    results.append({
                        "room_id": get_element_id_value(room),
                        "room_name": name,
                        "area_sqm": round(area_sqm, 2),
                        "findings": findings,
                    })
                except Exception as room_err:
                    results.append({
                        "room_id": get_element_id_value(room) if room else None,
                        "findings": [{"kind": "not_checked", "category": "room", "message": "Could not evaluate: {}".format(str(room_err))}],
                    })

            message = "Checked {} room{}: {} violation{}, {} warning{}".format(
                len(results), "s" if len(results) != 1 else "",
                violation_count, "s" if violation_count != 1 else "",
                warning_count, "s" if warning_count != 1 else "",
            )

            return routes.make_response(data={
                "status": "success",
                "rules_source": rules.get("source"),
                "rules_path": rules_path,
                "extra_rules_applied": bool(extra_rules),
                "results": results,
                "not_found": not_found,
                "violation_count": violation_count,
                "warning_count": warning_count,
                "message": message,
            })

        except Exception as e:
            logger.error("Failed to validate design: {}".format(str(e)))
            return routes.make_response(
                data={"error": str(e), "traceback": traceback.format_exc()}, status=500
            )

    logger.info("Validation routes registered successfully")
