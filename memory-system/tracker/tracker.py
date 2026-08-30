# -*- coding: ascii -*-
#
# Revit Project Tracker -- library (phase 1)
#
# ASCII SOURCE ONLY. Never place a non-ASCII character in this file.
# Hebrew exists only as DATA, and only inside JSON strings written with
# ensure_ascii=True, or in files written by Claude Code (never from here).
#
# Runs inside Revit through pyRevit routes (IronPython 2.7).
# Host injects into our globals: doc, DB, revit, clr, System, print
#
# The host wraps every execution in a DB.Transaction and commits it.
# We never open a transaction. Read-only passes end by raising TrackerOK,
# which forces the host to RollBack -- leaving the user's Undo stack clean.
# The real payload is always on disk before we raise.
#
# Entry point:  main(op, args)
#
import sys
import os
import json
import hashlib
import re
import traceback

TRACKER_VERSION = 1

# Bump this whenever anything changes HOW a record is computed -- the element
# filter, a signature function, the field layout. A stored baseline built by a
# different format version is not comparable, and comparing anyway produces
# phantom additions and deletions. Measured the hard way: widening the element
# filter once reported 966 deletions that never happened.
# 2 -> 3 (2026-08-16): VIEWER_TARGET_ELEVATION and VIEWER_EYE_ELEVATION added to
# VOLATILE_NAMES. Those are excluded from par_sig, so the `par` hash of every
# view-family element is now computed differently and old baselines are not
# comparable. Note this is the opposite call from the `pv` blob field added the
# same day, which deliberately did NOT bump: the blob is excluded from change
# detection, so it altered reporting only, never the comparison itself.
SNAPSHOT_FORMAT = 3

# Where tracker.py and last_run.json live. The wire never has to carry this
# path, and it must stay ASCII (see the module header), so it is derived from
# the home directory rather than from the (possibly non-ASCII) tracking-data
# path passed in `args["base"]`. Override with REVIT_TRACKER_DIR if the install
# lives somewhere else.
LIB_DIR = os.environ.get(
    "REVIT_TRACKER_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker"),
)
RESULT_PATH = os.path.join(LIB_DIR, "last_run.json")

BUDGET_MS_DEFAULT = 18000.0

# Governs the incremental gate fields only (hdr["gate"]). Independent of
# SNAPSHOT_FORMAT: a record produced incrementally is byte-identical to one
# produced by build_snapshot_chunked (both go through make_record), so the
# comparison semantics never change and a gate mismatch must fall back to a
# full rebuild THIS RUN ONLY -- never rebaseline. Only SNAPSHOT_FORMAT may
# rebaseline (see the compat check in op_snapshot).
GATE_SCHEMA = 1

# Bumped when the shape of a buffered DocumentChanged event dict changes.
# The events an incremental run drains were produced by a handler closure
# armed by a PREVIOUS load of tracker.py (see the staleness note on
# _tx_is_ours near _make_handler) -- if that closure predates a shape
# change, the events are unreadable and incremental must fall back once.
EVENT_SCHEMA = 1

# Hard ceiling on UI-thread occupancy per MCP call, for the sweep phase of
# an incremental checkpoint. Revit's ExternalEvent has no yield primitive,
# so this is not a niceness knob -- past it Windows can mark Revit "Not
# Responding". A sweep over budget chunks across multiple calls instead of
# running longer in one. Deliberately separate from BUDGET_MS_DEFAULT, which
# bounds total wire time; this bounds how long the UI freezes per call.
UI_BUDGET_MS = 2500.0

# Below this element count a full rebuild is already ~2s and the incremental
# machinery is not obviously worth its own overhead.
INCR_MIN_N = 300

# Force a full rebuild after this many consecutive incremental runs, so any
# single element's staleness is bounded even though the drift sampler below
# only reliably catches SYSTEMIC drift, not a single stale element.
INCR_MAX_STREAK = 20
INCR_MAX_DIRTY_FRAC = 0.5
INCR_MAX_FULL_AGE_S = 7200.0

# Sweep-vs-snapshot membership reconciliation: this many discrepancies get
# folded into the dirty set and re-verified once; more than this means
# someone is editing underneath us or the listener has drifted too far to
# trust for this run.
MAX_SWEEP_REPAIR = 200

# Drift audit: re-hash a small sample of non-dirty rows every incremental
# run, to catch a bug in the incremental logic itself quickly. Detects
# systemic drift fast; does NOT reliably catch a single stale element --
# that is bounded by INCR_MAX_STREAK instead, not by this sample.
AUDIT_FRAC = 0.02
AUDIT_MIN = 10
AUDIT_MAX = 100

# Fields compared by the diff, in TSV order after the uid.
FIELDS = ("cat", "typ", "lvl", "geo", "par")


class TrackerOK(Exception):
    """Raised on purpose to force the host transaction to roll back.

    Not an error. The result is already written to RESULT_PATH.
    Message format:  TRACKER_OK|<run_id>
    """
    pass


class TrackerAbort(Exception):
    """A real, unrecoverable problem. Also rolls back, but means failure."""
    pass


class IncrementalGiveUp(Exception):
    """Raised inside the incremental path to abandon it for THIS run only.

    Caught in op_snapshot; execution falls through to the ordinary full
    build_snapshot_chunked path. Never causes a rebaseline -- the stored
    baseline and prev/prev_hdr are untouched. The message is the reason
    code, appended to result["notes"] as INCR_FALLBACK_<reason>.
    """
    pass


# ---------------------------------------------------------------------------
# clock / budget
# ---------------------------------------------------------------------------

_CLOCK = {"t0": None}


def start_clock():
    _CLOCK["t0"] = System.DateTime.UtcNow


def elapsed_ms():
    if _CLOCK["t0"] is None:
        return 0.0
    return (System.DateTime.UtcNow - _CLOCK["t0"]).TotalMilliseconds


def now_iso():
    return System.DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss")


def new_run_id():
    stamp = System.DateTime.Now.ToString("yyyyMMdd-HHmmss")
    tail = System.Guid.NewGuid().ToString()[:4]
    return "r-%s-%s" % (stamp, tail)


# ---------------------------------------------------------------------------
# file io -- always through System.IO, always UTF8 without BOM
#
# UTF8Encoding(False) is deliberate. System.Text.Encoding.UTF8 emits a BOM,
# and json.loads on a BOM-prefixed string raises an error that looks random.
# ---------------------------------------------------------------------------

def _enc():
    return System.Text.UTF8Encoding(False)


def file_exists(path):
    try:
        return System.IO.File.Exists(path)
    except Exception:
        return False


def ensure_dir(path):
    if not System.IO.Directory.Exists(path):
        System.IO.Directory.CreateDirectory(path)
    return path


def read_text(path):
    if not file_exists(path):
        return None
    return System.IO.File.ReadAllText(path, _enc())


def read_lines(path):
    if not file_exists(path):
        return None
    return System.IO.File.ReadAllLines(path, _enc())


def write_text_atomic(path, text):
    """Write via .tmp then swap. Never leaves a half-written file in place."""
    tmp = path + ".tmp"
    System.IO.File.WriteAllText(tmp, text, _enc())
    if file_exists(path):
        System.IO.File.Replace(tmp, path, path + ".bak", True)
    else:
        System.IO.File.Move(tmp, path)


def write_lines_atomic(path, lines):
    tmp = path + ".tmp"
    arr = System.Array[System.String](list(lines))
    System.IO.File.WriteAllLines(tmp, arr, _enc())
    if file_exists(path):
        System.IO.File.Replace(tmp, path, path + ".bak", True)
    else:
        System.IO.File.Move(tmp, path)


def append_lines(path, lines):
    """Append with FileShare.Read so Claude Code can read while we write."""
    lines = list(lines)
    if not lines:
        return
    fs = System.IO.FileStream(
        path,
        System.IO.FileMode.Append,
        System.IO.FileAccess.Write,
        System.IO.FileShare.Read,
    )
    sw = System.IO.StreamWriter(fs, _enc())
    try:
        for ln in lines:
            sw.WriteLine(ln)
    finally:
        sw.Close()


def load_json(path, default=None):
    txt = read_text(path)
    if txt is None:
        return default
    txt = txt.lstrip(u"\ufeff").strip()
    if not txt:
        return default
    try:
        return json.loads(txt)
    except Exception:
        return default


def save_json(path, obj):
    write_text_atomic(path, json.dumps(obj, ensure_ascii=True, indent=1, sort_keys=True))


