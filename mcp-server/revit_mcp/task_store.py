# -*- coding: utf-8 -*-
"""
Task store -- the filesystem contract that lets a plan/task exist across two
runtimes (the CPython MCP server and the IronPython pyRevit routes process)
and across HTTP requests, client timeouts, and Revit restarts.

Deliberately Revit-API-free: no `pyrevit`, `DB`, or `doc` import anywhere in
this module. That is what makes it importable identically from three places:

  1. IronPython route handlers inside revit_mcp/*.py: `from task_store import ...`
     (bare -- matches every other sibling-module import in this package, e.g.
     `from utils import ...` in impact.py).
  2. IronPython ad-hoc code submitted through execute_revit_code: the bare
     form above does NOT resolve there (that exec() runs in a namespace that
     does not inherit the extension's own sys.path injection -- measured
     live, 2026-09-01); use `from revit_mcp.task_store import ...` instead.
  3. CPython, from tools/plan_tools.py or the test suite: `from revit_mcp
     import task_store` -- works because mcp-server/ is the package root on
     sys.path for the FastMCP server process, exactly as
     tests/conftest_revit_mcp.py's import_revit_mcp_module_as_package()
     already relies on for revit_mcp.utils etc.

Because this module needs zero fakes to run under CPython, it needs zero
fakes to TEST under CPython either -- tests/test_task_store.py imports it
directly, no conftest_revit_mcp.py fixtures required.

Design note worth recording: plan_hash() lives HERE, not duplicated once in
tools/plan_schema.py (CPython) and once in revit_mcp/plan_exec.py
(IronPython) with a cross-runtime equivalence test to catch drift between
them. Since this module is already provably importable from both runtimes,
duplicating the hash and testing for agreement would be strictly worse than
just sharing the one implementation -- there is nothing for the two copies
to drift apart FROM.

Two file-durability policies, deliberately different (see revision-4 ADR
"failure policy split"):

  - append_ledger() is FAIL-STOP. It raises LedgerAppendFailed on any I/O
    problem. The caller (plan_exec.py's slice loop) MUST treat that as "do
    not mutate the model" -- an unrecorded mutation is the one failure this
    whole architecture exists to make impossible.
  - write_progress() is fail-SOFT by convention: it can raise (this module
    does not swallow the exception itself, since that policy choice belongs
    to the caller), but callers are expected to wrap it and continue. A
    cosmetic progress-file failure must never roll back a valid Revit
    operation.

The ledger (ledger.ndjson) is the single source of truth; progress.json is a
pure, deterministic, disposable projection of it (fold_progress()). Losing
progress.json loses nothing -- rebuild it by reading the ledger again.
"""
import hashlib
import io
import json
import os
import time
import uuid

try:
    import System
    _HAS_SYSTEM = True
except ImportError:
    _HAS_SYSTEM = False
    System = None


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

# Same root and same REVIT_TRACKER_DIR override tracker.py's LIB_DIR uses --
# this is a sibling contract under the same directory, not a new namespace.
LIB_DIR = os.environ.get(
    "REVIT_TRACKER_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker"),
)
TASKS_DIR = os.path.join(LIB_DIR, "tasks")


def task_dir_path(task_id):
    return os.path.join(TASKS_DIR, task_id)


def plan_path(task_dir):
    return os.path.join(task_dir, "plan.json")


def approval_path(task_dir):
    return os.path.join(task_dir, "approval.json")


def ledger_path(task_dir):
    return os.path.join(task_dir, "ledger.ndjson")


def progress_path(task_dir):
    return os.path.join(task_dir, "progress.json")


def result_path(task_dir):
    return os.path.join(task_dir, "result.json")


def verify_path(task_dir):
    return os.path.join(task_dir, "verify.json")


def lock_path(task_dir):
    return os.path.join(task_dir, ".lock")


def cancel_path(task_dir):
    return os.path.join(task_dir, "cancel.json")


