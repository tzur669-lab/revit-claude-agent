# -*- coding: utf-8 -*-
"""
The 6 Phase-A plan/task tools: propose_plan, approve_plan, execute_plan,
get_task_status, cancel_task, rollback_task.

Three of these (propose_plan, get_task_status, rollback_task) are
filesystem-only -- they read/write task files under
~/.claude/revit-tracker/tasks/<task_id>/ directly via revit_mcp.task_store
and never call Revit at all; that module has zero Revit-API dependency
(see its own docstring) and is importable here exactly as it is from the
IronPython side. approve_plan, execute_plan, and cancel_task each call
exactly one new Revit route (/preflight_plan/, /execute_plan/,
/cancel_task/ respectively) -- a clean 1:1 mapping, not the N:M the
project's registry drift tests were written to tolerate as an edge case.
"""
import asyncio
import json
import os

from mcp.server.fastmcp import Context

from .utils import format_response
from . import plan_schema, ops_schema
from revit_mcp import task_store


def _check_unexpected_changes(task_dir, plan, progress):
    """(a) the executor already recorded touched_uids for free; (b) Claude
    runs the tracker checkpoint (via the revit-session skill, NOT this
    tool -- see revision-4 ADR for why the executor must never invoke the
    tracker itself: TrackerOK propagation, destructive drain_events(),
    budget cost, a LOCKED_BY race indistinguishable from real failure);
    (c) this function does the comparison, purely from files already on
    disk -- no Revit, no lock."""
    lib_dir = os.environ.get(
        "REVIT_TRACKER_DIR", os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker")
    )
    last_run_path = os.path.join(lib_dir, "last_run.json")
    if not os.path.isfile(last_run_path):
        return {"status": "unknown", "reason": "no tracker last_run.json found - run a checkpoint first"}
    try:
        with open(last_run_path, "r", encoding="utf-8") as f:
            last_run = json.load(f)
    except Exception as e:
        return {"status": "unknown", "reason": "could not read last_run.json: {}".format(e)}

    baseline_run = progress.get("baseline_run")
    current_run = last_run.get("run")
    if baseline_run and current_run == baseline_run:
        return {
            "status": "no_checkpoint_since_baseline", "baseline_run": baseline_run,
            "reason": "no checkpoint has run since this task's baseline - comparing now would "
                      "silently include work from before this task started",
        }

    delta_file = last_run.get("delta_file")
    if not delta_file or not os.path.isfile(delta_file):
        return {
            "status": "no_delta",
            "reason": "no delta file after a mutating task is itself a finding, not a success - "
                      "either nothing changed, or the checkpoint ran against the wrong baseline",
        }
    try:
        with open(delta_file, "r", encoding="utf-8") as f:
            delta = json.load(f)
    except Exception as e:
        return {"status": "unknown", "reason": "could not read delta file: {}".format(e)}

    expected_changes = (plan or {}).get("expected_changes") or {}
    tolerated = set(expected_changes.get("tolerated_side_effects") or [])
    expected_ids = set()
    for key in ("modified", "created", "deleted"):
        expected_ids.update(expected_changes.get(key) or [])

    observed_uids = set()
    for a in progress.get("actions", []):
        observed_uids.update(a.get("touched_uids") or [])

    attribution = delta.get("attribution") or {}
    items = delta.get("items") or []  # the UNCAPPED delta file, never last_run.json's capped items

    accounted, side_effect, foreign, unexpected = [], [], [], []
    for item in items:
        uid = item.get("uid")
        try:
            iid = int(item.get("id"))
        except (TypeError, ValueError):
            iid = None
        by = attribution.get(uid)
        if uid in observed_uids or (iid is not None and iid in expected_ids):
            accounted.append(item)
        elif item.get("cat") in tolerated:
            side_effect.append(item)
        elif by in ("human", "sync_incoming"):
            # Positive evidence someone else caused this - genuinely foreign.
            # "unknown" is a DIFFERENT epistemic state (no covering event
            # found at all, e.g. a derived geometry recompute the tracker's
            # listener never sees as its own named transaction - measured
            # live 2026-09-06: a room's boundary shifted as a side effect of
            # our own wall move, attribution="unknown", NOT "human") and
            # must never be treated as confident non-attribution - the
            # tracker's own rule is "unknown must never be upgraded to
            # human," and collapsing it into "foreign" here would be exactly
            # that mistake one layer up. An uncertain or "claude"-but-
            # undeclared change both fall through to "unexpected" instead -
            # the honest, cautious default.
            foreign.append(item)
        else:
            unexpected.append(item)

    return {
        "status": "compared", "baseline_run": baseline_run, "current_run": current_run,
        "truncated": bool(last_run.get("items_capped")),
        "accounted_count": len(accounted), "side_effect_count": len(side_effect),
        "foreign_count": len(foreign), "unexpected_count": len(unexpected),
        "foreign": foreign[:50], "unexpected": unexpected[:50],
    }