def jdump(obj):
    """Compact, guaranteed single-line, guaranteed pure ASCII."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# revit value helpers
# ---------------------------------------------------------------------------

def eid(x):
    """ElementId -> int, across Revit 2024..2027 (.Value vs .IntegerValue)."""
    if x is None:
        return -1
    v = getattr(x, "Value", None)
    if v is not None:
        return int(v)
    try:
        return int(x.IntegerValue)
    except Exception:
        return -1


def q(v):
    """Quantize a coordinate. 1e-4 ft is about 0.03 mm. The +0.0 kills -0.0."""
    return round(float(v), 4) + 0.0


def h12(obj):
    s = jdump(obj)
    return hashlib.md5(s.encode("ascii")).hexdigest()[:12]


def safe_folder_name(s):
    """Folder-safe. Hebrew is allowed here; this is a data path, not source."""
    bad = u'<>:"/\\|?*'
    out = []
    for ch in s:
        if ch in bad or ord(ch) < 32:
            out.append(u"_")
        else:
            out.append(ch)
    r = u"".join(out).strip().rstrip(u".")
    r = r[:60].strip()
    if not r:
        r = u"project"
    return r


def canon_path(p):
    if not p:
        return u""
    try:
        return System.IO.Path.GetFullPath(p).ToLowerInvariant()
    except Exception:
        try:
            return p.lower()
        except Exception:
            return p


# ---------------------------------------------------------------------------
# volatile parameters -- static list only in phase 1
#
# Built by name lookup so a parameter missing from a given Revit version
# degrades quietly instead of raising.
# ---------------------------------------------------------------------------

VOLATILE_NAMES = (
    "HOST_AREA_COMPUTED", "HOST_VOLUME_COMPUTED", "HOST_PERIMETER_COMPUTED",
    "ROOM_AREA", "ROOM_VOLUME", "ROOM_PERIMETER", "ROOM_COMPUTATION_HEIGHT",
    "EDITED_BY", "MODEL_UPDATER_STATUS", "SHEET_CURRENT_REVISION",
    "VIEW_PART_VISIBILITY", "ELEM_CATEGORY_PARAM", "ELEM_CATEGORY_PARAM_MT",
    "IFC_GUID", "IFC_TYPE_GUID", "EXPORT_LAYER_ID",
    "CURVE_ELEM_LENGTH", "STAIRS_ACTUAL_NUM_RISERS",
    # Camera position of a 3D view. Revit recomputes these when the document is
    # reopened, with no user action and no covering transaction, so every
    # session opened with a diff of exactly two phantom "modified" elements --
    # the 3D View and its Camera, which carry the same values.
    #
    # Identified 2026-08-16 by the `pv` parameter map, on the first session
    # opened after it existed: VIEWER_TARGET_ELEVATION 12.897 -> 14.0619 and
    # VIEWER_EYE_ELEVATION 6.5897 -> 20.3192, on an unmodified, unsaved-changes
    # -discarded document. It was NOT the parameter previously assumed.
    #
    # This is view navigation, not model content -- the same category as
    # VIEW_PART_VISIBILITY above -- and the tracker's stated scope is model and
    # annotation content.
    #
    # Little is actually given up. Verified by setting the parameter directly:
    # the View element then reports no change at all, while the Camera element
    # still reports a `geo` delta, because writing the parameter physically
    # relocates it. So a deliberate camera move is still visible through geo;
    # only the reopen-time recomputation, which touches `par` alone, goes quiet.
    "VIEWER_TARGET_ELEVATION", "VIEWER_EYE_ELEVATION",
)


def build_volatile():
    out = set()
    for nm in VOLATILE_NAMES:
        b = getattr(DB.BuiltInParameter, nm, None)
        if b is None:
            continue
        try:
            out.add(int(System.Convert.ToInt32(b)))
        except Exception:
            try:
                out.add(int(b))
            except Exception:
                pass
    return out


JUNK_NAMES = (
    "OST_IOSSketchGrid", "OST_SketchLines", "OST_ExtentElem", "OST_SunPath",
    "OST_IOSModelGroups", "OST_IOSRoomComputationHeight", "OST_IOSAttachedDetailGroups",
    "OST_IOSRegeneratedModelSurfaceStyle", "OST_IOSNotSilhouette", "OST_IOSSlabShapeEditorPointBoundary",
    "OST_IOSAlignmentGraphics", "OST_IOSCrashGraphics", "OST_IOSDragBoxInverted",
    "OST_IOSGhost", "OST_IOSMeasureLine", "OST_IOSPreviewShape", "OST_IOSRailingSketch",
    "OST_IOSRoomCalculationPoint", "OST_IOSSectionBoxNoVisibleGrip", "OST_IOSSuspendedSketch",
    "OST_IOSThinPixel", "OST_IOSTilePatterns", "OST_IOSTransparentFace",
    "OST_ReferenceViewer", "OST_CeilingOpening", "OST_SectionBox",
)


# Measured on this machine, 2026-08-16. A stock course project reported 1044
# elements, of which only ~75 were actual content: 422 Materials, 125 Space
# Type Settings, 117 Legend Components, 69 Material Assets, 53 Sun Path, 50
# HVAC Load Schedules ... and 13 Walls. Document settings and resources are
# not model content, and tracking them buries the signal and costs 10x.
#
# The structural rule is CategoryType: Internal means settings, AnalyticalModel
# is out of scope for architectural work. These raw ids cover resource-like
# categories that report CategoryType.Model despite being resources.
JUNK_BIC_EXTRA = set([
    -2000700,   # Materials
    -2000924,   # Material Assets
    -2009609,   # Sun Path
    -2000576,   # Legend Components
    -2000552,   # Color Fill Schema
    -2008163,   # Pipe Segments
])

# Internal categories that ARE worth tracking despite the blanket rule.
INTERNAL_ALLOW_NAMES = ("OST_Views", "OST_Viewers", "OST_Sheets")

# Categories whose `par` delta cannot be named by KEY_PARAM_NAMES -- every one
# of those is a wall/door/room/sheet parameter -- so these get a literal
# parameter map in the blob instead. See _append_record.
#
# This is deliberately NOT the same set as INTERNAL_ALLOW_NAMES, which answers
# a different question ("which Internal categories are worth tracking at all").
# OST_Cameras is CategoryType.Model, so it reaches the snapshot on its own
# without being allow-listed -- and it was precisely the element that exposed
# the gap: reusing the internal-allow set left Cameras 1045291 reporting
# "other" while the 3D View beside it named its parameter correctly.
PV_CATEGORY_NAMES = ("OST_Views", "OST_Viewers", "OST_Sheets", "OST_Cameras")


def _bic_set(names):
    out = set()
    for nm in names:
        b = getattr(DB.BuiltInCategory, nm, None)
        if b is None:
            continue
        try:
            out.add(int(System.Convert.ToInt32(b)))
        except Exception:
            try:
                out.add(int(b))
            except Exception:
                pass
    return out


def build_junk():
    out = _bic_set(JUNK_NAMES)
    out |= JUNK_BIC_EXTRA
    return out


def build_internal_allow():
    return _bic_set(INTERNAL_ALLOW_NAMES)


def build_pv_cats():
    return _bic_set(PV_CATEGORY_NAMES)


TIER1_NAMES = (
    "OST_Walls", "OST_Doors", "OST_Windows", "OST_Floors", "OST_Roofs",
    "OST_Ceilings", "OST_Columns", "OST_StructuralColumns", "OST_StructuralFraming",
    "OST_StructuralFoundation", "OST_Stairs", "OST_StairsRailing", "OST_Railings",
    "OST_Rooms", "OST_Areas", "OST_Grids", "OST_Levels", "OST_Sheets", "OST_Views",
    "OST_Furniture", "OST_FurnitureSystems", "OST_Casework", "OST_GenericModel",
    "OST_CurtainWallPanels", "OST_CurtainWallMullions", "OST_PlumbingFixtures",
    "OST_LightingFixtures", "OST_MechanicalEquipment", "OST_ElectricalEquipment",
    "OST_SpecialityEquipment", "OST_Parking", "OST_Site", "OST_Topography",
    "OST_Ramps", "OST_CurtainGrids", "OST_Mass",
)


def build_tier1():
    out = set()
    for nm in TIER1_NAMES:
        b = getattr(DB.BuiltInCategory, nm, None)
        if b is None:
            continue
        try:
            out.add(int(System.Convert.ToInt32(b)))
        except Exception:
            try:
                out.add(int(b))
            except Exception:
                pass
    return out


# ---------------------------------------------------------------------------
# signatures
# ---------------------------------------------------------------------------

def geo_sig(el):
    """Location first -- stored doubles, bit-stable across save/reopen.

    BoundingBox is computed geometry and therefore the weaker signal, so it
    is only the fallback. get_BoundingBox(None) is model space, not view
    dependent (the view-dependent form is get_BoundingBox(view)).
    """
    loc = None
    try:
        loc = el.Location
    except Exception:
        loc = None

    if loc is not None:
        try:
            if isinstance(loc, DB.LocationPoint):
                p = loc.Point
                try:
                    r = q(loc.Rotation)
                except Exception:
                    r = 0.0
                return ["P", q(p.X), q(p.Y), q(p.Z), r]
        except Exception:
            pass
        try:
            if isinstance(loc, DB.LocationCurve):
                c = loc.Curve
                a = c.GetEndPoint(0)
                b = c.GetEndPoint(1)
                sig = ["C", type(c).__name__,
                       q(a.X), q(a.Y), q(a.Z), q(b.X), q(b.Y), q(b.Z)]
                try:
                    # midpoint catches arc bulge / spline shape that endpoints miss
                    m = c.Evaluate(0.5, True)
                    sig.extend([q(m.X), q(m.Y), q(m.Z)])
                except Exception:
                    pass
                try:
                    sig.append(q(c.Length))
                except Exception:
                    pass
                return sig
        except Exception:
            pass

    try:
        bb = el.get_BoundingBox(None)
    except Exception:
        bb = None
    if bb is not None:
        try:
            return ["B", q(bb.Min.X), q(bb.Min.Y), q(bb.Min.Z),
                    q(bb.Max.X), q(bb.Max.Y), q(bb.Max.Z)]
        except Exception:
            pass

    return ["N"]


def par_sig(el, volatile):
    """Key by parameter Id (int), never by name -- names are localized.

    Never AsValueString(): it is unit- and locale-formatted, so a units
    change or a Revit language change would rewrite every single hash.
    """
    out = []
    try:
        ps = el.Parameters
    except Exception:
        return out
    for p in ps:
        try:
            if p.IsReadOnly:
                continue
            if not p.HasValue:
                continue
            pid = eid(p.Id)
            if pid in volatile:
                continue
            st = p.StorageType
            if st == DB.StorageType.Double:
                v = q(p.AsDouble())
            elif st == DB.StorageType.Integer:
                v = int(p.AsInteger())
            elif st == DB.StorageType.String:
                v = p.AsString()
            elif st == DB.StorageType.ElementId:
                v = eid(p.AsElementId())
            else:
                continue
            out.append([pid, v])
        except Exception:
            continue
    out.sort(key=lambda kv: kv[0])
    return out


KEY_PARAM_NAMES = (
    "ALL_MODEL_MARK", "ALL_MODEL_INSTANCE_COMMENTS", "DOOR_NUMBER",
    "WALL_USER_HEIGHT_PARAM", "WALL_BASE_OFFSET", "WALL_TOP_OFFSET",
    "INSTANCE_SILL_HEIGHT_PARAM", "INSTANCE_ELEVATION_PARAM",
    "FAMILY_LEVEL_PARAM", "SCHEDULE_LEVEL_PARAM", "ROOM_NAME", "ROOM_NUMBER",
    "LEVEL_ELEV", "DATUM_TEXT", "SHEET_NUMBER", "SHEET_NAME",
)


def build_key_params():
    out = {}
    for nm in KEY_PARAM_NAMES:
        b = getattr(DB.BuiltInParameter, nm, None)
        if b is None:
            continue
        try:
            out[int(System.Convert.ToInt32(b))] = nm
        except Exception:
            pass
    return out


def key_params(el, keymap):
    """Literal values for a small curated set, so a `par` delta can be named."""
    out = {}
    for pid, nm in keymap.items():
        try:
            bip = getattr(DB.BuiltInParameter, nm, None)
            if bip is None:
                continue
            p = el.get_Parameter(bip)
            if p is None or not p.HasValue:
                continue
            st = p.StorageType
            if st == DB.StorageType.Double:
                out[nm] = q(p.AsDouble())
            elif st == DB.StorageType.Integer:
                out[nm] = int(p.AsInteger())
            elif st == DB.StorageType.String:
                out[nm] = p.AsString()
            elif st == DB.StorageType.ElementId:
                out[nm] = eid(p.AsElementId())
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# TSV record
#
# uid \t cat \t typ \t lvl \t geo \t par \t id \t blob      (8 fields, 7 tabs)
#
# Fields 1..7 cannot contain a tab or a newline by construction: uid is
# GUID+hex, cat/typ/lvl/id are integers, geo/par are hex digests.
# Field 8 is json.dumps(..., ensure_ascii=True), which converts a newline to
# a two-character \n and a tab to a two-character \t. So the record is
# single-line and pure ASCII because of how it is encoded, not because of
# escaping logic that could be got wrong on one side.
# ---------------------------------------------------------------------------

def tsv_line(uid, cat, typ, lvl, geo, par, elid, blob):
    ln = u"\t".join([uid, str(cat), str(typ), str(lvl), geo, par, str(elid), blob])
    # Loud sanity check. If the reasoning above is wrong anywhere, fail here
    # rather than silently writing a corrupt snapshot.
    if ln.count(u"\t") != 7 or u"\n" in ln or u"\r" in ln:
        raise TrackerAbort("TSV_SANITY_FAIL uid=%s" % uid)
    return ln


def load_tsv(path):
    """uid -> rest-of-line. No JSON parsing, no object graph."""
    lines = read_lines(path)
    if lines is None:
        return None
    d = {}
    for ln in lines:
        if not ln:
            continue
        i = ln.find(u"\t")
        if i < 0:
            continue
        d[ln[:i]] = ln[i + 1:]
    return d


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def get_ident():
    if doc is None:
        raise TrackerAbort("NO_ACTIVE_DOCUMENT")

    is_family = False
    try:
        is_family = bool(doc.IsFamilyDocument)
    except Exception:
        pass

    path = u""
    try:
        path = doc.PathName or u""
    except Exception:
        path = u""

    workshared = False
    try:
        workshared = bool(doc.IsWorkshared)
    except Exception:
        pass

    central = None
    if workshared:
        try:
            mp = doc.GetWorksharingCentralModelPath()
            if mp is not None:
                central = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(mp)
        except Exception:
            central = None

    vguid = None
    saves = -1
    try:
        dv = DB.Document.GetDocumentVersion(doc)
        vguid = str(dv.VersionGUID)
        saves = int(dv.NumberOfSaves)
    except Exception:
        pass

    lineage = None
    try:
        lineage = doc.ProjectInformation.UniqueId
    except Exception:
        pass

    title = u""
    try:
        title = doc.Title or u""
    except Exception:
        pass

    modified = False
    try:
        modified = bool(doc.IsModified)
    except Exception:
        pass

    username = u""
    try:
        username = doc.Application.Username or u""
    except Exception:
        pass

    return {
        "lineage": lineage,
        "path": path,
        "canon": canon_path(central or path),
        "central": central,
        "workshared": workshared,
        "saves": saves,
        "vguid": vguid,
        "title": title,
        "modified": modified,
        "is_family": is_family,
        "username": username,
    }


BACKUP_RE = re.compile(r"\.\d{4}\.rvt$", re.IGNORECASE)


def resolve_instance(base, ident):
    """Map the open document to a tracked instance folder.

    The primary key is the CANONICAL PATH, not the lineage id.

    Measured: three unrelated projects created from the same Revit template
    all report the identical ProjectInformation.UniqueId, because the
    ProjectInformation element is inherited from the template. So in practice
    lineage is often a TEMPLATE id, and keying folders on it would collapse
    every project made from one template into a single tracking record.

    Lineage is therefore demoted to a hint: it is recorded, and used only to
    recognise a rename (same lineage, old path gone) and to note possible
    relatives. It never decides the folder.
    """
    notes = []

    if ident["is_family"]:
        raise TrackerAbort("FAMILY_DOCUMENT_NOT_TRACKED")
    if not ident["path"]:
        raise TrackerAbort("UNSAVED_DOCUMENT")

    ensure_dir(base)

    idx_path = os.path.join(base, "_index.json")
    idx = load_json(idx_path, None) or {"v": 1, "instances": {}}
    insts = idx.setdefault("instances", {})

    canon = ident["canon"]
    lineage = ident["lineage"]

    inst_id = None
    for iid, rec in insts.items():
        if rec.get("canon") == canon:
            inst_id = iid
            break

    if inst_id is None:
        # No path match. A rename looks like: same lineage, and the recorded
        # path is gone from disk. Anything else is simply a new project.
        cands = []
        for iid, rec in insts.items():
            if lineage and rec.get("lineage") != lineage:
                continue
            p = rec.get("path") or u""
            if p and not file_exists(p):
                cands.append(iid)

        if len(cands) == 1:
            inst_id = cands[0]
            insts[inst_id]["canon"] = canon
            insts[inst_id]["path"] = ident["path"]
            notes.append("PATH_CHANGED")
        else:
            if len(cands) > 1:
                notes.append("RENAME_AMBIGUOUS")
            inst_id = str(System.Guid.NewGuid())
            folder = u"%s [%s]" % (safe_folder_name(ident["title"]), inst_id[:8])
            cand = folder
            n = 2
            while System.IO.Directory.Exists(os.path.join(base, cand)):
                cand = u"%s (%d)" % (folder, n)
                n += 1
            sibs = [i for i, r in insts.items() if lineage and r.get("lineage") == lineage]
            insts[inst_id] = {
                "folder": cand,
                "canon": canon,
                "path": ident["path"],
                "central": ident["central"],
                "lineage": lineage,
                "created": now_iso(),
                "lineage_siblings": sibs,
            }
            notes.append("NEW_INSTANCE")
            if sibs:
                # Same lineage but a different file. Usually just a shared
                # template, so do NOT inherit their baseline -- start clean.
                notes.append("SHARES_LINEAGE_WITH_%d" % len(sibs))

    rec = insts[inst_id]

    # Backup restore: never overwrite the mainline baseline silently.
    prev_saves = rec.get("last_saves")
    if BACKUP_RE.search(ident["path"] or u""):
        notes.append("ROLLBACK_SUSPECTED_BACKUP_NAME")
    if prev_saves is not None and ident["saves"] >= 0 and ident["saves"] < prev_saves:
        notes.append("ROLLBACK_SUSPECTED_SAVE_COUNT")

    rec["last_seen"] = now_iso()
    rec["title"] = ident["title"]
    rec["lineage"] = lineage
    save_json(idx_path, idx)

    idir = ensure_dir(os.path.join(base, rec["folder"]))
    ensure_dir(os.path.join(idir, "deltas"))

    return {
        "instance_dir": idir,
        "instance_id": inst_id,
        "index_path": idx_path,
        "notes": notes,
        "folder": rec["folder"],
    }


# ---------------------------------------------------------------------------
# collection + snapshot
# ---------------------------------------------------------------------------

def snap_ctx(scope):
    """Resolve every filter/lookup table exactly once per run.

    build_volatile/build_junk/build_tier1/build_key_params/build_pv_cats/
    build_internal_allow each do a handful of getattr + Convert.ToInt32 calls
    to resolve BuiltInCategory/BuiltInParameter names to ints. Previously
    build_snapshot_chunked rebuilt all of them on every call, including every
    resumed chunk of a large model; snap_ctx pays that cost once and the
    caller threads the result through instead.
    """
    return {
        "scope": scope,
        "junk": build_junk(),
        "allow": build_internal_allow(),
        "volatile": build_volatile(),
        "tier1": build_tier1(),
        "pv": build_pv_cats(),
        "keymap": build_key_params(),
    }


def passes_filter(el, ctx):
    """The membership predicate: does this element belong in the snapshot?

    -> (True, cid) if it does, (False, None) if not. This is the ONLY place
    that decides membership. collect_elements (the full sweep) calls it, and
    from the incremental checkpoint on, the cheap per-run membership sweep
    and the dirty-set resolution call it too -- so there is exactly one copy
    of the rule to keep in sync. Two independent copies drifting apart is
    exactly how a phantom add or delete gets invented (see the
    SNAPSHOT_FORMAT comment above: widening the element filter once reported
    966 deletions that never happened).
    """
    try:
        c = el.Category
        if c is None:
            return False, None
        cid = eid(c.Id)
        if cid in ctx["junk"]:
            return False, None
        scope = ctx.get("scope", "model")
        if scope != "all":
            try:
                ct = str(c.CategoryType)
            except Exception:
                ct = "Model"
            if ct == "AnalyticalModel":
                return False, None
            if ct == "Internal" and cid not in (ctx.get("allow") or set()):
                return False, None
        return True, cid
    except Exception:
        return False, None


def collect_elements(ctx):
    """Model and annotation content only. See passes_filter for the rule.

    ctx["scope"]="all" keeps everything with a category, for diagnostics.
    ctx["scope"]="model" (default) drops document settings and resources:
      - CategoryType.Internal, except an explicit allow list (views, sheets)
      - CategoryType.AnalyticalModel
      - resource categories that claim CategoryType.Model (see JUNK_BIC_EXTRA)
    """
    col = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
    out = []
    for el in col:
        ok, cid = passes_filter(el, ctx)
        if ok:
            out.append((el, cid))
    return out


def fingerprint(pairs_or_ids):
    """T1: order-independent, per-category. Cheap tripwire for a torn read."""
    h = hashlib.md5()
    for x in sorted(pairs_or_ids):
        h.update(str(x))
        h.update(b",")
    return h.hexdigest()[:12]


def sweep_ids(ctx):
    """Cheap membership pass: touches Category only, never Location or
    Parameters. The exact set of ElementIds that SHOULD be snapshot rows
    under ctx's filter, straight from Revit.

    This is what an incremental checkpoint trusts for MEMBERSHIP instead of
    the DocumentChanged buffer. The listener says what it saw change; this
    proves what actually exists, every run, at a fraction of make_record's
    cost -- so a missed creation or deletion (the exact scar behind the
    SNAPSHOT_FORMAT comment: widening the filter once reported 966
    deletions that never happened) is caught even if the listener missed it.
    """
    col = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
    out = set()
    for el in col:
        ok, cid = passes_filter(el, ctx)
        if not ok:
            continue
        try:
            out.add(eid(el.Id))
        except Exception:
            pass
    return out


def filter_fp(ctx):
    """Fingerprint over every resolved filter/lookup id in ctx.

    Catches a Revit-version upgrade resolving a BuiltInCategory or
    BuiltInParameter name to a different int without anyone bumping
    SNAPSHOT_FORMAT -- membership or comparison rules would silently change
    underneath an incremental run (or a full one) with nothing to notice.
    Costs nothing extra: snap_ctx already built every set/dict this reads.
    """
    parts = []
    for key in ("junk", "allow", "volatile", "tier1", "pv", "keymap"):
        v = ctx.get(key)
        if isinstance(v, dict):
            parts.extend(str(k) for k in v.keys())
        elif v:
            parts.extend(str(x) for x in v)
    h = hashlib.md5()
    for x in sorted(parts):
        h.update(x)
        h.update(b",")
    return h.hexdigest()[:12]


def content_fp(lines):
    """Fingerprint over the snapshot's own content (sorted TSV lines).

    Distinct from fingerprint()/sweep's fp, which is derived from Revit's
    current element set -- this one is derived from the snapshot FILE, so it
    can catch a baseline mismatch that membership alone would miss: the
    .bak fallback in op_snapshot pairing a snapshot.tsv with a DIFFERENT
    run's header, or a hand-edited file.
    """
    h = hashlib.md5()
    for ln in sorted(lines):
        h.update(ln.encode("utf-8") if isinstance(ln, unicode) else ln)
        h.update(b"\n")
    return h.hexdigest()[:12]


def build_snapshot_chunked(budget_ms, scope, run, ctx):
    """Resumable snapshot. ctx comes from the caller (snap_ctx(scope), built
    once) so the full path and the incremental path share exactly one
    resolved filter/lookup table instead of each keeping a copy that could
    drift out of sync.

    Measured on this machine (last_run.json, 2026-08-30): 870 elements in
    6044ms, about 144 elements/second -- not the ~800/s once assumed here.
    An 18-second budget covers roughly 2,600 elements, not 14,000; most of
    the cost is par_sig reading every parameter of every element. Without
    resume, any model larger than that would exhaust the budget and never
    produce a baseline at all. Work accumulates in the state module between
    MCP calls; disk is only touched when the pass completes.

    Every chunk re-checks that the element set has not moved. If a human edits
    between chunks, the result would be a torn read stitched from two different
    model states, which is worse than no snapshot -- so the run is abandoned.
    """
    st = get_state()

    pairs = collect_elements(ctx)
    total = len(pairs)
    fp = fingerprint([eid(el.Id) for el, _ in pairs])

    resuming = (getattr(st, "snap_run", None) is not None
                and st.snap_scope == scope
                and st.snap_fp == fp)

    if resuming:
        lines = st.snap_lines
        cat_names, typ_names, lvl_names = st.snap_tables
        cursor = st.snap_cursor
        chunk_no = st.snap_chunk + 1
    else:
        if getattr(st, "snap_run", None) is not None and st.snap_fp != fp:
            st.snap_abandoned = st.snap_run
        lines = []
        cat_names, typ_names, lvl_names = {}, {}, {}
        cursor = 0
        chunk_no = 0

    truncated = False
    i = cursor
    n = 0
    while i < total:
        # Forward progress is guaranteed before the budget gets a vote.
        # Collecting the element set alone can outlast a small budget, and a
        # chunk that processes zero elements would resume at the same cursor
        # forever. Measured: a 120ms budget produced done=0 on a 76-element
        # model because collection had already spent it.
        if n >= MIN_PER_CHUNK and (n & 0x3F) == 0 and elapsed_ms() > budget_ms:
            truncated = True
            break
        el, cid = pairs[i]
        i += 1
        n += 1
        try:
            _append_record(el, cid, lines, ctx, cat_names, typ_names, lvl_names)
        except TrackerAbort:
            raise
        except Exception:
            continue

    if truncated:
        st.snap_run = run
        st.snap_scope = scope
        st.snap_fp = fp
        st.snap_lines = lines
        st.snap_tables = (cat_names, typ_names, lvl_names)
        st.snap_cursor = i
        st.snap_chunk = chunk_no
    else:
        st.snap_run = None
        st.snap_lines = None
        st.snap_tables = None

    lines_out = sorted(lines)
    header = {
        "v": SNAPSHOT_FORMAT,
        "taken": now_iso(),
        "tables": {"cat": cat_names, "typ": typ_names, "lvl": lvl_names},
        "integrity": {"n": len(lines_out), "complete": not truncated,
                      "total_seen": total, "fp": fp, "chunks": chunk_no + 1},
    }
    progress = {"done": i, "total": total, "chunk": chunk_no, "resumed": resuming}
    return header, lines_out, truncated, total, progress


def make_record(el, cid, ctx, cat_names, typ_names, lvl_names):
    """Build one TSV record line and collect its display names into the side
    tables. This is the ONLY place an element becomes a snapshot row -- the
    full sweep (via _append_record below) and, from the incremental
    checkpoint on, the dirty-set rehash both call this, so a record produced
    either way is byte-identical by construction."""
    uid = el.UniqueId
    elid = eid(el.Id)

    tid = -1
    try:
        tid = eid(el.GetTypeId())
    except Exception:
        tid = -1

    lid = -1
    try:
        lid = eid(el.LevelId)
    except Exception:
        lid = -1

    g = geo_sig(el)
    geo = h12(g)
    ps = par_sig(el, ctx["volatile"])
    par = h12(ps)

    rec = None
    if cid in ctx["tier1"]:
        rec = {"g": g, "kp": key_params(el, ctx["keymap"])}
    if cid in ctx["pv"]:
        # Views, Sheets and Cameras match none of KEY_PARAM_NAMES -- every one
        # of those is a wall/door/room/sheet parameter -- so a par delta on
        # them can only ever be described as "other", which is unactionable.
        # Measured 2026-08-16: elements 1045291 (Cameras) and 1045293 (Views)
        # came back modified with params_note "other" on two separate session
        # opens, and there was no way to learn WHICH parameter moved.
        #
        # Storing the literal parameter map for these few elements is free:
        # par_sig above already built this exact list and threw it away, and
        # the blob is field 8, which diff_snapshots deliberately excludes from
        # change detection (see its range(5) and the comment there). So this
        # names future parameter deltas without inventing diffs, without extra
        # Revit API calls, and without invalidating an existing baseline --
        # which is why SNAPSHOT_FORMAT is deliberately NOT bumped for it.
        if rec is None:
            rec = {}
        rec["pv"] = ps
    blob = jdump(rec) if rec is not None else u"{}"

    line = tsv_line(uid, cid, tid, lid, geo, par, elid, blob)

    if str(cid) not in cat_names:
        try:
            cat_names[str(cid)] = el.Category.Name
        except Exception:
            pass
    if tid > 0 and str(tid) not in typ_names:
        try:
            te = doc.GetElement(el.GetTypeId())
            if te is not None:
                nm = None
                try:
                    p = te.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
                    if p is not None:
                        nm = p.AsString()
                except Exception:
                    nm = None
                if not nm:
                    nm = getattr(te, "Name", None)
                if nm:
                    typ_names[str(tid)] = nm
        except Exception:
            pass
    if lid > 0 and str(lid) not in lvl_names:
        try:
            le = doc.GetElement(el.LevelId)
            if le is not None:
                nm = getattr(le, "Name", None)
                if nm:
                    lvl_names[str(lid)] = nm
        except Exception:
            pass

    return line


def _append_record(el, cid, lines, ctx, cat_names, typ_names, lvl_names):
    lines.append(make_record(el, cid, ctx, cat_names, typ_names, lvl_names))

# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def diff_snapshots(prev, cur):
    """Lazy parsing: the whole-line string compare is the fast path.

    Only records that actually differ are ever split into fields, so cost is
    proportional to the size of the change, not to the size of the model.
    """
    added = []
    deleted = []
    modified = []

    pk = set(prev.keys())
    ck = set(cur.keys())

    for uid in ck - pk:
        added.append(uid)
    for uid in pk - ck:
        deleted.append(uid)

    for uid in pk & ck:
        a = prev[uid]
        b = cur[uid]
        if a == b:
            continue
        pa = a.split(u"\t")
        pb = b.split(u"\t")
        if len(pa) < 5 or len(pb) < 5:
            continue
        fields = [FIELDS[i] for i in range(5) if pa[i] != pb[i]]
        if not fields:
            continue  # only the blob or the element id moved -- not a real change
        modified.append({"uid": uid, "fields": fields, "before": pa, "after": pb})

    return {"added": added, "deleted": deleted, "modified": modified}


def _bip_name(pid):
    """Resolve a BuiltInParameter id to its enum name; fall back to the id."""
    try:
        if pid < 0:
            nm = System.Enum.GetName(
                DB.BuiltInParameter,
                System.Enum.ToObject(DB.BuiltInParameter, pid))
            if nm:
                return nm
    except Exception:
        pass
    return str(pid)


def _pv_delta(blob_before, blob_after, limit=12):
    """Name the parameters that moved, for elements carrying a `pv` map.

    Views, Sheets and Cameras match no curated key parameter, so without this
    every `par` delta on them reads "other" and cannot be acted on.

    Returns [] when either side predates the `pv` field. That is the expected
    state for one diff after this was introduced -- the stored baseline was
    written by the older code -- and it is not an error: an unnameable change
    correctly falls back to "other" rather than guessing.
    """
    try:
        pb = json.loads(blob_before).get("pv")
        pa = json.loads(blob_after).get("pv")
    except Exception:
        return []
    if not pb or not pa:
        return []
    try:
        db = dict((int(k), v) for k, v in pb)
        da = dict((int(k), v) for k, v in pa)
    except Exception:
        return []
    out = []
    for pid in sorted(set(db.keys()) | set(da.keys())):
        if db.get(pid) != da.get(pid):
            out.append([_bip_name(pid), db.get(pid), da.get(pid)])
            if len(out) >= limit:
                break
    return out


def describe(diff, prev, cur, prev_hdr, cur_hdr, limit=50):
    """Turn raw diff into named, human-explainable items (capped)."""
    pt = (prev_hdr or {}).get("tables", {})
    ct = (cur_hdr or {}).get("tables", {})

    def nm(tables, kind, key):
        try:
            return tables.get(kind, {}).get(str(key))
        except Exception:
            return None

    items = []

    for uid in diff["added"][:limit]:
        parts = cur[uid].split(u"\t")
        items.append({
            "kind": "added", "uid": uid, "id": parts[5] if len(parts) > 5 else None,
            "cat": nm(ct, "cat", parts[0]), "typ": nm(ct, "typ", parts[1]),
            "lvl": nm(ct, "lvl", parts[2]),
        })

    for uid in diff["deleted"][:limit]:
        parts = prev[uid].split(u"\t")
        items.append({
            "kind": "deleted", "uid": uid, "id": parts[5] if len(parts) > 5 else None,
            "cat": nm(pt, "cat", parts[0]), "typ": nm(pt, "typ", parts[1]),
            "lvl": nm(pt, "lvl", parts[2]),
        })

    for m in diff["modified"][:limit]:
        pa = m["before"]
        pb = m["after"]
        it = {
            "kind": "modified", "uid": m["uid"], "fields": m["fields"],
            "id": pb[5] if len(pb) > 5 else None,
            "cat": nm(ct, "cat", pb[0]),
            "typ": nm(ct, "typ", pb[1]),
            "lvl": nm(ct, "lvl", pb[2]),
        }
        if "typ" in m["fields"]:
            it["typ_from"] = nm(pt, "typ", pa[1])
            it["typ_to"] = nm(ct, "typ", pb[1])
        if "lvl" in m["fields"]:
            it["lvl_from"] = nm(pt, "lvl", pa[2])
            it["lvl_to"] = nm(ct, "lvl", pb[2])
        if "geo" in m["fields"]:
            try:
                gb = json.loads(pa[6]).get("g")
                ga = json.loads(pb[6]).get("g")
                it["geo_from"] = gb
                it["geo_to"] = ga
                it["moved_ft"] = geo_distance(gb, ga)
            except Exception:
                pass
        if "par" in m["fields"]:
            try:
                kb = json.loads(pa[6]).get("kp") or {}
                ka = json.loads(pb[6]).get("kp") or {}
                ch = {}
                for k in set(kb.keys()) | set(ka.keys()):
                    if kb.get(k) != ka.get(k):
                        ch[k] = [kb.get(k), ka.get(k)]
                if ch:
                    it["params"] = ch
                else:
                    pvc = _pv_delta(pa[6], pb[6])
                    if pvc:
                        it["params_changed"] = pvc
                    else:
                        it["params_note"] = "other"
            except Exception:
                pass
        items.append(it)

    return items


def geo_distance(a, b):
    try:
        if not a or not b or a[0] != b[0]:
            return None
        if a[0] == "P":
            dx = b[1] - a[1]
            dy = b[2] - a[2]
            dz = b[3] - a[3]
        elif a[0] in ("C", "B"):
            dx = b[2] - a[2]
            dy = b[3] - a[3]
            dz = b[4] - a[4]
        else:
            return None
        return round((dx * dx + dy * dy + dz * dz) ** 0.5, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# attribution -- DocumentChanged listener
#
# The diff tells us WHAT changed. Only DocumentChanged tells us under which
# transaction, and GetTransactionNames() is the strongest evidence available:
# our own work always runs under a name containing "MCP", while a human's
# shows up as "Move", "Delete", or whatever Revit called their command.
#
# Three states, and `unknown` is the default. Changes also come from Revit
# itself -- wall joins, regeneration, link reloads -- so "not ours, therefore
# a human did it" would be a lie dressed as a fact.
# ---------------------------------------------------------------------------

STATE_MODULE = "revit_tracker_state"

# Cap ids captured per event. A sync-with-central can touch tens of thousands;
# the handler runs on the UI thread and must never become the slow part.
MAX_IDS_PER_EVENT = 5000
EVENT_BUFFER_MAX = 20000
EVENT_BUFFER_TRIM = 10000

# Minimum elements a chunk must process before the time budget may stop it.
# Without this, a budget smaller than the collection phase yields zero progress
# per chunk and the resume loop never terminates.
MIN_PER_CHUNK = 64


def get_state():
    """The state module is NEVER replaced, only mutated.

    That is what makes orphan detection possible: `gen` has to survive a
    reload of tracker.py, or a handler we lost the reference to has nothing
    to compare itself against and can never retire. `boot_id` works the same
    way for the whole module: it identifies one continuous lifetime of this
    Python engine inside Revit, untouched by tracker.py being reloaded --
    only a fresh Revit process gets a new one.
    """
    st = sys.modules.get(STATE_MODULE)
    if st is None:
        import imp
        st = imp.new_module(STATE_MODULE)
        st.gen = 0
        st.events = []
        st.handler = None
        st.hooked = False
        st.doc_path = None
        st.sync_until = None
        st.dropped = 0
        st.boot_id = str(System.Guid.NewGuid())
        st.doc_epoch = 0
        st.doc_handlers = None
        sys.modules[STATE_MODULE] = st
    for attr, default in (("gen", 0), ("events", None), ("handler", None),
                          ("hooked", False), ("doc_path", None),
                          ("sync_until", None), ("dropped", 0),
                          ("boot_id", ""), ("doc_epoch", 0),
                          ("doc_handlers", None)):
        if not hasattr(st, attr):
            setattr(st, attr, [] if default is None else default)
    if not st.boot_id:
        st.boot_id = str(System.Guid.NewGuid())
    return st


# Transaction names the MCP server opens that do NOT carry the "MCP" marker.
#
# Derived 2026-08-16 by reading the installed extension source
# (pyRevit/Extensions/mcp-server-for-revit-python.extension/revit_mcp/*.py),
# not guessed: 23 of its 28 transactions are named "<action> via MCP" or
# "MCP Code Execution: <description>", and exactly these five are not.
#
# Without them the "MCP" substring test fails and work Claude did through the
# friendly create_* tools is reported as a human edit. Measured on this machine:
# deltas/d-20260816130236.json counted 4 Claude-created elements as `human`
# under the transaction name "Create Levels".
#
# Exact-match, never substring: "Create Levels" is close enough to something a
# future Revit UI command could be called that a loose test would start
# swallowing real human edits. Revit's own level command names its transaction
# "Level", which does not match. If the extension is updated, re-derive this
# list rather than adding names by guesswork -- a wrong entry here silently
# relabels human work as ours, which is the one error this system must not make.
MCP_TX_NAMES = frozenset([
    u"Clear Element Colors",
    u"Color Elements by Parameter",
    u"Create Levels",
    u"Create Line-Based Elements",
    u"Create Surface-Based Elements",
])


def _tx_is_ours(names):
    for n in names:
        if not n:
            continue
        if "MCP" in n:
            return True
        if n in MCP_TX_NAMES:
            return True
    return False


def _ids(coll):
    """-> (ids, hit_cap). hit_cap means the collection may hold more than we
    captured -- a sync-with-central can touch tens of thousands of elements
    and this runs on the UI thread, so the cap is real, but a truncated event
    is an INCOMPLETE record and callers that trust event completeness (the
    incremental checkpoint) must be able to tell the difference."""
    out = []
    n = 0
    hit_cap = False
    try:
        for i in coll:
            if n >= MAX_IDS_PER_EVENT:
                hit_cap = True
                break
            out.append(eid(i))
            n += 1
    except Exception:
        pass
    return out, hit_cap


def _make_handler(gen):
    def _on_changed(sender, args):
        try:
            st = sys.modules.get(STATE_MODULE)
            if st is None or getattr(st, "gen", None) != gen:
                # We are an orphan from a previous incarnation and nobody
                # holds a reference to us. Retire ourselves.
                try:
                    args.GetDocument().Application.DocumentChanged -= _on_changed
                except Exception:
                    pass
                return

            d = args.GetDocument()
            if st.doc_path and getattr(d, "PathName", None) != st.doc_path:
                return

            names = []
            try:
                names = [n for n in args.GetTransactionNames()]
            except Exception:
                names = []

            a, a_tr = _ids(args.GetAddedElementIds())
            m, m_tr = _ids(args.GetModifiedElementIds())
            dl, d_tr = _ids(args.GetDeletedElementIds())
            if not (a or m or dl):
                return

            syncing = False
            try:
                su = st.sync_until
                syncing = su is not None and System.DateTime.UtcNow < su
            except Exception:
                syncing = False

            # Informational only -- an undo/redo is deliberately NOT an
            # incremental fallback trigger (Revit restores the same
            # UniqueId/ElementId, so the rehashed row reproduces the
            # original exactly and the diff is correctly empty). Recorded
            # so a give-up reason, if one happens for an unrelated cause,
            # can note SAW_UNDO rather than looking unexplained.
            opname = None
            try:
                opname = str(getattr(args, "Operation", None))
            except Exception:
                opname = None

            # `ours` is decided HERE, at event time, by the _tx_is_ours that
            # was in scope when this closure was built. execfile() builds a
            # fresh namespace per run, so a handler armed before an edit to
            # this file keeps running the OLD rule until the next snapshot
            # re-arms it. Measured 2026-08-16: right after MCP_TX_NAMES was
            # added, the first create_level still came back `human`; the next
            # one, after a re-arming checkpoint, came back `claude`.
            # After editing this file, take one snapshot before concluding a
            # fix did not work.
            st.events.append({
                "t": System.DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss"),
                "tx": names,
                "ours": _tx_is_ours(names),
                "sync": syncing,
                "a": a, "m": m, "d": dl,
                # Any list above being cut off at MAX_IDS_PER_EVENT means this
                # event is an INCOMPLETE record of what actually changed, not
                # just a large one. A consumer that trusts event completeness
                # (the incremental checkpoint) must fall back rather than diff
                # against a partial picture.
                "tr": 1 if (a_tr or m_tr or d_tr) else 0,
                # Shape version. An incremental run compares this against
                # EVENT_SCHEMA and falls back if this closure predates a
                # change to the dict shape it is writing.
                "ev": EVENT_SCHEMA,
                "op": opname,
            })
            if len(st.events) > EVENT_BUFFER_MAX:
                del st.events[:EVENT_BUFFER_TRIM]
                st.dropped += EVENT_BUFFER_TRIM
        except Exception:
            # A handler that throws on every transaction can destabilise Revit.
            # Remove ourselves rather than keep failing.
            try:
                args.GetDocument().Application.DocumentChanged -= _on_changed
            except Exception:
                pass
    return _on_changed


def arm_handler(ident):
    """Idempotent re-arm. Safe to call on every single tracker invocation."""
    st = get_state()
    st.doc_path = ident.get("path") or None

    app = None
    try:
        app = doc.Application
    except Exception:
        return {"armed": False, "reason": "no_application"}

    # Always detach first. Harmless if we were never attached, and it is the
    # only way to stop a handler we DO still hold from doubling up.
    if st.handler is not None:
        try:
            app.DocumentChanged -= st.handler
        except Exception:
            pass

    st.gen = int(st.gen) + 1
    h = _make_handler(st.gen)
    try:
        app.DocumentChanged += h
    except Exception, e:
        st.handler = None
        st.hooked = False
        return {"armed": False, "reason": str(e)}

    st.handler = h          # strong reference, or the delegate is collected
    st.hooked = True
    return {"armed": True, "gen": st.gen, "buffered": len(st.events)}


SYNC_WINDOW_MIN = 5


def arm_sync_watch():
    """Tag changes that arrive during a sync-with-central or reload-latest.

    Stored as an EXPIRY STAMP, not a boolean. There is no "sync failed" event
    in the API, so a flag raised on start and lowered on finish stays stuck
    forever if the sync dies mid-way -- every later edit by the user would then
    be mislabelled as someone else's incoming work. An expiry cannot leak:
    reading it is `now < sync_until`, and a stuck window simply lapses.
    """
    st = get_state()
    try:
        app = doc.Application
    except Exception:
        return {"watching": False}

    def _open(sender, args):
        try:
            st.sync_until = System.DateTime.UtcNow.AddMinutes(SYNC_WINDOW_MIN)
            st.sync_opened = System.DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss")
            st.sync_closed_cleanly = False
        except Exception:
            pass

    def _close(sender, args):
        try:
            st.sync_until = None
            st.sync_closed_cleanly = True
        except Exception:
            pass

    if getattr(st, "sync_handlers", None):
        for evname, h in st.sync_handlers:
            try:
                ev = getattr(app, evname, None)
                if ev is not None:
                    ev -= h
            except Exception:
                pass

    pairs = []
    for evname, fn in (("DocumentSynchronizingWithCentral", _open),
                       ("DocumentSynchronizedWithCentral", _close),
                       ("DocumentReloadingLatest", _open),
                       ("DocumentReloadedLatest", _close)):
        try:
            ev = getattr(app, evname, None)
            if ev is None:
                continue
            ev += fn
            pairs.append((evname, fn))
        except Exception:
            continue
    st.sync_handlers = pairs
    return {"watching": bool(pairs), "events": [p[0] for p in pairs]}


def arm_doc_watch():
    """Bump doc_epoch on close/reopen.

    Nothing else here detects this case: st.doc_path and GetDocumentVersion
    both stay exactly as they were, because closing and reopening the same
    file reproduces the same values. doc_epoch is incremented unconditionally
    by the CLR event itself, so it is the one thing that survives a close
    that leaves everything else looking untouched.

    Idempotent re-arm, same detach-then-attach pattern as arm_sync_watch.
    """
    st = get_state()
    try:
        app = doc.Application
    except Exception:
        return {"watching": False}

    def _bump(sender, args):
        try:
            st.doc_epoch = int(st.doc_epoch) + 1
        except Exception:
            pass

    if getattr(st, "doc_handlers", None):
        for evname, h in st.doc_handlers:
            try:
                ev = getattr(app, evname, None)
                if ev is not None:
                    ev -= h
            except Exception:
                pass

    pairs = []
    for evname in ("DocumentOpened", "DocumentClosing"):
        try:
            ev = getattr(app, evname, None)
            if ev is None:
                continue
            ev += _bump
            pairs.append((evname, _bump))
        except Exception:
            continue
    st.doc_handlers = pairs
    return {"watching": bool(pairs), "events": [p[0] for p in pairs]}


def check_sync_window():
    """Close a lapsed window and say whether its tagging is trustworthy."""
    st = get_state()
    su = getattr(st, "sync_until", None)
    if su is None:
        return None
    try:
        if System.DateTime.UtcNow >= su:
            st.sync_until = None
            if not getattr(st, "sync_closed_cleanly", True):
                # No matching completion event ever arrived. Downgrade rather
                # than keep a confident label that may well be wrong.
                return "SYNC_WINDOW_EXPIRED"
    except Exception:
        st.sync_until = None
    return None


def worksharing_owners(ids):
    """Real usernames, when the model is workshared. Best evidence there is."""
    out = {}
    try:
        if not doc.IsWorkshared:
            return out
    except Exception:
        return out
    for i in ids[:200]:
        try:
            info = DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, make_eid(i))
            out[str(i)] = {"creator": info.Creator, "owner": info.Owner,
                           "last_changed_by": info.LastChangedBy}
        except Exception:
            continue
    return out


def make_eid(i):
    try:
        return DB.ElementId(System.Int64(int(i)))
    except Exception:
        return DB.ElementId(int(i))


def drain_events(idir, run):
    """Move buffered events to disk and return them for attribution.

    Only the events present when we started are consumed, so anything that
    arrives while we snapshot survives for the next run.
    """
    st = get_state()
    n = len(st.events)
    if n == 0:
        return []
    taken = st.events[:n]
    del st.events[:n]

    lines = []
    for e in taken:
        rec = dict(e)
        rec["kind"] = "tx"
        rec["run"] = run
        lines.append(jdump(rec))
    try:
        append_lines(os.path.join(idir, "events.ndjson"), lines)
    except Exception:
        pass
    return taken


def build_id_index(snap):
    """ElementId (str) -> UniqueId, read out of field index 5 of each row.

    Shared by attribute() and (from phase 2 on) the incremental dirty-set
    computation, which needs the same bridge to resolve a DocumentChanged
    id back to the uid it names -- hoisted here so both build it once
    instead of each keeping a private copy that could drift out of sync.
    """
    out = {}
    if not snap:
        return out
    for uid, rest in snap.items():
        p = rest.split(u"\t")
        if len(p) > 5:
            out[p[5]] = uid
    return out


def attribute(diff, cur_ids, prev_ids, events):
    """Map each changed element to claude / human / unknown.

    ElementIds are ints and the snapshot is keyed by UniqueId, so the bridge
    runs through the id stored in each record: the current snapshot resolves
    added and modified elements, and the previous one is the only place a
    deleted element can still be named. cur_ids/prev_ids come from
    build_id_index() -- built once by the caller, not per call here.
    """
    verdict = {}     # uid -> {"by":..., "tx":[...]}
    for e in events:
        by = "claude" if e.get("ours") else ("sync_incoming" if e.get("sync") else "human")
        for key, src in (("a", cur_ids), ("m", cur_ids), ("d", prev_ids)):
            for i in e.get(key) or []:
                uid = src.get(str(i))
                if uid is None:
                    continue
                # A later event wins: "Claude made it, then a human moved it"
                # should end up attributed to the human's move.
                verdict[uid] = {"by": by, "tx": e.get("tx") or []}

    counts = {"claude": 0, "human": 0, "sync_incoming": 0, "unknown": 0}
    out = {}
    for uid in diff["added"] + diff["deleted"] + [m["uid"] for m in diff["modified"]]:
        v = verdict.get(uid)
        if v is None:
            out[uid] = {"by": "unknown", "tx": []}
            counts["unknown"] += 1
        else:
            out[uid] = v
            counts[v["by"]] = counts.get(v["by"], 0) + 1
    return out, counts


# ---------------------------------------------------------------------------
# ops
# ---------------------------------------------------------------------------

def op_smoke(args):
    """Phase 0. Verifies the environment without touching the tracking data."""
    res = {"op": "smoke", "ok": True, "checks": {}}
    c = res["checks"]

    c["execfile_scope"] = (doc is not None)
    c["ident"] = get_ident()

    try:
        DB.Document.GetDocumentVersion(doc)
        c["get_document_version"] = True
    except Exception, e:
        c["get_document_version"] = "FAIL: %s" % e
        res["ok"] = False

    # Hebrew path round trip -- the real trap
    base = args.get("base")
    try:
        ensure_dir(base)
        tp = os.path.join(base, "_smoke.json")
        payload = {"hebrew": c["ident"].get("title"), "path": c["ident"].get("path")}
        save_json(tp, payload)
        back = load_json(tp, None)
        c["hebrew_roundtrip"] = (back == payload)
        if not c["hebrew_roundtrip"]:
            res["ok"] = False
        try:
            System.IO.File.Delete(tp)
            System.IO.File.Delete(tp + ".bak")
        except Exception:
            pass
    except Exception, e:
        c["hebrew_roundtrip"] = "FAIL: %s" % e
        res["ok"] = False

    # TSV sanity guard must actually fire
    try:
        tsv_line(u"x", 1, 2, 3, u"a", u"b", 4, u'{"s":"a\nb"}')
        c["tsv_guard"] = "FAIL: did not raise on embedded newline"
        res["ok"] = False
    except TrackerAbort:
        c["tsv_guard"] = True
    except Exception, e:
        c["tsv_guard"] = "FAIL: %s" % e
        res["ok"] = False

    try:
        ctx_model = snap_ctx("model")
        c["element_count"] = len(collect_elements(ctx_model))
        c["element_count_all"] = len(collect_elements(snap_ctx("all")))
        c["volatile_resolved"] = len(ctx_model["volatile"])
        c["tier1_resolved"] = len(ctx_model["tier1"])
    except Exception, e:
        c["collect"] = "FAIL: %s" % e
        res["ok"] = False

    # Go/no-go measurement for the incremental checkpoint: sweep_ids must be
    # dramatically cheaper than a full record (geo_sig + par_sig) per element,
    # or trading a full rebuild for "sweep every run + rehash the dirty set"
    # is not worth the added machinery. Uses its own clock, deliberately not
    # start_clock()/elapsed_ms() -- that pair is a single global reset by
    # main() once per op, and reusing it here would corrupt the op's own
    # reported elapsed_ms.
    try:
        _t0 = System.DateTime.UtcNow
        n_sweep = len(sweep_ids(ctx_model))
        sweep_ms = (System.DateTime.UtcNow - _t0).TotalMilliseconds
        c["sweep_ms"] = round(sweep_ms, 1)
        c["sweep_count"] = n_sweep
        c["sweep_ms_per_1k"] = (round(sweep_ms * 1000.0 / n_sweep, 2)
                                 if n_sweep else None)
    except Exception, e:
        c["sweep"] = "FAIL: %s" % e
        res["ok"] = False

    try:
        c["documentchanged_available"] = hasattr(doc.Application, "DocumentChanged")
    except Exception:
        c["documentchanged_available"] = False

    return res, True


LOCK_STALE_SEC = 120

_EPOCH = None


def epoch_now():
    """Seconds since 1970 as a plain number.

    Deliberately NOT a formatted timestamp. `System.DateTime.Parse` on an
    ISO string ending in "Z" returns a LOCAL DateTime, so comparing it against
    `UtcNow` yields a negative age on any machine ahead of UTC -- a stale lock
    then looks permanently fresh and a crashed run blocks the project forever.
    Measured here at UTC+3. A number has no timezone to get wrong.
    """
    global _EPOCH
    if _EPOCH is None:
        _EPOCH = System.DateTime(1970, 1, 1, 0, 0, 0, System.DateTimeKind.Utc)
    return float((System.DateTime.UtcNow - _EPOCH).TotalSeconds)


def acquire_lock(idir, run):
    """CreateNew fails atomically if the file exists -- no check-then-act race.

    Revit's API is single-threaded, so two tracker scripts cannot interleave.
    The real contention is two Claude sessions driving the same project folder,
    plus a run that timed out on the wire but is still finishing inside Revit.
    """
    path = os.path.join(idir, ".lock")
    now = epoch_now()

    if file_exists(path):
        cur = load_json(path, None) or {}
        try:
            age = now - float(cur.get("hb_epoch"))
            stale = age > LOCK_STALE_SEC
        except Exception:
            stale = True            # unreadable or legacy lock -- treat as stale
        if not stale:
            return None, cur
        try:
            System.IO.File.Delete(path)
        except Exception:
            pass

    try:
        fs = System.IO.FileStream(path, System.IO.FileMode.CreateNew,
                                  System.IO.FileAccess.Write,
                                  System.IO.FileShare.None)
        fs.Close()
    except Exception:
        return None, load_json(path, None) or {"run": "unknown"}

    rec = {"run": run, "hb_epoch": now, "hb": now_iso()}
    try:
        write_text_atomic(path, jdump(rec))
    except Exception:
        pass
    return path, rec


def release_lock(path):
    if not path:
        return
    try:
        System.IO.File.Delete(path)
    except Exception:
        pass
    for ext in (".bak", ".tmp"):
        try:
            if file_exists(path + ext):
                System.IO.File.Delete(path + ext)
        except Exception:
            pass


def listener_state_before():
    """Snapshot of the listener's own state, taken BEFORE drain_events,
    arm_handler, arm_sync_watch or arm_doc_watch run and mutate it.

    Must be the first line of op_snapshot. Two of the strongest incremental
    gate checks (a generation mismatch, a dropped-event count mismatch)
    compare "what the listener looked like when this baseline was taken" to
    "what it looks like now" -- get the ordering wrong and both compare a
    value against itself, always pass, and silently disable themselves.
    """
    st = get_state()
    return {
        "boot": getattr(st, "boot_id", ""),
        "gen": getattr(st, "gen", 0),
        "dropped": getattr(st, "dropped", 0),
        "doc_epoch": getattr(st, "doc_epoch", 0),
        "sync_active": _sync_active(st),
    }


def _sync_active(st):
    su = getattr(st, "sync_until", None)
    if su is None:
        return False
    try:
        return System.DateTime.UtcNow < su
    except Exception:
        return False


def dirty_from_events(events, id_to_uid):
    """Union of a/m/d ElementIds across drained events -- the candidate set
    of ids that might need rehashing. id_to_uid is the ElementId(str)->uid
    bridge from build_id_index(prev); it is only consulted by the caller,
    not here, since a fresh add cannot resolve to an existing uid anyway.
    """
    ids = set()
    for e in events:
        for k in ("a", "m", "d"):
            for i in (e.get(k) or []):
                ids.add(i)
    return ids


def incremental_ok(lsb, prev, prev_hdr, ident, events, ctx, args):
    """Soundness gate: (True, None) if the incremental path may run this
    checkpoint, else (False, REASON). First hit wins.

    Every check here is static -- needs no sweep, no dirty-set resolution --
    so it is cheap even when the answer is no. Checks that need the dirty
    set or the sweep result (too much changed, the sweep disagrees with the
    snapshot by more than the repair cap, an element's UniqueId could not be
    read, the drift audit failed) live in build_snapshot_incremental instead
    and raise IncrementalGiveUp there.

    NOTHING here may ever cause a rebaseline: a False here just means THIS
    run takes the ordinary full path. prev/prev_hdr are untouched either way.
    """
    if (args.get("mode") or "").lower() == "full":
        return False, "FORCED"

    if prev is None or prev_hdr is None:
        return False, "NO_BASELINE"
    if len(prev) < INCR_MIN_N:
        return False, "MODEL_TOO_SMALL"

    integ = prev_hdr.get("integrity") or {}
    if integ.get("complete") is not True:
        return False, "PREV_PARTIAL"
    if integ.get("n") != len(prev):
        return False, "TSV_HDR_MISMATCH"
    try:
        recon = [u + u"\t" + r for u, r in prev.items()]
        if integ.get("content_fp") != content_fp(recon):
            return False, "TSV_HDR_MISMATCH"
    except Exception:
        return False, "TSV_HDR_MISMATCH"

    if prev_hdr.get("gate") != GATE_SCHEMA:
        return False, "GATE_SCHEMA"

    lst = prev_hdr.get("listener") or {}
    if not lst.get("armed"):
        return False, "LISTENER_DEAD"
    if lst.get("boot") != lsb["boot"]:
        return False, "BOOT_MISMATCH"
    if lst.get("gen") != lsb["gen"]:
        return False, "GEN_MISMATCH"
    if lst.get("ev") != EVENT_SCHEMA:
        return False, "EV_SCHEMA"
    if lst.get("dropped") != lsb["dropped"]:
        return False, "EVENTS_DROPPED"
    if any(e.get("tr") for e in events):
        return False, "EVENT_TRUNCATED"

    pdoc = prev_hdr.get("doc") or {}
    if pdoc.get("canon") != ident.get("canon"):
        return False, "DOC_CHANGED"
    if lst.get("doc_epoch") != lsb["doc_epoch"]:
        return False, "DOC_REOPENED"
    if (pdoc.get("modified") is True and not ident.get("modified")
            and pdoc.get("saves") == ident.get("saves")):
        return False, "DOC_REVERTED"
    try:
        if int(ident.get("saves") or 0) < int(pdoc.get("saves") or 0):
            return False, "SAVES_BACKWARD"
    except Exception:
        return False, "SAVES_BACKWARD"
    if (pdoc.get("vguid") != ident.get("vguid")
            and ident.get("saves") == pdoc.get("saves")):
        return False, "VERSION_NO_SAVE"

    # Worksharing: a sync compromises ATTRIBUTION, not membership -- the
    # sweep (in build_snapshot_incremental) proves membership fresh every
    # run regardless, and a sync large enough to matter already blows
    # MAX_IDS_PER_EVENT, which EVENT_TRUNCATED above already catches. So a
    # sync window or a workshared save does not, by itself, force a full
    # rebuild under the default "verified" policy -- only under "strict",
    # which restores the old blanket behaviour as an escape hatch.
    sync_policy = (args.get("sync_policy") or "verified").lower()
    if sync_policy == "strict":
        if lsb.get("sync_active"):
            return False, "SYNC_STRICT"
        if ident.get("workshared") and ident.get("saves") != pdoc.get("saves"):
            return False, "SYNC_STRICT"

    if integ.get("filter_fp") != filter_fp(ctx):
        return False, "FILTER_FP"

    if integ.get("incr_streak", 0) >= INCR_MAX_STREAK:
        return False, "STREAK_LIMIT"
    full_at_epoch = integ.get("full_at_epoch")
    if full_at_epoch is None:
        return False, "NO_FULL_AT"
    try:
        if (epoch_now() - float(full_at_epoch)) > INCR_MAX_FULL_AGE_S:
            return False, "FULL_AGE"
    except Exception:
        return False, "NO_FULL_AT"

    return True, None


def build_snapshot_incremental(prev, prev_hdr, events, ctx, run, lsb, ident,
                                scope, armed_gen):
    """Rehash only the elements the DocumentChanged buffer says might have
    changed, plus anything a cheap membership sweep disagrees with the
    stored snapshot about. Raises IncrementalGiveUp for THIS run only on any
    dynamic condition incremental_ok could not see; prev/prev_hdr are never
    touched, so a give-up always falls straight through to the ordinary full
    build in the caller.

    -> (header, lines_out, cur, stats)
    """
    id_to_uid = build_id_index(prev)
    dirty_ids = dirty_from_events(events, id_to_uid)

    # Membership is never trusted to the listener alone -- sweep_ids is
    # exact, straight from Revit, at a fraction of make_record's cost. This
    # is what makes a missed creation or deletion (the exact scar behind the
    # SNAPSHOT_FORMAT comment) impossible to miss silently.
    model_ids = sweep_ids(ctx)
    snap_ids = set()
    for uid, rest in prev.items():
        p = rest.split(u"\t")
        if len(p) > 5:
            try:
                snap_ids.add(int(p[5]))
            except Exception:
                pass

    missing = model_ids - snap_ids   # creations the listener never reported
    extra = snap_ids - model_ids     # deletions the listener never reported
    resweep_fixed = 0
    if missing or extra:
        n_disc = len(missing) + len(extra)
        if n_disc > MAX_SWEEP_REPAIR:
            raise IncrementalGiveUp("SWEEP_DRIFT_%d" % n_disc)
        dirty_ids |= missing
        dirty_ids |= extra
        resweep_fixed = n_disc
        # Re-verify once. A second non-empty result means someone is editing
        # underneath us right now, not a listener gap -- the same torn-read
        # condition build_snapshot_chunked's fingerprint guards against.
        if sweep_ids(ctx) != model_ids:
            raise IncrementalGiveUp("SWEEP_UNSTABLE")

    if len(dirty_ids) > INCR_MAX_DIRTY_FRAC * max(len(prev), 1):
        raise IncrementalGiveUp("TOO_DIRTY_%d" % len(dirty_ids))

    cat_names = dict(((prev_hdr.get("tables") or {}).get("cat")) or {})
    typ_names = dict(((prev_hdr.get("tables") or {}).get("typ")) or {})
    lvl_names = dict(((prev_hdr.get("tables") or {}).get("lvl")) or {})

    cur = dict(prev)          # opaque strings, ~free to copy
    dead_uids = set()
    written_uids = set()

    for elid_int in dirty_ids:
        prev_uid = id_to_uid.get(str(elid_int))
        el = None
        try:
            el = doc.GetElement(make_eid(elid_int))
        except Exception:
            el = None

        if el is None:
            if prev_uid is not None:
                dead_uids.add(prev_uid)
            # else: created and deleted between checkpoints -- it never
            # entered the baseline, so there is nothing to retire.
            continue

        try:
            uid = el.UniqueId
        except Exception:
            raise IncrementalGiveUp("UID_UNREADABLE")

        ok, cid = passes_filter(el, ctx)
        if not ok:
            if prev_uid is not None:
                dead_uids.add(prev_uid)
            continue

        if prev_uid is not None and prev_uid != uid:
            # Revit reused this ElementId for a DIFFERENT element (typically
            # after an undone creation). Retire the old uid explicitly; the
            # new one gets written below like any other live element.
            dead_uids.add(prev_uid)

        try:
            line = make_record(el, cid, ctx, cat_names, typ_names, lvl_names)
        except TrackerAbort:
            raise
        except Exception:
            continue

        i = line.find(u"\t")
        new_uid = line[:i]
        cur[new_uid] = line[i + 1:]
        written_uids.add(new_uid)

    for uid in dead_uids:
        cur.pop(uid, None)

    # Drift audit: re-hash a small sample of rows this run did NOT already
    # touch, to catch a bug in the incremental logic itself quickly.
    # Reliably catches SYSTEMIC drift; a single stale element is bounded by
    # INCR_MAX_STREAK instead, not by this sample (see the constants block).
    non_dirty = [u for u in cur if u not in written_uids]
    k = 0
    audit_fail = 0
    if non_dirty:
        k = min(len(non_dirty),
                max(AUDIT_MIN, min(AUDIT_MAX, int(len(prev) * AUDIT_FRAC))))
    if k > 0:
        rnd = System.Random()
        sample_idx = set()
        while len(sample_idx) < k:
            sample_idx.add(rnd.Next(len(non_dirty)))
        for idx in sample_idx:
            uid = non_dirty[idx]
            rest = cur.get(uid, u"")
            p = rest.split(u"\t")
            if len(p) < 6:
                continue
            try:
                elid_int = int(p[5])
                el = doc.GetElement(make_eid(elid_int))
                if el is None:
                    audit_fail += 1
                    continue
                ok2, cid2 = passes_filter(el, ctx)
                if not ok2:
                    audit_fail += 1
                    continue
                fresh_line = make_record(el, cid2, ctx, cat_names, typ_names, lvl_names)
                fi = fresh_line.find(u"\t")
                fresh_fields = fresh_line[fi + 1:].split(u"\t")[0:5]
                stored_fields = p[0:5]
                if fresh_fields != stored_fields:
                    audit_fail += 1
            except Exception:
                audit_fail += 1

    if audit_fail > 0:
        raise IncrementalGiveUp("AUDIT_FAIL_%d" % audit_fail)

    saw_undo = False
    for e in events:
        op = e.get("op")
        if op and (u"Undo" in op or u"Redo" in op or u"RolledBack" in op):
            saw_undo = True
            break

    lines_out = sorted(u + u"\t" + cur[u] for u in cur)
    prev_integ = prev_hdr.get("integrity") or {}
    header = {
        "v": SNAPSHOT_FORMAT,
        "taken": now_iso(),
        "scope": scope,
        "gate": GATE_SCHEMA,
        "mode": "incremental",
        "run": run,
        "tables": {"cat": cat_names, "typ": typ_names, "lvl": lvl_names},
        "doc": {
            "canon": ident.get("canon"), "saves": ident.get("saves"),
            "vguid": ident.get("vguid"), "modified": ident.get("modified"),
            "workshared": ident.get("workshared"),
        },
        "listener": {
            # POST-bump gen, same reasoning as the full path's header -- see
            # the comment at that assignment. Must use the same convention
            # or a full run followed by an incremental one (or vice versa)
            # would mismatch on gen alone.
            "armed": True, "boot": lsb["boot"], "gen": armed_gen,
            "ev": EVENT_SCHEMA, "dropped": lsb["dropped"],
            "doc_epoch": lsb["doc_epoch"],
        },
        "integrity": {
            "n": len(cur), "complete": True, "total_seen": len(cur),
            "fp": fingerprint(model_ids), "chunks": 1,
            "content_fp": content_fp(lines_out),
            "filter_fp": filter_fp(ctx),
            "incr_streak": int(prev_integ.get("incr_streak", 0)) + 1,
            "full_at": prev_integ.get("full_at"),
            "full_at_epoch": prev_integ.get("full_at_epoch"),
            "full_run": prev_integ.get("full_run"),
            "dirty": len(dirty_ids),
            "events": len(events),
            "resweep_fixed": resweep_fixed,
            "audit_k": k,
            "audit_fail": audit_fail,
            "notes": ([u"SAW_UNDO"] if saw_undo else []),
        },
    }
    stats = {
        "dirty": len(dirty_ids), "resweep_fixed": resweep_fixed,
        "audit_k": k, "audit_fail": audit_fail, "total_seen": len(cur),
    }
    return header, lines_out, cur, stats


def op_snapshot(args):
    """Identify, snapshot, diff against the stored baseline, persist."""
    lsb = listener_state_before()
    budget = float(args.get("budget_ms", BUDGET_MS_DEFAULT))
    base = args.get("base")
    if not base:
        raise TrackerAbort("MISSING_BASE")

    ident = get_ident()
    loc = resolve_instance(base, ident)
    idir = loc["instance_dir"]

    lock, lockrec = acquire_lock(idir, args.get("_run"))
    if lock is None:
        raise TrackerAbort("LOCKED_BY:%s" % lockrec.get("run", "unknown"))
    get_state().active_lock = lock      # main() releases it, even on failure

    # Drain BEFORE snapshotting: these are the transactions that produced the
    # changes we are about to diff. Re-arm after, so the listener is live for
    # whatever happens next.
    events = drain_events(idir, args.get("_run"))
    armed = arm_handler(ident)
    armed["sync"] = arm_sync_watch()
    armed["doc"] = arm_doc_watch()
    sync_note = check_sync_window()
    if sync_note:
        loc["notes"].append(sync_note)
        # Evidence is compromised: demote confident sync labels to unknown.
        for e in events:
            if e.get("sync"):
                e["sync"] = False
                e["ours"] = False
                e["tx"] = []

    tsv_path = os.path.join(idir, "snapshot.tsv")
    hdr_path = os.path.join(idir, "snapshot.hdr.json")

    prev = load_tsv(tsv_path)
    prev_hdr = load_json(hdr_path, None)

    baseline_lost = False
    if prev is None and file_exists(tsv_path):
        prev = load_tsv(tsv_path + ".bak")
        prev_hdr = load_json(hdr_path + ".bak", None)
        if prev is None:
            baseline_lost = True

    scope = args.get("scope", "model")

    # A baseline is only comparable when it was produced the same way.
    # Anything else re-baselines rather than inventing changes. Checked here,
    # before the build, so the incompatibility is known up front rather than
    # discovered only after the current snapshot is already in hand -- which
    # matters once an incremental path needs this answer before deciding
    # whether a baseline exists to diff against at all.
    if prev is not None:
        incompatible = None
        if prev_hdr is None:
            incompatible = "HEADER_MISSING"
        elif prev_hdr.get("v") != SNAPSHOT_FORMAT:
            incompatible = "FORMAT_%s_TO_%s" % (prev_hdr.get("v"), SNAPSHOT_FORMAT)
        elif prev_hdr.get("scope", "model") != scope:
            incompatible = "SCOPE_%s_TO_%s" % (prev_hdr.get("scope", "model"), scope)
        if incompatible:
            loc["notes"].append("REBASELINE_" + incompatible)
            prev = None
            prev_hdr = None

    ctx = snap_ctx(scope)   # once; shared by the full build and (from the
                            # incremental path on) the sweep and dirty-set too
    hdr, lines, truncated, total, progress = build_snapshot_chunked(
        budget, scope, args.get("_run"), ctx)
    hdr["scope"] = scope
    hdr["gate"] = GATE_SCHEMA
    hdr["mode"] = "full"
    hdr["run"] = args.get("_run")
    hdr["doc"] = {
        "canon": ident.get("canon"), "saves": ident.get("saves"),
        "vguid": ident.get("vguid"), "modified": ident.get("modified"),
        "workshared": ident.get("workshared"),
    }
    # POST-bump gen (arm_handler's own return value), NOT lsb["gen"].
    # arm_handler runs once per checkpoint and always bumps st.gen, so
    # storing the pre-bump value here would make every run's stored gen
    # exactly one behind what the VERY NEXT run observes -- a permanent,
    # unconditional GEN_MISMATCH with nothing ever wrong. Storing the
    # post-bump value means back-to-back runs match, and a mismatch only
    # appears when something ELSE bumped gen in between (an intervening run
    # that drained events and armed but never wrote a header) -- which is
    # the actual condition this check exists to catch. Also fed to the
    # shadow-mode incremental build below, so both paths agree.
    armed_gen = armed.get("gen", lsb["gen"])
    hdr["listener"] = {
        "armed": bool(armed.get("armed")), "boot": lsb["boot"],
        "gen": armed_gen, "ev": EVENT_SCHEMA, "dropped": lsb["dropped"],
        "doc_epoch": lsb["doc_epoch"],
    }
    hdr["integrity"]["content_fp"] = content_fp(lines)
    hdr["integrity"]["filter_fp"] = filter_fp(ctx)
    # A full run resets the incremental streak/age counters. full_at_epoch is
    # numeric UTC (epoch_now(), same reasoning as the .lock file's hb_epoch)
    # -- full_at alongside it is ISO but LOCAL time with no offset marker
    # (now_iso() uses System.DateTime.Now), so it is display-only and must
    # never be parsed back for a comparison against UtcNow.
    hdr["integrity"]["incr_streak"] = 0
    hdr["integrity"]["full_at"] = hdr["taken"]
    hdr["integrity"]["full_at_epoch"] = epoch_now()
    hdr["integrity"]["full_run"] = args.get("_run")

    cur = {}
    for ln in lines:
        i = ln.find(u"\t")
        cur[ln[:i]] = ln[i + 1:]

    # SHADOW MODE. The incremental path is built and compared against the
    # full result above, but the full result (cur, hdr, lines) is always what
    # gets diffed, written to disk and returned -- this block can only add a
    # note, never change behaviour. This is deliberate: it is the only way to
    # earn confidence in the dirty-set logic against real edits before it is
    # allowed to affect a real diff. Guarded so nothing in here can fail the
    # run: an exception anywhere in shadow mode is caught and noted, never
    # raised past this block.
    if not truncated and prev is not None:
        try:
            ok_incr, why_incr = incremental_ok(
                lsb, prev, prev_hdr, ident, events, ctx, args)
            if ok_incr:
                try:
                    _shdr, _slines, scur, sstats = build_snapshot_incremental(
                        prev, prev_hdr, events, ctx, args.get("_run"),
                        lsb, ident, scope, armed_gen)
                    mism = 0
                    for u in set(cur.keys()) | set(scur.keys()):
                        if cur.get(u) != scur.get(u):
                            mism += 1
                    if mism:
                        loc["notes"].append("INCR_SHADOW_MISMATCH_%d" % mism)
                    else:
                        loc["notes"].append(
                            "INCR_SHADOW_OK_dirty%d" % sstats["dirty"])
                except IncrementalGiveUp, ge:
                    loc["notes"].append("INCR_SHADOW_GIVEUP_%s" % str(ge))
            else:
                loc["notes"].append("INCR_SHADOW_SKIP_%s" % why_incr)
        except Exception, se:
            loc["notes"].append("INCR_SHADOW_ERROR_%s" % str(se))

    result = {
        "op": "snapshot",
        "run": args.get("_run"),
        "at": now_iso(),
        "ident": ident,
        "instance_id": loc["instance_id"],
        "folder": loc["folder"],
        "instance_dir": idir,
        "notes": loc["notes"],
        "counts": {"elements": len(lines), "seen": total},
        "truncated": truncated,
        "progress": progress,
        "listener": armed,
        "events_seen": len(events),
        "elapsed_ms": round(elapsed_ms(), 1),
    }

    # Budget exhausted. Report progress and stop -- the caller re-invokes the
    # same op to continue from the cursor. No diff, no write: a partial pass
    # must never be mistaken for a baseline.
    if truncated:
        result["status"] = "partial"
        result["baseline"] = "IN_PROGRESS"
        result["notes"].append("CHUNK_%d_OF_MANY" % progress["chunk"])
        result["resume"] = "call the same op again to continue"
        return result, True
    result["status"] = "complete"

    if baseline_lost:
        result["baseline"] = "BASELINE_LOST"
        result["notes"].append("BASELINE_LOST")
    elif prev is None:
        result["baseline"] = "CREATED"
        result["notes"].append("BASELINE_CREATED")
    else:
        d = diff_snapshots(prev, cur)
        result["baseline"] = "COMPARED"
        result["diff"] = {
            "added": len(d["added"]),
            "deleted": len(d["deleted"]),
            "modified": len(d["modified"]),
        }
        cur_ids = build_id_index(cur)
        prev_ids = build_id_index(prev)
        attr, acounts = attribute(d, cur_ids, prev_ids, events)
        result["attribution"] = acounts
        if ident.get("workshared"):
            changed_ids = []
            for uid in d["added"] + [m["uid"] for m in d["modified"]]:
                p = cur.get(uid, u"").split(u"\t")
                if len(p) > 5:
                    changed_ids.append(p[5])
            wsi = worksharing_owners(changed_ids)
            if wsi:
                result["worksharing"] = wsi
        # describe() is run once, at full size. The two former call sites
        # (limit=50 for the result summary, limit=100000 for the delta file)
        # differed only in how many of each kind of item they kept, and the
        # limit=100000 output is a strict superset -- so the summary is
        # derived from it instead of paying for a second describe() pass.
        full_items = describe(d, prev, cur, prev_hdr, hdr, limit=100000)
        capped_counts = {}
        capped_items = []
        for it in full_items:
            k = it["kind"]
            if capped_counts.get(k, 0) < 50:
                capped_counts[k] = capped_counts.get(k, 0) + 1
                # A copy: the by/tx merge below must stay out of full_items,
                # which the delta file below reuses as-is and must not carry
                # per-run attribution fields that were never part of its shape.
                capped_items.append(dict(it))
        result["items"] = capped_items
        for it in result["items"]:
            a = attr.get(it["uid"])
            if a:
                it["by"] = a["by"]
                if a["tx"]:
                    it["tx"] = a["tx"]
        result["items_capped"] = (
            len(d["added"]) + len(d["deleted"]) + len(d["modified"]) > len(result["items"])
        )
        if result["diff"]["added"] or result["diff"]["deleted"] or result["diff"]["modified"]:
            seq = int(System.DateTime.Now.ToString("yyyyMMddHHmmss"))
            dpath = os.path.join(idir, "deltas", "d-%d.json" % seq)
            save_json(dpath, {
                "run": args.get("_run"), "at": now_iso(),
                "added": d["added"], "deleted": d["deleted"],
                "modified": [{"uid": m["uid"], "fields": m["fields"]} for m in d["modified"]],
                "items": full_items,
                "attribution": dict((k, v["by"]) for k, v in attr.items()),
                "transactions": [{"t": e["t"], "tx": e["tx"], "ours": e["ours"],
                                  "n": {"a": len(e["a"]), "m": len(e["m"]), "d": len(e["d"])}}
                                 for e in events],
            })
            result["delta_file"] = dpath

    # Snapshot is written LAST, and only when complete, so a partial pass can
    # never be mistaken for a valid baseline on the next run.
    if not truncated:
        write_lines_atomic(tsv_path, lines)
        save_json(hdr_path, hdr)

        idx = load_json(loc["index_path"], None)
        if idx:
            r = idx.get("instances", {}).get(loc["instance_id"])
            if r is not None:
                r["last_saves"] = ident["saves"]
                r["last_vguid"] = ident["vguid"]
                r["last_snapshot"] = now_iso()
                r["last_count"] = len(lines)
                save_json(loc["index_path"], idx)

    try:
        append_lines(os.path.join(idir, "events.ndjson"), [jdump({
            "t": now_iso(), "run": args.get("_run"), "kind": "snapshot",
            "baseline": result.get("baseline"), "n": len(lines),
            "diff": result.get("diff"), "notes": result["notes"],
        })])
    except Exception:
        pass

    return result, True


OPS = {
    "smoke": op_smoke,
    "snapshot": op_snapshot,
    "session_start": op_snapshot,
    "checkpoint": op_snapshot,
}


def main(op, args=None):
    """Entry point.

    Always writes the full result to RESULT_PATH (ASCII path, so the wire
    never carries Hebrew), then raises TrackerOK for read-only passes.

    TrackerOK is SUCCESS, not failure. It forces the host transaction to roll
    back so snapshots never appear in the user's Undo list. Two conditions
    must both hold for a caller to treat a 500 as success:
      1. the message starts with TRACKER_OK|<run_id>
      2. RESULT_PATH exists and its "run" equals that run_id
    Anything else is a genuine failure.
    """
    start_clock()
    args = dict(args or {})
    run = new_run_id()
    args["_run"] = run
    # OPS maps both session_start and checkpoint to op_snapshot; this is how
    # op_snapshot can still tell which one it was called as.
    args["_op"] = op

    fn = OPS.get(op)
    if fn is None:
        raise TrackerAbort("UNKNOWN_OP:%s" % op)

    try:
        result, read_only = fn(args)
        result["run"] = run
        result["ok"] = result.get("ok", True)
    except TrackerAbort, e:
        result = {"op": op, "run": run, "ok": False, "error": str(e), "at": now_iso()}
        read_only = True
    except Exception, e:
        result = {
            "op": op, "run": run, "ok": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "traceback": traceback.format_exc(),
            "at": now_iso(),
        }
        read_only = True

    result["elapsed_ms"] = round(elapsed_ms(), 1)

    # Release the folder lock on every path, success or failure, so a crashed
    # run never blocks the next one for the full stale timeout.
    try:
        st = get_state()
        release_lock(getattr(st, "active_lock", None))
        st.active_lock = None
    except Exception:
        pass

    try:
        ensure_dir(LIB_DIR)
        write_text_atomic(RESULT_PATH, json.dumps(result, ensure_ascii=True, indent=1, sort_keys=True))
    except Exception, e:
        # Nothing on disk means nothing to read. Fail loudly and visibly.
        print(jdump({"ok": False, "run": run, "error": "RESULT_WRITE_FAILED: %s" % e}))
        return

    if read_only:
        raise TrackerOK("TRACKER_OK|%s" % run)

    print(jdump({"ok": result.get("ok"), "run": run, "result": RESULT_PATH}))