# ---------------------------------------------------------------------------
# clock -- epoch seconds only, never a parsed DateTime string
#
# Mirrors tracker.py's own epoch_now() exactly, for the exact same reason:
# System.DateTime.Parse on a 'Z'-suffixed ISO string returns LOCAL time, so
# comparing it to UtcNow gives a NEGATIVE age on any machine ahead of UTC
# (measured at UTC+3 on this machine) -- a stale lock then looks permanently
# fresh and blocks the task forever. A plain number has no timezone to get
# wrong. now_iso() is display-only and must never be parsed back -- guarded
# by tests/test_task_store.py's AST check that this module never calls
# DateTime.Parse.
# ---------------------------------------------------------------------------

_EPOCH_ANCHOR = None


def epoch_now():
    if _HAS_SYSTEM:
        global _EPOCH_ANCHOR
        if _EPOCH_ANCHOR is None:
            _EPOCH_ANCHOR = System.DateTime(1970, 1, 1, 0, 0, 0, System.DateTimeKind.Utc)
        return float((System.DateTime.UtcNow - _EPOCH_ANCHOR).TotalSeconds)
    return time.time()


def now_iso():
    """Display-only. Never parsed back -- see epoch_now()'s docstring."""
    if _HAS_SYSTEM:
        return System.DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss")
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def new_task_id():
    """t-YYYYMMDD-HHMMSS-<4hex> -- structurally like tracker.new_run_id()'s
    r-... shape, visually distinct. Built from epoch_now() via gmtime(), so
    there is nothing here for the Parse/local-time trap to reach."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(epoch_now()))
    tail = uuid.uuid4().hex[:4]
    return "t-%s-%s" % (stamp, tail)


# ---------------------------------------------------------------------------
# low-level file IO -- System.IO under IronPython, io/os under CPython.
#
# IronPython 2.7's stdlib `os` module has no os.replace() (added in Python
# 3.3) and os.rename() is not an atomic replace-if-exists on Windows -- this
# is exactly why tracker.py uses System.IO.File.Replace instead of the
# stdlib. The CPython fallback below exists ONLY so this module -- and
# therefore the ledger/lock/state-machine logic built on it -- is testable
# offline with zero new fakes; production always has System available
# (mscorlib/System.dll are default-referenced in IronPython, so plain
# `import System` works with no clr.AddReference, exactly as tracker.py
# already relies on).
# ---------------------------------------------------------------------------

def _file_exists(path):
    if _HAS_SYSTEM:
        try:
            return System.IO.File.Exists(path)
        except Exception:
            return False
    return os.path.isfile(path)


def _ensure_dir(path):
    if not path:
        return
    if _HAS_SYSTEM:
        if not System.IO.Directory.Exists(path):
            System.IO.Directory.CreateDirectory(path)
        return
    if not os.path.isdir(path):
        os.makedirs(path)


def _delete_file(path):
    try:
        if _HAS_SYSTEM:
            if System.IO.File.Exists(path):
                System.IO.File.Delete(path)
        else:
            if os.path.isfile(path):
                os.remove(path)
    except Exception:
        pass


def _read_text(path):
    if _HAS_SYSTEM:
        try:
            return System.IO.File.ReadAllText(path, System.Text.UTF8Encoding(False))
        except Exception:
            return None
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _write_text(path, text):
    unicode_type = type(u"")
    payload = text if isinstance(text, unicode_type) else text.decode("ascii")
    with io.open(path, "w", encoding="ascii") as f:
        f.write(payload)


def _replace_atomic(tmp_path, dest_path, bak_path):
    """System.IO.File.Replace is one atomic OS-level operation (ReplaceFile)
    -- tmp becomes dest, dest's previous content becomes bak, tmp is gone,
    all or nothing. The CPython fallback (offline tests only) uses two
    independently-atomic os.replace() calls -- each step is atomic on
    Windows (MoveFileEx/MOVEFILE_REPLACE_EXISTING) but the PAIR is not a
    single atomic unit the way File.Replace is. Acceptable here because
    production never takes this branch."""
    if _HAS_SYSTEM:
        if System.IO.File.Exists(dest_path):
            System.IO.File.Replace(tmp_path, dest_path, bak_path, True)
        else:
            System.IO.File.Move(tmp_path, dest_path)
        return
    if os.path.exists(dest_path):
        try:
            os.replace(dest_path, bak_path)
        except Exception:
            pass
    os.replace(tmp_path, dest_path)


def _create_new_file(path):
    """Atomic create-if-absent -- no check-then-act race. Mirrors
    tracker.acquire_lock's FileStream(CreateNew, ..., FileShare.None); the
    CPython fallback (O_CREAT|O_EXCL) has the same atomicity guarantee."""
    try:
        if _HAS_SYSTEM:
            fs = System.IO.FileStream(
                path, System.IO.FileMode.CreateNew, System.IO.FileAccess.Write,
                getattr(System.IO.FileShare, "None"),
            )
            fs.Close()
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        return True
    except Exception:
        return False


def _append_line(path, line):
    _ensure_dir(os.path.dirname(path))
    if _HAS_SYSTEM:
        fs = System.IO.FileStream(
            path, System.IO.FileMode.Append, System.IO.FileAccess.Write,
            System.IO.FileShare.Read,
        )
        sw = System.IO.StreamWriter(fs, System.Text.UTF8Encoding(False))
        try:
            sw.WriteLine(line)
        finally:
            sw.Close()
        return
    with io.open(path, "a", encoding="utf-8") as f:
        f.write(line + u"\n")


# ---------------------------------------------------------------------------
# JSON files -- atomic write, .bak-fallback read. Same discipline as
# tracker.py's state.json / journal.ndjson: tmp -> verify it parses ->
# .bak -> replace. A valid old file beats a broken new one.
# ---------------------------------------------------------------------------

def canonical_json(obj):
    """Deterministic, ASCII-safe JSON text -- byte-identical on both
    runtimes for the same logical content. Matches tracker.jdump()'s own
    convention (sort_keys, fixed separators, ensure_ascii=True) exactly."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def write_json_atomic(path, obj):
    """tmp -> prove it round-trips through json.loads -> .bak -> replace.
    ensure_ascii=True is mandatory: Hebrew (room names, etc.) goes to disk
    as \\uXXXX escapes, never raw bytes -- the same rule tracker.py's
    header states."""
    text = json.dumps(obj, ensure_ascii=True, indent=1, sort_keys=True)
    json.loads(text)  # prove it round-trips BEFORE anything touches disk
    _ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    bak = path + ".bak"
    _write_text(tmp, text)
    _replace_atomic(tmp, path, bak)


