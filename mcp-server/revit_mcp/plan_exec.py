# -*- coding: UTF-8 -*-
"""
The Phase-A execution engine: one HTTP call = one BUDGETED SLICE = one
DB.TransactionGroup containing one DB.Transaction per action.

Mechanics proven live against this project's real model before any of
this was written (2026-09-01, see docs/operation-contracts.md):
  - TransactionGroup exposes RollBack() -- NOT RollBackAll(), which does
    not exist.
  - TransactionGroup has no GetFailureHandlingOptions/SetFailureHandling
    Options -- suppress_warnings() goes on the INNER transactions only;
    calling it on the group would silently no-op (its own try/except
    swallows the resulting AttributeError) while looking protected.
  - Assimilate() merges the slice's inner transactions into ONE undo
    entry named after the group; Commit() would leave them separate.
  - A failed inner transaction (commit_verified -> tx_ok is False) does
    NOT poison the group -- a later action in the same slice can still
    commit successfully and the group can still Assimilate() cleanly.
  - Never hold a group open across HTTP requests: a group outliving its
    handler is owned by nobody, and a client timeout would strand the
    model with no code path left to close it.

Every op's `run` is transaction-agnostic (revit_mcp/ops.py); this module
owns every Start()/commit_verified()/RollBack()/Assimilate() call.
"""
from pyrevit import routes, revit, DB

import evaluators
import ops
import refs
import task_store
from refs import resolve_ref
from utils import get_element_id_value, suppress_warnings, commit_verified, repair_hebrew_in

import json
import logging
import sys
import traceback

logger = logging.getLogger(__name__)

# The ordering that must hold: slice budget < HTTP timeout (main.py's
# ENDPOINT_TIMEOUTS["/execute_plan/"] = 75.0) < client patience. The
# budget bounds when the loop STOPS STARTING new actions -- it cannot
# bound an action already running, nor Assimilate(), nor the ledger/
# progress writes, nor queueing behind another route on the UI thread.
DEFAULT_SLICE_BUDGET_MS = 20000.0
MAX_SLICE_BUDGET_MS = 25000.0


def _current_canon(doc):
    """Minimal, self-contained document-identity signal. Deliberately NOT
    importing memory-system/tracker/tracker.py -- that module expects
    doc/DB/etc. injected as globals by execfile, not a real importable
    package. Matches tracker.get_ident()'s 'canon' field in spirit (a
    lowercased path), not by shared code."""
    try:
        path = doc.PathName
    except Exception:
        path = None
    if not path:
        return None
    return path.strip().lower()


def _get_boot_id():
    """The tracker's engine-lifetime GUID, read WITHOUT creating it --
    tracker.get_state() (never imported here) MINTS a boot_id when absent,
    which would change the tracker's own view of engine lifetime as a side
    effect of merely looking. sys.modules is the same mechanism the
    tracker itself uses (a persistent namespace across execfile runs)."""
    st = sys.modules.get("revit_tracker_state")
    return getattr(st, "boot_id", "") if st is not None else ""


# ---------------------------------------------------------------------------
# preflight -- ONE function, TWO call sites (plan_tools.py's approve_plan
# calls this too, advisory only). Binding here: failure blocks the slice.
#
# Scoped-down from the full design on purpose, disclosed rather than
# hidden: full per-target UniqueId drift detection ("did this id start
# pointing at a DIFFERENT element since propose time") would need a
# Revit round-trip at propose time (CPython has no direct doc access) to
# capture a uid baseline to compare against. Not implemented in this
# pass -- preflight checks that every target still EXISTS, not that it is
# still the SAME element. Each op's own capture()/verify() cycle at run
# time provides a partial (not complete) safety net on top of this.
# ---------------------------------------------------------------------------

