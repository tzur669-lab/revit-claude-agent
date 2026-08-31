# -*- coding: utf-8 -*-
from pyrevit import DB
import os
import traceback
import logging

logger = logging.getLogger(__name__)


class _FailureSwallower(DB.IFailuresPreprocessor):
    """Resolve Revit failures during a transaction without ever showing a modal
    dialog. Warnings are deleted (the operation proceeds); if any error-severity
    failure is present, the transaction is rolled back. Either way the headless
    Routes server keeps running instead of hanging on a dialog."""

    def PreprocessFailures(self, failuresAccessor):
        try:
            # Delete all warnings so they don't block (operation continues).
            failuresAccessor.DeleteAllWarnings()
            # If any genuine errors remain, roll back rather than go modal.
            for f in failuresAccessor.GetFailureMessages():
                if f.GetSeverity() == DB.FailureSeverity.Error:
                    return DB.FailureProcessingResult.ProceedWithRollBack
        except Exception:
            pass
        return DB.FailureProcessingResult.Continue


def suppress_warnings(transaction):
    """Configure a transaction so Revit failures never block on a modal dialog.

    Essential for unattended/headless operation: without this, a routine Revit
    warning (e.g. overlapping walls) OR an error (e.g. "Can't cut instance out
    of Wall") pops a modal dialog that blocks the Routes server indefinitely —
    every later request then times out until a human clicks the dialog.

    Warnings are auto-deleted (operation proceeds); errors roll the transaction
    back cleanly. Call right after transaction.Start(). Best-effort — never raises.
    """
    try:
        opts = transaction.GetFailureHandlingOptions()
        opts.SetForcedModalHandling(False)
        opts.SetClearAfterRollback(True)
        opts.SetFailuresPreprocessor(_FailureSwallower())
        transaction.SetFailureHandlingOptions(opts)
    except Exception:
        pass


# All seven DB.TransactionStatus members, derived live from this Revit
# session via System.Enum.GetNames(DB.TransactionStatus) - not guessed. Only
# Committed means the database actually accepted the change:
#   Uninitialized, Started  - Commit() did not take effect
#   RolledBack               - the _FailureSwallower above returns
#                               ProceedWithRollBack on any error-severity
#                               failure, and Commit() then returns RolledBack
#                               WITHOUT raising - this is the exact mechanism
#                               behind "MoveElement returned success and
#                               moved nothing"
#   Pending                  - still open; never report a pending write as done
#   Error, Proceed           - not documented as steady-state Commit() results;
#                               treated as failures rather than assumed safe
# Fail closed: any status this set does not name - including one a future
# Revit version might add - is treated as failure, never as success.
_TX_OK_STATUSES = frozenset([DB.TransactionStatus.Committed])


def commit_verified(t):
    """Commit a transaction and report what Revit actually did.

    Low-level verification only: this function does NOT construct MCP
    responses and does NOT decide handler policy. It returns
    (tx_ok, tx_status) - a bool (or None) and the exact TransactionStatus
    name as a string - and nothing else. Each caller maps that outcome into
    its own existing response contract; keeping this module ignorant of the
    response shape is deliberate, since every route imports it.

    tx_ok is True only when Commit() returns Committed. Every other status
    maps to False (see _TX_OK_STATUSES above for why each one does).

    Committed means the database accepted the change. It does NOT mean the
    operation achieved its intent - that is the caller's own post-condition
    check to make, separately.

    t may be None - the "#!notx" convention in code_execution.py, where the
    caller opted out of the wrapping transaction and manages its own. In
    that case this returns (None, "self_managed"): not True, not False.
    self_managed means the transaction outcome was not observable by this
    helper, never that it succeeded - a caller must not treat it as a pass.
    """
    if t is None:
        return None, "self_managed"
    status = t.Commit()
    return (status in _TX_OK_STATUSES), str(status)


# Provisional - see docs/operation-contracts.md. Not a documented Revit
# constant; a starting tolerance to be refined from measured behaviour.
PARAM_DOUBLE_TOLERANCE = 1e-6