def _try_load_json(path):
    if not _file_exists(path):
        return None
    text = _read_text(path)
    if text is None:
        return None
    text = text.lstrip(u"﻿").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def read_json_with_fallback(path):
    """(obj_or_None, read_from) where read_from in ('primary','bak','none').
    An unparseable primary falls back to .bak and DECLARES it -- never a
    silent substitution. Two unreadable files means None, 'none', never a
    guess."""
    obj = _try_load_json(path)
    if obj is not None:
        return obj, "primary"
    obj = _try_load_json(path + ".bak")
    if obj is not None:
        return obj, "bak"
    return None, "none"


# ---------------------------------------------------------------------------
# plan identity -- content hash, and the op-source version bound to approval
# ---------------------------------------------------------------------------

# Every key here changes executable behaviour; nothing else does. A plan
# key outside this list can be edited after approval with no re-approval
# required (risks prose, assumptions, confidence, intent, timestamps,
# notes). Unknown top-level plan keys are rejected at validation time
# (tools/plan_schema.py) rather than folded in here fail-closed -- that
# removes the ambiguity instead of merely handling it safely.
MATERIAL_PLAN_KEYS = (
    "schema_v", "objective", "scope", "constraints", "phases", "actions",
    "expected_changes", "success_criteria", "policy", "reasoning",
)


def _material_subset(plan):
    return dict((k, plan.get(k)) for k in MATERIAL_PLAN_KEYS)