def preflight(doc, plan, approval=None):
    """approval=None is the pre-approval mode: approve_plan calls this
    (via /preflight_plan/) BEFORE approval.json exists, to capture current
    canon/ops_version/boot_id for stamping into it and to run the checks
    that don't need a prior baseline (ops_exist, refs_resolve). Every
    execute_plan call passes the real, already-written approval -- the
    binding mode with a baseline to compare against."""
    checked = []
    failures = []
    current_canon = _current_canon(doc)
    current_ov = task_store.ops_version()

    if approval is not None:
        checked.append("plan_hash")
        ph = task_store.plan_hash(plan)
        if ph != approval.get("plan_hash"):
            failures.append({"check": "plan_hash", "reason": "plan_hash mismatch - plan edited after approval",
                              "expected": approval.get("plan_hash"), "actual": ph})

        checked.append("ops_version")
        approved_ov = approval.get("ops_version")
        if approved_ov is not None and current_ov is not None and approved_ov != current_ov:
            failures.append({"check": "ops_version", "reason": "ops.py changed after approval",
                              "expected": approved_ov, "actual": current_ov})

        checked.append("schema_v")
        if plan.get("schema_v") != approval.get("schema_v"):
            failures.append({"check": "schema_v", "reason": "schema_v mismatch",
                              "expected": approval.get("schema_v"), "actual": plan.get("schema_v")})

        checked.append("document_identity")
        approved_canon = approval.get("canon")
        if approved_canon and current_canon and approved_canon != current_canon:
            failures.append({"check": "document_identity", "reason": "document canon path changed",
                              "expected": approved_canon, "actual": current_canon})

    checked.append("ops_exist")
    for a in plan.get("actions", []):
        if a.get("op") not in ops.OPS:
            failures.append({"check": "ops_exist", "reason": "unknown op", "op": a.get("op"), "i": a.get("i")})

    checked.append("refs_resolve")
    for a in plan.get("actions", []):
        targets = a.get("targets")
        try:
            elem_ids = resolve_ref(doc, targets)
        except Exception as e:
            failures.append({"check": "refs_resolve", "reason": str(e), "i": a.get("i")})
            continue
        for eid in elem_ids:
            if doc.GetElement(eid) is None:
                failures.append({"check": "refs_resolve", "reason": "target no longer exists",
                                  "i": a.get("i"), "id": get_element_id_value(eid)})

    return {"ok": len(failures) == 0, "failures": failures, "checked": checked,
            "canon": current_canon, "ops_version": current_ov, "boot_id": _get_boot_id()}


# ---------------------------------------------------------------------------
# resume classification -- the recovery matrix's crash-window row
# ---------------------------------------------------------------------------

def _classify_open_action(doc, open_action):
    """(decision, note, recovered_ids) where decision in "run" | "skip" |
    "block". "absolute"-class ops converge on re-application, so an
    unknown outcome is always safe to just run again. "creating"-class ops
    must probe(). recovered_ids is None except for a "skip" whose probe()
    could conclusively resolve the actual created element (Phase B slice
    2: create_room's probe already resolves it via GetRoomAtPoint; the
    other three creating ops' probes are unconditional ("unknown", None) --
    a pre-existing, disclosed Phase A limitation, not solved here) -- when
    present, the caller can record the real id instead of an empty
    created_ids, which is what makes a later created_by reference to this
    action's output resolvable after a crash-resume skip."""
    op_name = open_action.get("op")
    spec = ops.OPS.get(op_name)
    if spec is None:
        return "block", "unknown op %r for open action" % (op_name,), None
    if spec["idempotency"] == "absolute":
        return "run", "absolute op - safe to re-apply", None
    probe_fn = spec.get("probe")
    if probe_fn is None:
        return "block", "creating op with no probe()", None
    try:
        outcome, recovered_ids = probe_fn(doc, open_action.get("args") or {}, open_action.get("before"))
    except Exception as e:
        return "block", "probe() raised: %s" % str(e), None
    if outcome == "not_applied":
        return "run", "probe: not yet applied", None
    if outcome == "applied":
        return "skip", "probe: already applied", recovered_ids
    return "block", "probe: outcome unknown", None