def param_read_matches(param, storage_type, intended):
    """Re-read a parameter's CURRENT value and compare it to the typed value
    that was requested - not a raw string comparison, since requesting "5"
    for a Double parameter that correctly reads back 5.0 must not read as a
    mismatch. Used as the level-2 post-condition for set_parameter and
    modify_element: Set() not raising is not proof the value actually holds
    once Revit's regeneration finishes running. Returns (matches, actual_str).

    Shared by parameters.py and editing.py rather than duplicated - both
    handlers set parameters by the same four storage types and need the
    same comparison semantics."""
    try:
        if storage_type == DB.StorageType.String:
            actual = param.AsString() or ""
            return actual == str(intended), actual
        elif storage_type == DB.StorageType.Integer:
            actual = param.AsInteger()
            return actual == int(intended), str(actual)
        elif storage_type == DB.StorageType.Double:
            actual = param.AsDouble()
            return abs(actual - float(intended)) <= PARAM_DOUBLE_TOLERANCE, str(round(actual, 6))
        elif storage_type == DB.StorageType.ElementId:
            actual = get_element_id_value(param.AsElementId())
            return actual == get_element_id_value(make_element_id(intended)), str(actual)
    except Exception as e:
        return False, "read failed: {}".format(str(e))
    return False, "unsupported storage type"


def verify_created_elements(doc, id_category_pairs):
    """Level-2 post-condition for create_* operations: each created element
    id must resolve, and (when an expected BuiltInCategory is given for that
    item - pass None to skip) its actual category must match.

    id_category_pairs is a list of (element_id_int, expected_builtin_category
    or None). Handles batches that mix categories in one call (e.g.
    create_line_based creates both walls and beams) by checking each item
    against its own expected category rather than one category for the
    whole batch. Returns a verified dict in the standard shape.

    Shared by building.py and rooms.py rather than duplicated per handler."""
    checked = 0
    failures = []
    for eid, expected_cat in id_category_pairs:
        checked += 1
        elem = doc.GetElement(DB.ElementId(eid))
        if elem is None:
            failures.append({"id": eid, "reason": "does not resolve after commit"})
            continue
        if expected_cat is not None:
            try:
                actual_cat = elem.Category.Id.IntegerValue if elem.Category else None
            except Exception:
                actual_cat = None
            if actual_cat != int(expected_cat):
                failures.append({
                    "id": eid,
                    "reason": "category mismatch",
                    "expected_category": int(expected_cat),
                    "actual_category": actual_cat,
                })
    verified = {
        "ok": (len(failures) == 0) if checked else None,
        "method": "element_category",
        "expected": {"count": checked},
        "actual": {"count_ok": checked - len(failures)},
    }
    if not checked:
        verified["status"] = "not_checked"
        verified["reason"] = "Nothing was created"
    if failures:
        verified["failures"] = failures[:50]
    return verified


def verified_ok(method, expected, actual):
    """Build the passing shape of the documented `verified` dict - see
    docs/operation-contracts.md. One place these three shapes are
    constructed, instead of each handler assembling the dict literal by
    hand (characterized and consolidated from 8 near-identical inline
    blocks across annotation.py, tags.py, interop.py (x2), transforms.py,
    mep.py, view_management.py, documentation.py)."""
    return {"ok": True, "method": method, "expected": expected, "actual": actual}


def verified_failed(method, expected, actual, reason=None, failures=None):
    """The failing shape. `failures`, when given, is capped at 50 items -
    matching verify_created_elements's own convention above, so a huge
    failed batch cannot bloat a response unboundedly."""
    v = {"ok": False, "method": method, "expected": expected, "actual": actual}
    if reason:
        v["reason"] = reason
    if failures:
        v["failures"] = failures[:50]
    return v


def verified_not_checked(reason):
    """The not_checked shape - ok is None, never True or False. Used when
    there was nothing to check (an empty batch) or no fixed contract
    applies (execute_revit_code, color_splash/clear_colors)."""
    return {"ok": None, "status": "not_checked", "reason": reason}


def verify_elements_exist(doc, ids, empty_reason="Nothing to verify - empty id list"):
    """Level-2 post-condition: every id in a batch still resolves. Shared
    by annotation.py (create_dimensions), tags.py (tag_elements),
    interop.py (link_file's single-id case, ids=[id]), and
    transforms.py (transform_elements's copy branch) - characterized from
    those four near-identical inline blocks, which agreed on every
    observable field except two: whether an empty id list means ok=None
    (three of four sites) or a vacuous ok=True (transforms.py's copy
    branch - though that branch's own `and new_element_ids` guard means
    ids is never actually empty when reached, so this is not an observable
    behavior change there), and whether failures was capped at 50
    (transforms.py already did; the batch sites in annotation.py/tags.py
    did not - capped here for all callers, consistent with
    verify_created_elements above).

    empty_reason lets each caller keep its own original wording ("No
    dimension was created" vs "No tag was created") for the empty-batch
    case instead of a generic message.

    Uses make_element_id (not a bare DB.ElementId(int)) since that is this
    codebase's own documented Revit-2027-safe pattern (see
    code_execution.py's comment on the same ambiguity) - three of the four
    original inline blocks used the bare form; unified here rather than
    left inconsistent."""
    if not ids:
        return verified_not_checked(empty_reason)
    still_missing = [i for i in ids if doc.GetElement(make_element_id(i)) is None]
    expected = {"count": len(ids)}
    actual = {"count_ok": len(ids) - len(still_missing)}
    if still_missing:
        return verified_failed(
            "element_exists", expected, actual,
            failures=[{"id": i} for i in still_missing],
        )
    return verified_ok("element_exists", expected, actual)