def _md5_12(text):
    data = text.encode("ascii") if isinstance(text, type(u"")) else text
    return hashlib.md5(data).hexdigest()[:12]


def plan_hash(plan):
    """md5(canonical_json(material subset))[:12] -- tracker.h12()'s own
    12-hex convention. Deterministic across both runtimes because
    canonical_json() is."""
    return _md5_12(canonical_json(_material_subset(plan)))


def ops_version(ops_source_path=None):
    """md5 of ops.py's own source text, bound into approval.json. This repo
    is junction-linked into a live pyRevit install and edited during
    development -- not a theoretical concern -- so an edit to the op
    implementations after a plan was approved must void that approval.
    Returns None (never a guess) if the file cannot be read."""
    if ops_source_path is None:
        ops_source_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops.py")
    text = _read_text(ops_source_path)
    if text is None:
        return None
    return _md5_12(text)


# ---------------------------------------------------------------------------
# ledger -- append-only, one writer (under the task lock), fail-stop
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_V = 1

# Ledger record kinds this fold understands structurally. verdict/note/
# preflight are recognised-but-not-folded (they matter for audit, not for
# the projection's core fields). Anything else is preserved verbatim by
# read_ledger() but flagged by fold_progress() as unknown -- never silently
# dropped from visibility, per revision-4's ledger invariant.
KNOWN_LEDGER_KINDS = frozenset([
    "task_meta", "state", "outcome_amend", "slice_open", "action_begin",
    "stage", "action_end", "slice_close", "verdict", "note", "preflight",
])


class LedgerAppendFailed(Exception):
    """Raised when a ledger record could not be durably written. FAIL-STOP:
    the caller must not proceed to mutate the model -- an unrecorded
    mutation is the one failure this architecture exists to make
    impossible. Never catch this to "try again silently"; surface it."""


def append_ledger(task_dir, record):
    """Append one JSON line. Assigns 'seq' (monotonic -- caller must hold
    the task lock, so this is race-free by construction, not by locking
    inside this function) and 't' if not already present. Raises
    LedgerAppendFailed on any I/O problem."""
    path = ledger_path(task_dir)
    rec = dict(record)
    rec.setdefault("lv", LEDGER_SCHEMA_V)
    if "seq" not in rec:
        rec["seq"] = _next_seq(task_dir)
    if "t" not in rec:
        rec["t"] = now_iso()
    line = canonical_json(rec)
    try:
        _append_line(path, line)
    except Exception as e:
        raise LedgerAppendFailed(str(e))
    return rec


def _next_seq(task_dir):
    records, _ = read_ledger(task_dir)
    if not records:
        return 1
    return max((r.get("seq", 0) for r in records)) + 1