# ---------------------------------------------------------------------------
# the slice loop
# ---------------------------------------------------------------------------

def _run_slice(doc, task_id, task_dir, plan, policy, budget_ms, cancel_check_enabled=True):
    """Executes actions from the current cursor until the plan ends, the
    budget expires, cancellation is requested, or a failure under
    policy="abort" stops the slice. Returns a dict describing the slice
    outcome for the caller to fold into a lifecycle transition."""
    actions = plan.get("actions", [])
    ledger_records, _ = task_store.read_ledger(task_dir)
    done_indices = set()
    for rec in ledger_records:
        if rec.get("kind") == "action_end" and rec.get("status") in ("done", "skipped"):
            done_indices.add(rec.get("i"))

    # crash-window recovery: resolve any open action before starting new work
    open_action = task_store.find_open_action(task_dir)
    if isinstance(open_action, dict) and open_action.get("kind") == "corrupt":
        return {"outcome": "blocked", "review_reasons": ["ledger_corrupt"],
                "note": "more than one open action in the ledger"}
    if open_action is not None:
        decision, note, recovered_ids = _classify_open_action(doc, open_action)
        i = open_action.get("i")
        if decision == "block":
            return {"outcome": "blocked", "review_reasons": ["unknown_action_outcome"], "note": note}
        if decision == "skip":
            task_store.append_ledger(task_dir, {
                "kind": "action_end", "i": i, "status": "skipped",
                "execution_ok": None, "verified": {"ok": None, "status": "not_checked", "reason": note},
                "touched_ids": [], "touched_uids": [], "created_ids": recovered_ids or [],
                "cascaded_ids": [], "error": None,
            })
            done_indices.add(i)
        # decision == "run": fall through, the action below will re-run it

    remaining = [a for a in actions if a.get("i") not in done_indices]
    if not remaining:
        return {"outcome": "completed", "slice_atomicity": "assimilated", "actions_run": 0}

    slice_n = sum(1 for r in ledger_records if r.get("kind") == "slice_open") + 1
    group_name = "MCP Plan Slice: {} #{}".format(task_id, slice_n)
    tg = DB.TransactionGroup(doc, group_name)
    tg.Start()

    task_store.append_ledger(task_dir, {
        "kind": "slice_open", "n": slice_n, "group": group_name, "budget_ms": budget_ms,
    })

    slice_start = task_store.epoch_now()
    aborted_for_failure = False
    cancelled = False
    cancel_mode = None
    actions_run = 0

    try:
        for action in remaining:
            elapsed_ms = (task_store.epoch_now() - slice_start) * 1000.0
            if elapsed_ms >= budget_ms:
                break  # budget checked BEFORE starting each action, never mid-action

            if cancel_check_enabled:
                cancel_req = task_store.check_cancel(task_dir)
                if cancel_req is not None:
                    cancelled = True
                    cancel_mode = cancel_req.get("mode", "rollback_slice")
                    break

            i = action.get("i")
            op_name = action.get("op")
            spec = ops.OPS.get(op_name)
            targets = action.get("targets")
            args = action.get("args") or {}

            before = spec["capture"](doc, args, targets)

            inverse_now = None
            fidelity_now = None
            if spec["idempotency"] == "absolute":
                inverse_now = spec["inverse"](args, targets, before, None)
                fidelity_now = spec["fidelity"]

            begin_rec = {
                "kind": "action_begin", "i": i, "op": op_name, "phase": action.get("phase"),
                "targets": targets, "before": before,
            }
            if inverse_now is not None:
                begin_rec["inverse"] = inverse_now
                begin_rec["inverse_fidelity"] = fidelity_now
            task_store.append_ledger(task_dir, begin_rec)  # fail-stop: raises before any mutation

            task_store.append_ledger(task_dir, {"kind": "stage", "i": i, "stage": "applying"})

            action_start = task_store.epoch_now()
            t = DB.Transaction(doc, "MCP Plan Action: {}".format(op_name))
            t.Start()
            suppress_warnings(t)

            run_error = None
            result = None
            try:
                result = spec["run"](doc, args, targets, before)
            except Exception as run_err:
                run_error = str(run_err) + "\n" + traceback.format_exc()
                if t.HasStarted() and not t.HasEnded():
                    t.RollBack()

            if run_error is not None:
                task_store.append_ledger(task_dir, {
                    "kind": "action_end", "i": i, "status": "failed",
                    "tx_status": "RolledBack", "execution_ok": False,
                    "verified": {"ok": None, "status": "not_checked", "reason": "run() raised"},
                    "touched_ids": [], "touched_uids": [], "created_ids": [], "cascaded_ids": [],
                    "elapsed_ms": (task_store.epoch_now() - action_start) * 1000.0, "error": run_error,
                })
                actions_run += 1
                if policy.get("on_action_failure", "abort") == "abort":
                    aborted_for_failure = True
                    break
                continue

            task_store.append_ledger(task_dir, {"kind": "stage", "i": i, "stage": "committing"})
            tx_ok, tx_status = commit_verified(t)

            if tx_ok is False:
                task_store.append_ledger(task_dir, {
                    "kind": "action_end", "i": i, "status": "failed",
                    "tx_status": tx_status, "execution_ok": False,
                    "verified": {"ok": None, "status": "not_checked", "reason": "transaction did not commit"},
                    "touched_ids": [], "touched_uids": [], "created_ids": [], "cascaded_ids": [],
                    "elapsed_ms": (task_store.epoch_now() - action_start) * 1000.0, "error": None,
                })
                actions_run += 1
                if policy.get("on_action_failure", "abort") == "abort":
                    aborted_for_failure = True
                    break
                continue

            task_store.append_ledger(task_dir, {"kind": "stage", "i": i, "stage": "verifying"})
            try:
                verified = spec["verify"](doc, args, targets, before, result)
            except Exception as verify_err:
                verified = {"ok": None, "status": "not_checked",
                             "reason": "verify() raised: %s" % str(verify_err)}

            inverse_after = None
            fidelity_after = None
            if spec["idempotency"] == "creating":
                try:
                    inverse_after = spec["inverse"](args, targets, before, result)
                except Exception:
                    inverse_after = None
                fidelity_after = spec["fidelity"]

            task_store.append_ledger(task_dir, {"kind": "stage", "i": i, "stage": "recording"})

            verify_failed = (verified.get("ok") is False)
            status = "failed" if verify_failed else "done"

            end_rec = {
                "kind": "action_end", "i": i, "status": status,
                "tx_status": tx_status, "execution_ok": True, "verified": verified,
                "touched_ids": result.get("touched_ids", []), "touched_uids": result.get("touched_uids", []),
                "created_ids": result.get("created_ids", []), "cascaded_ids": result.get("cascaded_ids", []),
                "elapsed_ms": (task_store.epoch_now() - action_start) * 1000.0, "error": None,
            }
            if inverse_after is not None:
                end_rec["inverse"] = inverse_after
                end_rec["inverse_fidelity"] = fidelity_after
            task_store.append_ledger(task_dir, end_rec)
            actions_run += 1

            if verify_failed and policy.get("on_verify_failure", "abort") == "abort":
                aborted_for_failure = True
                break

        # --- close the slice ---
        if cancelled and cancel_mode == "rollback_slice":
            rb_status = tg.RollBack()
            slice_atomicity = "rolled_back"
        elif aborted_for_failure:
            rb_status = tg.RollBack()
            slice_atomicity = "rolled_back"
        else:
            a_status = tg.Assimilate()
            slice_atomicity = "assimilated" if str(a_status) == "Committed" else "assimilate_failed"

    except Exception:
        # leak guard: close the group on ANY unexpected exception, even one
        # this function's own logic did not anticipate.
        try:
            if tg.HasStarted() and not tg.HasEnded():
                tg.RollBack()
                slice_atomicity = "rolled_back"
            else:
                slice_atomicity = "group_leak"
        except Exception:
            slice_atomicity = "group_leak"
        task_store.append_ledger(task_dir, {
            "kind": "slice_close", "n": slice_n, "slice_atomicity": slice_atomicity,
            "elapsed_ms": (task_store.epoch_now() - slice_start) * 1000.0,
        })
        raise

    task_store.append_ledger(task_dir, {
        "kind": "slice_close", "n": slice_n, "slice_atomicity": slice_atomicity,
        "elapsed_ms": (task_store.epoch_now() - slice_start) * 1000.0,
    })

    if cancelled:
        return {"outcome": "cancelled", "slice_atomicity": slice_atomicity, "actions_run": actions_run}
    if slice_atomicity == "assimilate_failed":
        return {"outcome": "blocked", "review_reasons": ["assimilate_unknown"],
                "note": "Assimilate() did not return Committed", "actions_run": actions_run}
    if aborted_for_failure:
        return {"outcome": "failed", "slice_atomicity": slice_atomicity, "actions_run": actions_run}

    # did we finish the whole plan, or just run out of budget?
    ledger_records2, _ = task_store.read_ledger(task_dir)
    done_now = set(
        r.get("i") for r in ledger_records2
        if r.get("kind") == "action_end" and r.get("status") in ("done", "skipped")
    )
    if all(a.get("i") in done_now for a in actions):
        return {"outcome": "completed", "slice_atomicity": slice_atomicity, "actions_run": actions_run}
    return {"outcome": "paused", "slice_atomicity": slice_atomicity, "actions_run": actions_run}