def register_plan_tools(mcp, revit_get, revit_post, revit_image=None):
    """Register the plan/task tools with the MCP server."""
    _ = revit_get, revit_image  # unused, kept for interface consistency with every other register_*_tools

    @mcp.tool()
    async def propose_plan(
        objective: str,
        actions: list,
        constraints: list = None,
        assumptions: list = None,
        phases: list = None,
        expected_changes: dict = None,
        success_criteria: list = None,
        risks: list = None,
        policy: dict = None,
        confidence: dict = None,
        reasoning: dict = None,
        scope: dict = None,
        revision_of: str = None,
        derived_from: dict = None,
        ctx: Context = None,
    ) -> str:
        """
        Propose a structured, machine-validated plan for a coordinated
        Revit change. Validates and writes plan.json to disk; does NOT
        touch the Revit model. The plan must be approved (approve_plan)
        before execute_plan can run it, and every action must reference an
        op from the Phase-A v1 set (move_elements, set_parameters,
        set_wall_curve, delete_elements, create_room,
        create_room_separation, create_line_based_element, place_family).

        Each action needs: i (int index), op, targets ({"kind":"element",
        "ids":[...]}), args (per-op, see docs/operation-contracts.md),
        optionally phase, intent, reversible, needs (dependency indices for
        compensation ordering).

        success_criteria entries use evaluator (room_area_sqm, param_value,
        element_exists, element_absent, distance_mm), target, op
        (>=/<=/==/!=), value.

        Returns the plan's reversibility summary BEFORE approval - review
        which actions (if any) are irreversible before approving.
        """
        plan = {
            "schema_v": plan_schema.CURRENT_SCHEMA_V,
            "task_id": None,
            "created": task_store.now_iso(),
            "objective": objective,
            "scope": scope or {},
            "constraints": constraints or [],
            "assumptions": assumptions or [],
            "phases": phases or [],
            "actions": actions or [],
            "expected_changes": expected_changes or {
                "modified": [], "created": [], "deleted": [], "tolerated_side_effects": [],
            },
            "success_criteria": success_criteria or [],
            "risks": risks or [],
            "policy": policy or {"on_action_failure": "abort", "on_verify_failure": "abort"},
            "confidence": confidence or {"level": "medium", "why": ""},
            "reasoning": reasoning or {},
        }
        if revision_of is not None:
            plan["revision_of"] = revision_of
        if derived_from is not None:
            plan["derived_from"] = derived_from

        task_id = task_store.new_task_id()
        plan["task_id"] = task_id

        ok, errors = plan_schema.validate_plan(plan)
        if not ok:
            return format_response({
                "status": "error", "error": "Plan validation failed - not written to disk",
                "validation_errors": errors,
            })

        ph = plan_schema.plan_hash(plan)

        # Reversibility summary, before approval -- never overstate what
        # rollback_task could later restore.
        irreversible = []
        fidelity_order = ["exact", "approximate", "compensatable", "irreversible", "unknown"]
        worst_rank = 0
        for a in plan["actions"]:
            spec = ops_schema.OPS.get(a.get("op"))
            if spec is None:
                continue
            fid = spec["fidelity"]
            if fid == "irreversible":
                irreversible.append({"i": a.get("i"), "op": a.get("op")})
            worst_rank = max(worst_rank, fidelity_order.index(fid))
        worst = fidelity_order[worst_rank] if plan["actions"] else "exact"
        if worst in ("exact", "approximate"):
            reversibility_level = "full"
        elif irreversible and len(irreversible) == len(plan["actions"]):
            reversibility_level = "none"
        else:
            reversibility_level = "partial"

        # Baseline for future unexpected-change detection: the tracker's
        # last_run.json is a local file on this same machine - no Revit
        # round-trip needed to read it.
        baseline_run = None
        lib_dir = os.environ.get(
            "REVIT_TRACKER_DIR", os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker")
        )
        last_run_path = os.path.join(lib_dir, "last_run.json")
        if os.path.isfile(last_run_path):
            try:
                with open(last_run_path, "r", encoding="utf-8") as f:
                    baseline_run = json.load(f).get("run")
            except Exception:
                baseline_run = None

        task_dir = task_store.init_task(task_id, ph, boot_id=None, baseline_run=baseline_run, canon=None)
        task_store.write_json_atomic(task_store.plan_path(task_dir), plan)

        if ctx:
            await ctx.info("propose_plan {}: {} action(s), reversibility={}".format(
                task_id, len(plan["actions"]), reversibility_level))

        return format_response({
            "status": "success", "task_id": task_id, "plan_hash": ph,
            "objective": objective, "action_count": len(plan["actions"]),
            "reversibility": {"level": reversibility_level, "irreversible_actions": irreversible},
            "policy": plan["policy"],
            "message": "Plan {} proposed with {} action(s). Reversibility: {}. "
                       "Call approve_plan to run preflight and approve.".format(
                           task_id, len(plan["actions"]), reversibility_level),
        })

    @mcp.tool()
    async def approve_plan(
        task_id: str,
        decision: str = "approve",
        plan_hash: str = None,
        note: str = None,
        ctx: Context = None,
    ) -> str:
        """
        Approve, reject, or request changes to a proposed plan.

        decision: "approve" | "approve_with_modifications" | "reject" |
        "request_changes". For approve_with_modifications, propose a NEW
        plan first (propose_plan with derived_from={"task_id": task_id,
        "relation": "revision"}) and approve that one - an approval is
        always bound to one exact, immutable plan.

        Pass plan_hash (from propose_plan's response) to confirm you are
        approving the exact plan you reviewed - a mismatch is refused
        rather than silently approving something else.

        Runs an advisory preflight against the live Revit document and
        stamps the current document identity / ops.py version into the
        approval, so a later material change (the document swapped, or
        ops.py was edited) can be detected before execution - not to
        forbid an ordinary save, which never voids approval.
        """
        task_dir = task_store.task_dir_path(task_id)
        plan, _ = task_store.read_json_with_fallback(task_store.plan_path(task_dir))
        if plan is None:
            return format_response({"status": "error", "error": "plan.json not found for task {}".format(task_id)})

        ph = task_store.plan_hash(plan)
        if plan_hash is not None and plan_hash != ph:
            return format_response({
                "status": "error",
                "error": "plan_hash mismatch - you are approving a different plan than you reviewed",
                "expected": plan_hash, "actual": ph,
            })

        if decision in ("reject", "request_changes"):
            try:
                task_store.transition(task_dir, "finished", "user decision: {}".format(decision), outcome="rejected")
            except task_store.IllegalTransition as e:
                return format_response({"status": "error", "error": str(e)})
            return format_response({
                "status": "success", "task_id": task_id, "decision": decision,
                "message": "Plan {} was {}.".format(task_id, decision),
            })

        if decision not in ("approve", "approve_with_modifications"):
            return format_response({
                "status": "error",
                "error": "decision must be one of approve/approve_with_modifications/reject/request_changes",
            })

        response = await revit_post("/preflight_plan/", {"task_id": task_id}, ctx)
        if isinstance(response, str):
            return format_response({"status": "error", "error": "preflight_plan call failed: {}".format(response)})
        if response.get("_http_status") not in (None, 200):
            return format_response(response)

        approval = {
            "task_id": task_id, "plan_hash": ph, "schema_v": plan.get("schema_v"),
            "decision": decision, "note": note, "at": task_store.now_iso(),
            "canon": response.get("canon"), "ops_version": response.get("ops_version"),
            "boot_id": response.get("boot_id"),
            "preflight_advisory": {
                "ok": response.get("ok"), "failures": response.get("failures"), "checked": response.get("checked"),
            },
        }
        task_store.write_json_atomic(task_store.approval_path(task_dir), approval)
        try:
            task_store.transition(task_dir, "approved", "user decision: {}".format(decision))
        except task_store.IllegalTransition as e:
            return format_response({"status": "error", "error": str(e)})

        if ctx:
            await ctx.info("approve_plan {}: decision={} preflight_ok={}".format(
                task_id, decision, response.get("ok")))

        warn = "" if response.get("ok") else " WARNING: advisory preflight found issues - review before executing."
        return format_response({
            "status": "success", "task_id": task_id, "plan_hash": ph, "decision": decision,
            "preflight_ok": response.get("ok"), "preflight_failures": response.get("failures"),
            "message": "Plan {} approved.{}".format(task_id, warn),
        })

    @mcp.tool()
    async def execute_plan(
        task_id: str,
        budget_ms: float = 20000.0,
        resume: bool = False,
        ctx: Context = None,
    ) -> str:
        """
        Execute one budgeted slice of an approved plan as one coordinated
        Revit TransactionGroup. Call again to resume if the task pauses
        (budget expired with actions remaining) or after resolving a
        'blocked' review reason (pass resume=True).

        Reports live progress via MCP progress notifications where the
        client supports them; the durable, authoritative state always
        comes from progress.json on disk, which is what this tool reads
        and returns even if the underlying HTTP call itself times out -
        a timeout is never proof the work did not happen.
        """
        payload = {"task_id": task_id, "budget_ms": budget_ms, "resume": resume}
        call = asyncio.ensure_future(revit_post("/execute_plan/", payload, ctx))

        task_dir = task_store.task_dir_path(task_id)
        while True:
            done, _pending = await asyncio.wait({call}, timeout=1.0)
            if call in done:
                break
            if ctx:
                try:
                    progress, _meta = task_store.read_progress(task_dir)
                    counts = progress.get("counts", {})
                    actions = progress.get("actions") or []
                    stage = actions[-1].get("stage", "") if actions else ""
                    await ctx.report_progress(
                        counts.get("done", 0), counts.get("total", 0),
                        "lifecycle={} - {}/{} - {}".format(
                            progress.get("lifecycle"), counts.get("done", 0), counts.get("total", 0), stage,
                        ),
                    )
                except Exception:
                    pass  # progress reporting is fail-soft - never affects execution

        response = call.result()

        if isinstance(response, dict) and response.get("error_kind") == "client_timeout":
            progress, meta = task_store.read_progress(task_dir)
            counts = progress.get("counts", {})
            return format_response({
                "status": "error",
                "message": (
                    "execute_plan timed out after {}s. Revit may still be working. Last recorded "
                    "progress (written by Revit itself, read_from={}): {}/{} actions done, "
                    "lifecycle={}. Do NOT re-issue execute_plan yet - the task lock may still be "
                    "held. Call get_task_status first; resume only once lifecycle is "
                    "paused/blocked/finished."
                ).format(response.get("timeout_s"), meta.get("read_from"),
                          counts.get("done", 0), counts.get("total", 0), progress.get("lifecycle")),
                "progress": task_store.summarize_for_response(progress),
            })

        return format_response(response)

    @mcp.tool()
    async def get_task_status(
        task_id: str,
        check_unexpected: bool = False,
        ctx: Context = None,
    ) -> str:
        """
        Read a task's current state from disk - NEVER touches Revit, so it
        works even while a slice is running on Revit's UI thread or right
        after a client timeout. This is the recovery path: the timeout
        becomes a read, and the read is authoritative because its writer
        is the thing that timed out.

        check_unexpected=True additionally classifies the tracker's most
        recent checkpoint delta (accounted / side_effect / foreign /
        unexpected) against this task's expected_changes - requires that a
        checkpoint has run (via the revit-session skill) since this task's
        baseline; otherwise reports why the comparison could not be made.
        """
        task_dir = task_store.task_dir_path(task_id)
        if not os.path.isdir(task_dir):
            return format_response({"status": "error", "error": "no such task: {}".format(task_id)})

        lifecycle, outcome, review_reasons, progress = task_store.current_state(task_dir)
        plan, _ = task_store.read_json_with_fallback(task_store.plan_path(task_dir))

        out = {
            "status": "success", "task_id": task_id,
            "lifecycle": lifecycle, "outcome": outcome, "review_reasons": sorted(review_reasons),
            "objective": (plan or {}).get("objective"),
            "progress": task_store.summarize_for_response(progress),
        }
        if check_unexpected:
            out["unexpected_changes"] = _check_unexpected_changes(task_dir, plan, progress)

        return format_response(out)

    @mcp.tool()
    async def cancel_task(
        task_id: str,
        mode: str = "rollback_slice",
        reason: str = None,
        ctx: Context = None,
    ) -> str:
        """
        Request cancellation of a running/paused task. Honoured ONLY at
        the next between-actions boundary - an action already inside a
        Revit API call cannot be safely interrupted.

        mode="rollback_slice" (default): discards the in-flight slice via
        TransactionGroup.RollBack() - earlier committed slices are
        untouched. mode="keep_slice": Assimilates whatever has already
        committed in the current slice, then stops.
        """
        payload = {"task_id": task_id, "mode": mode, "reason": reason}
        response = await revit_post("/cancel_task/", payload, ctx)
        return format_response(response)

    @mcp.tool()
    async def rollback_task(
        task_id: str,
        ctx: Context = None,
    ) -> str:
        """
        Build a compensation PLAN from a finished task's recorded inverses.
        Does NOT execute anything - propose the returned suggested_plan via
        propose_plan, then approve_plan/execute_plan, exactly like any
        other plan. Compensation order is reverse-topological over each
        action's 'needs', falling back to plain reverse order when none
        were declared.

        Reports restored: "actions" (every recorded inverse), never
        restored: "model" - compensation cannot resurrect deleted elements
        or reverse Revit's own regeneration side effects (the measured
        case: moving a wall back does not restore the room-boundary shift
        that wall's first move caused).
        """
        task_dir = task_store.task_dir_path(task_id)
        if not os.path.isdir(task_dir):
            return format_response({"status": "error", "error": "no such task: {}".format(task_id)})

        lifecycle, outcome, _reasons, _progress = task_store.current_state(task_dir)
        if lifecycle != "finished":
            return format_response({
                "status": "error",
                "error": "task {} is not finished (lifecycle={}) - nothing to compensate yet".format(
                    task_id, lifecycle),
            })

        records, _ = task_store.read_ledger(task_dir)
        inverses_by_i = {}
        for rec in records:
            if rec.get("kind") in ("action_begin", "action_end") and rec.get("inverse") is not None:
                inverses_by_i[rec.get("i")] = rec.get("inverse")

        plan, _ = task_store.read_json_with_fallback(task_store.plan_path(task_dir))
        actions = (plan or {}).get("actions", [])
        order = plan_schema.compensation_order(actions)
        irreversible = [a.get("i") for a in actions if a.get("i") not in inverses_by_i]

        new_actions = []
        for idx, i in enumerate(order):
            inv = inverses_by_i.get(i)
            if inv is None:
                continue
            new_actions.append({
                "i": idx, "phase": 0, "op": inv["op"], "targets": inv["targets"], "args": inv["args"],
                "intent": "compensate action {} of task {}".format(i, task_id),
            })

        if not new_actions:
            return format_response({
                "status": "error",
                "error": "no compensable actions recorded for task {} - every action was irreversible".format(task_id),
                "irreversible_action_indices": irreversible,
            })

        suggested_plan = {
            "objective": "Compensate task {}: {}".format(task_id, (plan or {}).get("objective", "")),
            "actions": new_actions,
            "policy": {"on_action_failure": "abort", "on_verify_failure": "abort"},
            "expected_changes": {"modified": [], "created": [], "deleted": [], "tolerated_side_effects": []},
            "success_criteria": [],
            "risks": [{
                "statement": "Compensation restores recorded actions, NOT the full model state - "
                             "Revit's own regeneration side effects (e.g. a moved wall's room "
                             "boundary) are not reversed.",
                "severity": "medium",
            }],
            "confidence": {"level": "medium", "why": "derived mechanically from recorded inverses"},
            "reasoning": {"selected": "compensation", "why": "reverse of task {}".format(task_id)},
            "derived_from": {"task_id": task_id, "relation": "compensation"},
        }

        if ctx:
            await ctx.info("rollback_task {}: {} of {} action(s) compensable".format(
                task_id, len(new_actions), len(actions)))

        return format_response({
            "status": "success", "original_task_id": task_id,
            "compensable_action_count": len(new_actions), "irreversible_action_indices": irreversible,
            "restored": "actions",
            "message": (
                "Built a compensation plan for {} of {} action(s) from task {}. {} action(s) are "
                "irreversible and cannot be compensated. Call propose_plan with suggested_plan, then "
                "approve_plan and execute_plan - compensation goes through the same approval gate as "
                "any other plan."
            ).format(len(new_actions), len(actions), task_id, len(irreversible)),
            "suggested_plan": suggested_plan,
        })