def read_ledger(task_dir):
    """(records, meta). meta = {'truncated_tail': bool, 'stopped_at_line':
    int|None}.

    A trailing unparseable line is a truncated write (the process died
    mid-append) -- dropped silently, noted in meta. An unparseable line in
    the MIDDLE is real corruption: reading stops there, records before it
    are still returned, and 'stopped_at_line' is set so the caller can
    raise ledger_corrupt rather than silently continuing past a hole in
    the truth."""
    text = _read_text(ledger_path(task_dir))
    if text is None:
        return [], {"truncated_tail": False, "stopped_at_line": None}
    lines = text.split(u"\n")
    if lines and lines[-1] == u"":
        lines = lines[:-1]
    records = []
    truncated_tail = False
    stopped_at = None
    for i, ln in enumerate(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            records.append(json.loads(ln))
        except Exception:
            if i == len(lines) - 1:
                truncated_tail = True
            else:
                stopped_at = i
            break
    return records, {"truncated_tail": truncated_tail, "stopped_at_line": stopped_at}


def find_open_action(task_dir):
    """Scan the ledger for an action_begin with no matching action_end --
    the crash-window row of the recovery matrix. Returns the action_begin
    record, {'kind': 'corrupt', 'open_indices': [...]} if more than one is
    open (impossible under Phase A's strict sequential execution, so a
    real corruption signal if seen), or None if the ledger is clean.

    This function only detects that an action is open; it does not decide
    what that means -- resolving "unknown outcome" needs a live document
    (an op's probe()) and belongs to plan_exec.py, not here."""
    records, _ = read_ledger(task_dir)
    begins = {}
    for rec in records:
        if rec.get("kind") == "action_begin":
            begins[rec.get("i")] = rec
        elif rec.get("kind") == "action_end":
            begins.pop(rec.get("i"), None)
    if not begins:
        return None
    if len(begins) > 1:
        return {"kind": "corrupt", "open_indices": sorted(begins.keys())}
    return list(begins.values())[0]


# ---------------------------------------------------------------------------
# projection -- pure fold over the ledger, no I/O, no clock reads
# ---------------------------------------------------------------------------

SCHEMA_V = 1
MAX_ACTIONS_IN_RESPONSE = 10

SLICE_ATOMICITY = frozenset([
    "assimilated", "rolled_back", "assimilate_failed", "group_leak",
    "rollback_failed", "open",
])


def fold_progress(records):
    """Deterministic fold: ledger records -> the progress.json shape. Two
    calls with the same records list must return identical output -- this
    is what makes "rebuild from the ledger" a real recovery path rather
    than a promise. No I/O; every timestamp comes from the record itself."""
    lifecycle = "proposed"
    outcome = None
    review_reasons = set()
    task_id = None
    ph = None
    boot_id = None
    baseline_run = None
    canon = None
    slices = []
    actions = {}
    cur_slice = None
    unknown_kinds = set()
    higher_lv_seen = set()

    for rec in records:
        kind = rec.get("kind")
        lv = rec.get("lv", 1)
        if lv > LEDGER_SCHEMA_V:
            higher_lv_seen.add(lv)
        if kind not in KNOWN_LEDGER_KINDS:
            unknown_kinds.add(kind)
            continue

        if kind == "task_meta":
            task_id = rec.get("task_id", task_id)
            ph = rec.get("plan_hash", ph)
            boot_id = rec.get("boot_id", boot_id)
            baseline_run = rec.get("baseline_run", baseline_run)
            canon = rec.get("canon", canon)
        elif kind == "state":
            if rec.get("ok"):
                lifecycle = rec.get("to", lifecycle)
                if lifecycle == "finished":
                    outcome = rec.get("outcome")
                to_state = rec.get("to")
                if to_state == "blocked" and rec.get("review_reasons"):
                    review_reasons = set(rec["review_reasons"])
                elif rec.get("from") == "blocked" and to_state != "blocked":
                    review_reasons = set()
        elif kind == "outcome_amend":
            outcome = rec.get("outcome", outcome)
        elif kind == "slice_open":
            cur_slice = {
                "n": rec.get("n"), "group": rec.get("group"),
                "budget_ms": rec.get("budget_ms"), "started": rec.get("t"),
                "ended": None, "slice_atomicity": None, "elapsed_ms": None,
                "actions": [],
            }
        elif kind == "action_begin":
            i = rec.get("i")
            actions[i] = {
                "i": i, "op": rec.get("op"), "phase": rec.get("phase"),
                "status": "running", "stage": "resolving",
                "targets": rec.get("targets"),
                "before": rec.get("before"), "inverse": rec.get("inverse"),
                "inverse_fidelity": rec.get("inverse_fidelity"),
                "tx_status": None, "execution_ok": None, "verified": None,
                "touched_ids": [], "touched_uids": [], "created_ids": [],
                "cascaded_ids": [], "elapsed_ms": None, "error": None,
            }
            if cur_slice is not None:
                cur_slice["actions"].append(i)
        elif kind == "stage":
            i = rec.get("i")
            if i in actions:
                actions[i]["stage"] = rec.get("stage")
        elif kind == "action_end":
            i = rec.get("i")
            if i in actions:
                actions[i].update({
                    "status": rec.get("status", "done"),
                    "stage": "recording",
                    "tx_status": rec.get("tx_status"),
                    "execution_ok": rec.get("execution_ok"),
                    "verified": rec.get("verified"),
                    "touched_ids": rec.get("touched_ids", []),
                    "touched_uids": rec.get("touched_uids", []),
                    "created_ids": rec.get("created_ids", []),
                    "cascaded_ids": rec.get("cascaded_ids", []),
                    "elapsed_ms": rec.get("elapsed_ms"),
                    "error": rec.get("error"),
                })
        elif kind == "slice_close":
            if cur_slice is not None:
                cur_slice["ended"] = rec.get("t")
                cur_slice["slice_atomicity"] = rec.get("slice_atomicity")
                cur_slice["elapsed_ms"] = rec.get("elapsed_ms")
                slices.append(cur_slice)
                cur_slice = None
        # verdict / note / preflight: intentionally not folded here.

    if cur_slice is not None:
        # crash during slice close -- surface the open slice, don't hide it.
        cur_slice["slice_atomicity"] = "open"
        slices.append(cur_slice)

    ordered = [actions[i] for i in sorted(actions.keys())]
    total = len(ordered)
    done = sum(1 for a in ordered if a["status"] == "done")
    failed = sum(1 for a in ordered if a["status"] == "failed")
    skipped = sum(1 for a in ordered if a["status"] == "skipped")
    pending = total - done - failed - skipped

    current_action = None
    for a in ordered:
        if a["status"] == "running":
            current_action = a["i"]
            break

    if not ordered:
        execution_ok = None
    elif any(a["execution_ok"] is False for a in ordered):
        execution_ok = False
    elif done == total and all(a["execution_ok"] is True for a in ordered):
        execution_ok = True
    else:
        execution_ok = None

    def _vok(a):
        v = a.get("verified")
        return v.get("ok") if isinstance(v, dict) else None

    if not ordered:
        verification_ok = None
    elif any(_vok(a) is False for a in ordered):
        verification_ok = False
    elif done == total and all(_vok(a) is True for a in ordered):
        verification_ok = True
    else:
        verification_ok = None

    return {
        "v": SCHEMA_V,
        "task_id": task_id,
        "plan_hash": ph,
        "lifecycle": lifecycle,
        "outcome": outcome,
        "review_reasons": sorted(review_reasons),
        "boot_id": boot_id,
        "baseline_run": baseline_run,
        "canon": canon,
        "cursor": done + failed + skipped,
        "current_action": current_action,
        "slices": slices,
        "actions": ordered,
        "counts": {"total": total, "done": done, "failed": failed,
                   "skipped": skipped, "pending": pending},
        "verdicts": {"execution_ok": execution_ok,
                     "verification_ok": verification_ok, "design_ok": None},
        "unknown_ledger_kinds": sorted(unknown_kinds),
        "higher_ledger_schema_seen": sorted(higher_lv_seen),
        "updated_epoch": epoch_now(),
    }


def summarize_for_response(progress):
    """Bounded view for an MCP tool response: counts, current slice/action,
    the last MAX_ACTIONS_IN_RESPONSE actions, plus every failing action
    regardless of position. Never the full action list -- format_response's
    MAX_DATA_CHARS=12000 cap would silently truncate an unbounded one."""
    actions = progress.get("actions", [])
    tail = actions[-MAX_ACTIONS_IN_RESPONSE:]
    tail_idx = set(a["i"] for a in tail)
    failures = [a for a in actions if a.get("status") == "failed" and a["i"] not in tail_idx]
    out = dict(progress)
    out["actions"] = failures + tail
    out["actions_shown"] = len(out["actions"])
    out["actions_total"] = len(actions)
    return out


def write_progress(task_dir, progress=None):
    """Rewrite progress.json from the ledger (or a pre-folded dict). Atomic.
    Failure here is fail-SOFT by policy -- this function does not swallow
    the exception itself (that choice belongs to the caller), it just
    performs the write; callers in plan_exec.py wrap this and continue."""
    if progress is None:
        records, _ = read_ledger(task_dir)
        progress = fold_progress(records)
    write_json_atomic(progress_path(task_dir), progress)
    return progress


def read_progress(task_dir):
    """(progress_dict, meta). Falls back to .bak and declares it; if
    neither parses, rebuilds straight from the ledger (the authoritative
    source anyway) rather than ever reporting 'unreadable' -- unlike
    plan.json/approval.json, progress.json is disposable by construction."""
    obj, read_from = read_json_with_fallback(progress_path(task_dir))
    if obj is not None:
        return obj, {"read_from": read_from}
    records, ledger_meta = read_ledger(task_dir)
    rebuilt = fold_progress(records)
    return rebuilt, {"read_from": "rebuilt", "ledger": ledger_meta}


# ---------------------------------------------------------------------------
# task lock -- one per task, held only for the duration of one slice
# ---------------------------------------------------------------------------

LOCK_STALE_SEC = 120.0


def acquire_task_lock(task_dir, run):
    """CreateNew fails atomically if the file exists -- no check-then-act
    race. Mirrors tracker.acquire_lock() exactly, pointed at
    <task_dir>/.lock instead of <instance_dir>/.lock -- a SEPARATE lock
    that protects none of the tracker's files and is protected by none of
    theirs (same independence as the code-execution audit directory)."""
    path = lock_path(task_dir)
    now = epoch_now()
    if _file_exists(path):
        cur, _ = read_json_with_fallback(path)
        cur = cur or {}
        try:
            stale = (now - float(cur.get("hb_epoch"))) > LOCK_STALE_SEC
        except Exception:
            stale = True  # unreadable or legacy lock -- treat as stale
        if not stale:
            return None, cur
        _delete_file(path)

    if not _create_new_file(path):
        cur, _ = read_json_with_fallback(path)
        return None, (cur or {"run": "unknown"})

    rec = {"run": run, "hb_epoch": now, "hb": now_iso()}
    try:
        write_json_atomic(path, rec)
    except Exception:
        pass
    return path, rec


def release_task_lock(path):
    if not path:
        return
    _delete_file(path)
    for ext in (".bak", ".tmp"):
        _delete_file(path + ext)


# ---------------------------------------------------------------------------
# cancellation -- a file flag, honoured only at the between-actions boundary
# ---------------------------------------------------------------------------

def request_cancel(task_dir, mode="rollback_slice", reason=None):
    """Written from CPython (cancel_task tool) -- works even while Revit's
    UI thread is busy running a slice, since it is just a file write. The
    executor cannot act on it mid-action: a Revit API call already in
    flight cannot be safely interrupted. It is checked once, between
    actions, alongside the slice time budget."""
    if mode not in ("rollback_slice", "keep_slice"):
        raise ValueError("mode must be 'rollback_slice' or 'keep_slice', got %r" % (mode,))
    write_json_atomic(cancel_path(task_dir), {
        "requested": True, "mode": mode, "reason": reason, "at": now_iso(),
    })


def check_cancel(task_dir):
    obj, _ = read_json_with_fallback(cancel_path(task_dir))
    return obj if (obj and obj.get("requested")) else None


def clear_cancel(task_dir):
    _delete_file(cancel_path(task_dir))


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------

LIFECYCLE_STATES = frozenset(["proposed", "approved", "running", "paused", "blocked", "finished"])
OUTCOMES = frozenset(["completed", "failed", "aborted", "rejected", "superseded", "rolled_back"])
REVIEW_REASONS = frozenset([
    "unknown_action_outcome", "precondition_failed", "ref_ambiguous",
    "revit_restarted", "assimilate_unknown", "group_leak", "ledger_corrupt",
    "document_identity_unsafe",
])

# "finished" is the sole terminal lifecycle value, parameterised by
# `outcome`. rolled_back is reachable only via amend_outcome() on an
# already-finished task (when its compensation task itself completes) --
# never a direct lifecycle transition, so lifecycle stays genuinely
# terminal at "finished" and TRANSITIONS below never needs a self-edge.
TRANSITIONS = {
    "proposed": frozenset(["approved", "finished"]),
    "approved": frozenset(["running", "blocked", "finished"]),
    "running": frozenset(["paused", "blocked", "finished"]),
    "paused": frozenset(["running", "blocked", "finished"]),
    "blocked": frozenset(["running", "finished"]),
    "finished": frozenset(),
}


class IllegalTransition(Exception):
    pass


def init_task(task_id, ph, boot_id=None, baseline_run=None, canon=None):
    """Create the task directory and its first two ledger records:
    task_meta, then the initial proposed state. Call exactly once, at
    propose time -- not idempotent."""
    task_dir = task_dir_path(task_id)
    _ensure_dir(task_dir)
    append_ledger(task_dir, {
        "kind": "task_meta", "task_id": task_id, "plan_hash": ph,
        "boot_id": boot_id, "baseline_run": baseline_run, "canon": canon,
    })
    append_ledger(task_dir, {
        "kind": "state", "from": None, "to": "proposed", "ok": True,
        "reason": "propose_plan",
    })
    try:
        write_progress(task_dir)
    except Exception:
        pass
    return task_dir


def current_state(task_dir):
    """(lifecycle, outcome, review_reasons_set, progress_dict)."""
    progress, _ = read_progress(task_dir)
    return (
        progress.get("lifecycle", "proposed"),
        progress.get("outcome"),
        set(progress.get("review_reasons") or []),
        progress,
    )


def transition(task_dir, to_lifecycle, reason, outcome=None, review_reasons=None):
    """The one function either runtime uses to change a task's lifecycle.

    Validates against TRANSITIONS and appends a 'state' ledger record
    UNCONDITIONALLY -- legal or not. An illegal attempt is itself evidence
    of a lifecycle bug and must be visible in the ledger, not hidden by an
    exception that leaves no trace. Raises IllegalTransition if the move
    was not permitted (after recording it). A ledger write failure raises
    LedgerAppendFailed first and takes priority -- if the attempt itself
    cannot be recorded, nothing proceeds, including the illegal-transition
    signal."""
    lifecycle, _, _, _ = current_state(task_dir)
    allowed = TRANSITIONS.get(lifecycle, frozenset())
    ok = to_lifecycle in allowed
    rec = {"kind": "state", "from": lifecycle, "to": to_lifecycle, "ok": ok, "reason": reason}
    if outcome is not None:
        rec["outcome"] = outcome
    if review_reasons:
        rec["review_reasons"] = sorted(set(review_reasons))
    append_ledger(task_dir, rec)  # LedgerAppendFailed propagates -- fail-stop
    if not ok:
        raise IllegalTransition(
            "%s -> %s not permitted (allowed from %s: %s)"
            % (lifecycle, to_lifecycle, lifecycle, sorted(allowed))
        )
    try:
        write_progress(task_dir)
    except Exception:
        pass  # fail-soft: the ledger record above is what matters
    return rec


def amend_outcome(task_dir, new_outcome, reason):
    """Change a FINISHED task's outcome after the fact. The only legitimate
    case: the original task's outcome moves failed -> rolled_back once its
    compensation task (a separate task, derived_from.relation="compensation")
    reaches outcome=completed. NOT a lifecycle transition -- lifecycle stays
    "finished" throughout, recorded as its own ledger kind so it is visible
    without ever being confused with a TRANSITIONS edge."""
    lifecycle, cur_outcome, _, _ = current_state(task_dir)
    if lifecycle != "finished":
        raise IllegalTransition(
            "amend_outcome only applies to a finished task (current lifecycle: %s)" % lifecycle
        )
    if new_outcome not in OUTCOMES:
        raise ValueError("unknown outcome %r" % (new_outcome,))
    rec = {"kind": "outcome_amend", "from_outcome": cur_outcome, "outcome": new_outcome, "reason": reason}
    append_ledger(task_dir, rec)
    try:
        write_progress(task_dir)
    except Exception:
        pass
    return rec