def register_plan_routes(api):
    """Register plan-execution routes with the API."""

    @api.route("/preflight_plan/", methods=["POST"])
    def preflight_plan_handler(doc, request):
        """Advisory preflight, read-only, opens no transaction. Called by
        approve_plan (CPython, tools/plan_tools.py) BEFORE approval.json
        exists -- returns current canon/ops_version/boot_id for approve_plan
        to stamp into the approval, plus whatever preflight() can check
        with no prior baseline (ops_exist, refs_resolve). Also safe to call
        again later purely for an up-to-date advisory read."""
        try:
            if not doc:
                return routes.make_response(data={"error": "No active Revit document"}, status=503)
            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)
            task_id = data.get("task_id")
            if not task_id:
                return routes.make_response(data={"error": "task_id is required"}, status=400)

            task_dir = task_store.task_dir_path(task_id)
            plan, _ = task_store.read_json_with_fallback(task_store.plan_path(task_dir))
            if plan is None:
                return routes.make_response(data={"error": "plan.json not found or unreadable for task %s" % task_id}, status=404)
            approval, _ = task_store.read_json_with_fallback(task_store.approval_path(task_dir))

            pf = preflight(doc, plan, approval)
            return routes.make_response(data={
                "status": "success", "task_id": task_id, "ok": pf["ok"],
                "failures": pf["failures"], "checked": pf["checked"],
                "canon": pf["canon"], "ops_version": pf["ops_version"], "boot_id": pf["boot_id"],
            })
        except Exception as e:
            return routes.make_response(data={"error": str(e), "traceback": traceback.format_exc()}, status=500)

    @api.route("/execute_plan/", methods=["POST"])
    def execute_plan_handler(doc, request):
        try:
            if not doc:
                return routes.make_response(data={"error": "No active Revit document"}, status=503)

            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)

            task_id = data.get("task_id")
            if not task_id:
                return routes.make_response(data={"error": "task_id is required"}, status=400)
            resume = bool(data.get("resume", False))
            budget_ms = min(float(data.get("budget_ms", DEFAULT_SLICE_BUDGET_MS)), MAX_SLICE_BUDGET_MS)

            task_dir = task_store.task_dir_path(task_id)
            plan, plan_src = task_store.read_json_with_fallback(task_store.plan_path(task_dir))
            approval, approval_src = task_store.read_json_with_fallback(task_store.approval_path(task_dir))
            if plan is None:
                return routes.make_response(data={"error": "plan.json not found or unreadable for task %s" % task_id}, status=404)
            if approval is None:
                return routes.make_response(data={"error": "task %s has not been approved" % task_id}, status=409)

            lifecycle, outcome, review_reasons, progress = task_store.current_state(task_dir)

            if lifecycle == "finished":
                return routes.make_response(data={
                    "status": "success", "task_id": task_id, "lifecycle": lifecycle, "outcome": outcome,
                    "message": "Task already finished (outcome=%s) - nothing to execute." % outcome,
                    "progress": task_store.summarize_for_response(progress),
                })

            if lifecycle == "blocked" and not resume:
                return routes.make_response(data={
                    "status": "success", "task_id": task_id, "lifecycle": lifecycle,
                    "review_reasons": sorted(review_reasons),
                    "message": "Task is blocked and requires an explicit resume=true to continue.",
                    "progress": task_store.summarize_for_response(progress),
                })

            if lifecycle not in ("approved", "paused", "blocked", "running"):
                return routes.make_response(data={
                    "error": "Task %s is in lifecycle %r - execute_plan requires approved/paused/blocked/running."
                             % (task_id, lifecycle),
                }, status=409)

            lock_path, lock_rec = task_store.acquire_task_lock(task_dir, task_store.new_task_id())
            if lock_path is None:
                age_s = task_store.epoch_now() - float(lock_rec.get("hb_epoch", 0))
                return routes.make_response(data={
                    "status": "locked_by", "task_id": task_id, "run": lock_rec.get("run"),
                    "lock_age_s": age_s,
                    "error": "Task is currently locked by run %s (%.1fs old) - do not re-issue; "
                             "call get_task_status instead." % (lock_rec.get("run"), age_s),
                }, status=409)

            try:
                if lifecycle in ("approved", "paused", "blocked"):
                    task_store.transition(task_dir, "running",
                                           "resume" if lifecycle != "approved" else "first slice start")

                boot_id = _get_boot_id()
                approved_boot = approval.get("boot_id")
                if approved_boot and boot_id and approved_boot != boot_id and not resume:
                    task_store.transition(task_dir, "blocked", "revit restarted since approval",
                                           review_reasons=["revit_restarted"])
                    _, _, review_reasons2, progress2 = task_store.current_state(task_dir)
                    return routes.make_response(data={
                        "status": "success", "task_id": task_id, "lifecycle": "blocked",
                        "review_reasons": sorted(review_reasons2),
                        "message": "Revit has restarted since this task's boot_id was recorded. "
                                   "Attribution for any gap is unavailable - resume only with resume=true.",
                        "progress": task_store.summarize_for_response(progress2),
                    })

                pf = preflight(doc, plan, approval)
                task_store.append_ledger(task_dir, {"kind": "preflight", "ok": pf["ok"],
                                                      "failures": pf["failures"], "checked": pf["checked"]})
                if not pf["ok"]:
                    task_store.transition(task_dir, "blocked", "preflight failed",
                                           review_reasons=["precondition_failed"])
                    _, _, review_reasons3, progress3 = task_store.current_state(task_dir)
                    return routes.make_response(data={
                        "status": "success", "task_id": task_id, "lifecycle": "blocked",
                        "review_reasons": sorted(review_reasons3), "preflight_failures": pf["failures"],
                        "message": "Preflight failed - the model no longer matches what this plan assumed.",
                        "progress": task_store.summarize_for_response(progress3),
                    })

                policy = plan.get("policy") or {"on_action_failure": "abort", "on_verify_failure": "abort"}
                slice_result = _run_slice(doc, task_id, task_dir, plan, policy, budget_ms)

                if slice_result["outcome"] == "completed":
                    # success_criteria describe the PLAN's end state, not one
                    # budgeted slice of it -- evaluated exactly once, here,
                    # after every action has committed and this slice's
                    # TransactionGroup has already Assimilate()d, so `doc` is
                    # the fully-regenerated, final committed state, the same
                    # assumption every other post-commit verify() in this
                    # codebase already makes. Read-only, no transaction (see
                    # tests/test_evaluators_no_transactions.py). Written
                    # BEFORE the "finished" transition below so design_ok is
                    # already foldable in this same response. A "completed"
                    # outcome with design_ok False is a legitimate, disclosed
                    # terminal state -- this does not escalate to "failed";
                    # execution/verification/design stay orthogonal.
                    #
                    # Phase B slice 2: created_ids_by_action reads the SAME
                    # ledger records _run_slice just finished appending, so
                    # a "created_by" criterion can reference an element THIS
                    # slice's own actions created.
                    final_ledger_records, _ = task_store.read_ledger(task_dir)
                    created_by_action = task_store.created_ids_by_action(final_ledger_records)
                    criteria_results = evaluators.evaluate_success_criteria(doc, plan, created_by_action)
                    for cr in criteria_results:
                        task_store.append_ledger(task_dir, dict(cr, kind="criterion"))

                outcome_map = {"completed": "completed", "failed": "failed", "cancelled": "aborted"}
                if slice_result["outcome"] in outcome_map:
                    task_store.transition(task_dir, "finished", "slice result: %s" % slice_result["outcome"],
                                           outcome=outcome_map[slice_result["outcome"]])
                    task_store.write_json_atomic(task_store.result_path(task_dir), {
                        "task_id": task_id, "outcome": outcome_map[slice_result["outcome"]],
                        "slice_atomicity": slice_result.get("slice_atomicity"),
                        "at": task_store.now_iso(),
                    })
                elif slice_result["outcome"] == "blocked":
                    task_store.transition(task_dir, "blocked", slice_result.get("note", "blocked"),
                                           review_reasons=slice_result.get("review_reasons") or [])
                elif slice_result["outcome"] == "paused":
                    task_store.transition(task_dir, "paused", "slice budget expired")

                _, _, final_reasons, final_progress = task_store.current_state(task_dir)
                return routes.make_response(data={
                    "status": "success", "task_id": task_id,
                    "lifecycle": final_progress.get("lifecycle"), "outcome": final_progress.get("outcome"),
                    "review_reasons": sorted(final_reasons),
                    "slice_atomicity": slice_result.get("slice_atomicity"),
                    "actions_run_this_slice": slice_result.get("actions_run", 0),
                    "message": "execute_plan slice result: %s" % slice_result["outcome"],
                    "progress": task_store.summarize_for_response(final_progress),
                })

            finally:
                task_store.release_task_lock(lock_path)

        except Exception as e:
            logger.error("execute_plan failed: {}".format(str(e)))
            return routes.make_response(
                data={"error": str(e), "traceback": traceback.format_exc()}, status=500
            )

    @api.route("/cancel_task/", methods=["POST"])
    def cancel_task_handler(doc, request):
        """File-flag cancellation -- works even while a slice is running on
        Revit's UI thread, since it is only a file write, and is honoured
        by the executor ONLY at the between-actions boundary (an action
        already inside a Revit API call cannot be safely interrupted)."""
        try:
            data = json.loads(request.data) if isinstance(request.data, str) else request.data
            data = repair_hebrew_in(data)
            task_id = data.get("task_id")
            if not task_id:
                return routes.make_response(data={"error": "task_id is required"}, status=400)
            mode = data.get("mode", "rollback_slice")
            reason = data.get("reason")
            task_dir = task_store.task_dir_path(task_id)
            task_store.request_cancel(task_dir, mode=mode, reason=reason)
            return routes.make_response(data={
                "status": "success", "task_id": task_id, "mode": mode,
                "message": "Cancellation requested - honoured at the next between-actions boundary, "
                           "not mid-action.",
            })
        except Exception as e:
            return routes.make_response(data={"error": str(e), "traceback": traceback.format_exc()}, status=500)

    logger.info("Plan execution routes registered successfully")