def verify_element_named(element, expected_name, actual_name, subject="Element"):
    """Level-2 post-condition: a single element resolves AND carries the
    name it was requested to have. Shared by mep.py (create_mep_system) and
    view_management.py (create_view) - characterized from those two
    near-identical inline blocks. Each site keeps its own way of resolving
    `element` and reading `actual_name` (mep.py reads a "System Name"/
    "Comments" parameter; view_management.py uses get_element_name) since
    that difference is a real, intentional per-element-type distinction,
    not duplication - only the ok/expected/actual/reason assembly was
    actually duplicated.

    `subject` customizes the reason text ("System does not resolve..." vs
    "View does not resolve...") to match each site's original wording."""
    resolves = element is not None
    matches = bool(resolves and actual_name == expected_name)
    expected = {"name": expected_name}
    actual = {"name": actual_name, "resolves": resolves}
    if matches:
        return verified_ok("element_exists_and_name", expected, actual)
    return verified_failed(
        "element_exists_and_name", expected, actual,
        reason="{} does not resolve or its name does not match what was requested".format(subject),
    )


def verify_file_written(file_path, min_bytes=1):
    """Level-2 post-condition for operations whose real product is a file
    on disk, not model state (tx_status Committed says nothing about
    whether Revit's export/save API actually wrote anything). Shared by
    documentation.py (export_document) and interop.py (export_ifc) -
    characterized from those two near-identical inline blocks, both
    checked against a "size_kb > 0" threshold (i.e. at least 1024 bytes,
    after KB truncation) - both callers pass min_bytes=1024 explicitly to
    preserve that exact existing threshold; a new caller wanting a
    stricter/looser check can pass its own.

    documentation.py's original guarded `bool(file_path) and
    os.path.exists(...)`; interop.py's did not (a None file_path would
    have raised inside the try/except Exception: pass, silently reporting
    not-exists rather than raising - so behaviourally equivalent already,
    just made explicit here for both callers)."""
    exists = False
    size_bytes = 0
    try:
        exists = bool(file_path) and os.path.exists(file_path)
        if exists:
            size_bytes = os.path.getsize(file_path)
    except Exception:
        pass
    size_kb = int(size_bytes / 1024)
    expected = {"file_path": file_path}
    actual = {"exists": exists, "size_kb": size_kb}
    if exists and size_bytes >= min_bytes:
        return verified_ok("file_exists", expected, actual)
    return verified_failed(
        "file_exists", expected, actual,
        reason="Export reported success but the output file does not exist or is empty",
    )


def _to_text(value):
    """Convert a value to text without corrupting non-ASCII characters.
    IronPython 2.7 compatible: tries unicode first (its native string type
    for .NET strings), falls back to str for Py3 environments.

    IronPython 2.7's unicode(str) does not raise on non-ASCII bytes the way
    CPython 2.7 does - it silently maps each byte to the codepoint of the
    same value (a Latin-1 decode), verified directly against this engine.
    A str carrying UTF-8 bytes - e.g. straight out of urllib.unquote() on a
    percent-encoded URL path segment, which is how view_name and similar
    <param> route arguments arrive - comes out as mojibake: one wrong
    "character" per original UTF-8 byte, no exception raised. Repaired
    below via the same encode('latin-1')/decode('utf-8') round-trip used
    for POST-body JSON in hebrew_io_fix.py. Safe no-op on text that was
    never corrupted: ASCII round-trips unchanged, and genuine Unicode text
    (real Hebrew sits at codepoints 1488-1514, past latin-1's 0-255 range)
    fails the encode('latin-1') step and is returned as-is."""
    try:
        text = unicode(value)
    except NameError:
        return str(value)
    return repair_hebrew_text(text)


def repair_hebrew_text(text):
    """Undo a Latin-1-per-byte mojibake decode of UTF-8 text, if present.

    This is the same repair _to_text applies internally, exposed directly
    for callers that already have a unicode/str value in hand - e.g. a
    route handler's request.data dict, parsed by pyrevit core's
    _prepare_request (pyrevit/routes/server/server.py) in a DIFFERENT
    engine scope than this extension's own code. See handler.py's own
    comment on base.Response for confirmation this cross-scope split is a
    real, documented pyRevit architecture detail, not a guess: "this
    module is executed on a different Engine than the script that
    registered the request handler function". A monkeypatch on pyRevit
    core's HttpRequestHandler therefore never reaches route handlers in
    this extension - verified live, 2026-08-24: the patch showed as
    installed on pyrevit.routes.server.server.HttpRequestHandler when
    checked immediately after installing it, yet request.data as received
    by this extension's own handlers stayed uncorrected. The repair must
    happen here instead, at the point each handler actually reads its
    text params - same round-trip as _to_text, safe no-op on text that
    was never corrupted (see _to_text's docstring for why)."""
    if not isinstance(text, unicode):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def repair_hebrew_in(value):
    """Recursively apply repair_hebrew_text to every string in a parsed
    JSON value (dict/list/str), leaving other types untouched. Call this
    on request.data as the first thing a POST handler does, before
    reading any individual field, so every text parameter - not just the
    ones a handler happens to route through normalize_string/
    sanitize_string - gets fixed the same way."""
    if isinstance(value, dict):
        return dict((repair_hebrew_in(k), repair_hebrew_in(v)) for k, v in value.items())
    if isinstance(value, list):
        return [repair_hebrew_in(v) for v in value]
    if isinstance(value, unicode):
        return repair_hebrew_text(value)
    return value


def normalize_string(text):
    """Safely normalize string values, preserving Unicode text (e.g. Hebrew)
    for JSON serialization. JSON natively supports Unicode, so there is no
    need to strip non-ASCII characters -- doing so previously turned Hebrew
    (and other non-ASCII) text into literal '?' characters."""
    if text is None:
        return "Unnamed"
    try:
        result = _to_text(text).strip()
        return result if result else "Unnamed"
    except Exception:
        return "Unnamed"


def sanitize_string(text):
    """Safely convert a value to text for JSON serialization, preserving
    Unicode characters (e.g. Hebrew)."""
    if text is None:
        return "Unnamed"
    try:
        return _to_text(text)
    except Exception:
        return "Unnamed"


def get_element_name(element):
    """
    Get the name of a Revit element.
    Useful for both FamilySymbol and other elements.
    Returns ASCII-safe string for JSON serialization.
    """
    try:
        name = element.Name
    except AttributeError:
        name = DB.Element.Name.__get__(element)
    return sanitize_string(name)


def get_element_id_value(element_or_id):
    """
    Extract an integer element ID from an Element or ElementId.
    Accepts both a full Revit Element and a raw ElementId (duck typing).
    Compatible with Revit 2024, 2025, 2026, and 2027.
    Returns a plain Python int for JSON serialization.
    Raises ValueError if the ID cannot be extracted or input is None.
    """
    if element_or_id is None:
        raise ValueError("Cannot extract ElementId from None")
    try:
        eid = element_or_id.Id if hasattr(element_or_id, "Id") else element_or_id
    except Exception:
        raise ValueError("Cannot extract ElementId from input: {}".format(
            type(element_or_id).__name__))
    try:
        return int(eid.Value)
    except (AttributeError, TypeError):
        pass
    try:
        return int(eid.IntegerValue)
    except (AttributeError, TypeError):
        raise ValueError("Cannot read ID value from: {}".format(
            type(element_or_id).__name__))


def make_element_id(id_value):
    """
    Create a DB.ElementId from an integer value.
    Compatible with Revit 2024, 2025, 2026, and 2027.
    Tries System.Int64 constructor first (2024+), falls back to int.
    Raises ValueError if the ElementId cannot be created or input is invalid.
    """
    if id_value is None:
        raise ValueError("Cannot create ElementId from None")
    try:
        int_val = int(id_value)
    except (TypeError, ValueError):
        raise ValueError("Cannot create ElementId from {}: not a valid integer".format(
            repr(id_value)))
    try:
        import System
        return DB.ElementId(System.Int64(int_val))
    except (TypeError, OverflowError, ImportError):
        pass
    try:
        return DB.ElementId(int_val)
    except Exception as e:
        raise ValueError("Cannot create ElementId from {}: {}".format(
            id_value, str(e)))


def find_family_symbol_safely(doc, target_family_name, target_type_name=None):
    """
    Safely find a family symbol by name.
    Uses get_element_name() for consistent string handling in IronPython.
    """
    try:
        collector = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)

        for symbol in collector:
            try:
                fam_name = sanitize_string(symbol.Family.Name)
            except Exception:
                continue
            if fam_name == target_family_name:
                if not target_type_name or get_element_name(symbol) == target_type_name:
                    return symbol
        return None
    except Exception as e:
        logger.error("Error finding family symbol: %s", str(e))
        return None
