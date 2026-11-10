# -*- coding: utf-8 -*-
"""
Plan validation and identity -- CPython side, zero Revit involvement. A
malformed plan is rejected in milliseconds and never occupies Revit's UI
thread. This validates the DOCUMENT (the JSON is internally consistent);
Revit-side preflight (plan_exec.py, built later) validates the WORLD --
that every ref resolves right now and matches what propose_plan captured.
"CPython validates the document; Revit validates the world."

plan_hash() is intentionally NOT reimplemented here. revit_mcp/task_store.py
has zero Revit-API dependency and is already importable from both runtimes
(see that module's docstring), so there is exactly one plan_hash()
implementation, imported here and by revit_mcp/plan_exec.py alike -- no
cross-runtime drift is possible because there is nothing to drift apart.
"""
from revit_mcp.task_store import plan_hash as _plan_hash, MATERIAL_PLAN_KEYS  # noqa: F401

from . import ops_schema

CURRENT_SCHEMA_V = 1

TOP_LEVEL_REQUIRED = frozenset([
    "schema_v", "task_id", "created", "objective", "constraints", "assumptions",
    "phases", "actions", "expected_changes", "success_criteria", "risks",
    "policy", "confidence", "reasoning",
])
# scope, revision_of, derived_from are optional. plan_hash itself is
# computed by the caller, never authored inside the plan document.
TOP_LEVEL_OPTIONAL = frozenset(["scope", "revision_of", "derived_from"])
TOP_LEVEL_ALLOWED = TOP_LEVEL_REQUIRED | TOP_LEVEL_OPTIONAL

REF_KINDS = ops_schema.REF_KINDS
CRITERION_OPS = frozenset([">=", "<=", "==", "!="])
POLICY_VALUES = frozenset(["abort", "continue"])
POLICY_KEYS = frozenset(["on_action_failure", "on_verify_failure"])


def validate_plan(plan):
    """(ok: bool, errors: [str]). Never raises for a malformed plan -- a
    validator returning structured errors is the whole point. Raises only
    for a genuine programming error (plan not even a dict)."""
    if not isinstance(plan, dict):
        return False, ["plan must be a JSON object"]

    errors = []

    unknown = set(plan.keys()) - TOP_LEVEL_ALLOWED
    if unknown:
        errors.append(
            "unknown top-level key(s): %s -- reject rather than silently "
            "hash-including or silently ignoring them (an approval must "
            "never authorize a plan shape it never saw)" % sorted(unknown)
        )

    missing = TOP_LEVEL_REQUIRED - set(plan.keys())
    if missing:
        errors.append("missing required top-level key(s): %s" % sorted(missing))

    if plan.get("schema_v") != CURRENT_SCHEMA_V:
        errors.append("schema_v must be %r, got %r" % (CURRENT_SCHEMA_V, plan.get("schema_v")))

    actions = plan.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("actions must be a non-empty list")
        actions = []
    errors.extend(_validate_actions(actions))

    cycle = find_needs_cycle(actions)
    if cycle:
        errors.append("actions form a 'needs' dependency cycle: %s" % (cycle,))

    errors.extend(_validate_success_criteria(plan.get("success_criteria"), actions))
    errors.extend(_validate_policy(plan.get("policy")))
    errors.extend(_validate_expected_changes(plan.get("expected_changes")))

    return (len(errors) == 0), errors


def _validate_actions(actions):
    errors = []
    seen_i = set()
    known_i = set(a.get("i") for a in actions if isinstance(a, dict))

    for idx, a in enumerate(actions):
        if not isinstance(a, dict):
            errors.append("actions[%d] is not an object" % idx)
            continue

        i = a.get("i")
        if i is None or i in seen_i:
            errors.append("actions[%d]: missing or duplicate 'i' (%r)" % (idx, i))
        seen_i.add(i)

        op = a.get("op")
        if op not in ops_schema.SUPPORTED_OPS:
            errors.append("actions[%d] (i=%r): unknown op %r (known: %s)"
                           % (idx, i, op, sorted(ops_schema.SUPPORTED_OPS)))
            continue
        op_spec = ops_schema.OPS[op]

        args = a.get("args")
        if not isinstance(args, dict):
            errors.append("actions[%d] (i=%r): args must be an object" % (idx, i))
            args = {}
        required_args = set(op_spec["args"]["required"])
        allowed_args = required_args | set(op_spec["args"]["optional"])
        missing_args = required_args - set(args.keys())
        if missing_args:
            errors.append("actions[%d] (i=%r): op %r missing required arg(s) %s"
                           % (idx, i, op, sorted(missing_args)))
        extra_args = set(args.keys()) - allowed_args
        if extra_args:
            errors.append("actions[%d] (i=%r): op %r has unexpected arg(s) %s"
                           % (idx, i, op, sorted(extra_args)))

        targets = a.get("targets")
        if not isinstance(targets, dict) or targets.get("kind") not in REF_KINDS:
            errors.append("actions[%d] (i=%r): targets.kind must be one of %s"
                           % (idx, i, sorted(REF_KINDS)))

        declared_reversible = a.get("reversible")
        expected_reversible = ops_schema.is_reversible(op)
        if declared_reversible is not None and bool(declared_reversible) != expected_reversible:
            errors.append(
                "actions[%d] (i=%r): reversible=%r disagrees with op %r's own "
                "fidelity (%r) -- reversible is DERIVED from the ops table, "
                "never declared independently"
                % (idx, i, declared_reversible, op, op_spec["fidelity"])
            )

        needs = a.get("needs") or []
        if not isinstance(needs, list):
            errors.append("actions[%d] (i=%r): needs must be a list" % (idx, i))
        else:
            for dep in needs:
                if dep not in known_i:
                    errors.append(
                        "actions[%d] (i=%r): needs references unknown action i=%r"
                        % (idx, i, dep)
                    )

    return errors


def _validate_single_ref(ref, idx, ev, by_i, label):
    """(count_or_None, errors) for ONE single-source reference -- either
    the singular `target` field, or one entry of the plural `targets`
    list. `count` is how many element ids this ref will resolve to at
    evaluation time: an "element" ref contributes len(ids); a
    "created_by" ref always contributes exactly 1 once its own shape is
    valid (one index into one action's output). None means the ref's own
    shape was already invalid -- nothing further should be counted
    against it, matching how a malformed target already skips the
    cardinality check below rather than compounding one error into two.

    "created_by".action_i must reference a KNOWN action whose op is
    idempotency=="creating" -- referencing e.g. a move_elements action (which
    never creates anything) is rejected here, at propose time, not left to
    fail silently as invalid_reference at evaluation time."""
    errors = []
    if not isinstance(ref, dict) or ref.get("kind") not in ops_schema.CRITERION_TARGET_KINDS:
        errors.append("success_criteria[%d] (evaluator=%r): %s.kind must be one of %s"
                       % (idx, ev, label, sorted(ops_schema.CRITERION_TARGET_KINDS)))
        return None, errors

    kind = ref.get("kind")
    if kind == "element":
        ids = ref.get("ids")
        if not isinstance(ids, list) or not ids:
            errors.append("success_criteria[%d] (evaluator=%r): %s.ids must be a non-empty list"
                           % (idx, ev, label))
            return None, errors
        return len(ids), errors

    # kind == "created_by"
    action_i = ref.get("action_i")
    action = by_i.get(action_i)
    if action is None:
        errors.append("success_criteria[%d] (evaluator=%r): %s.action_i %r does not reference a "
                       "known action" % (idx, ev, label, action_i))
        return None, errors
    op_spec = ops_schema.OPS.get(action.get("op"))
    if op_spec is None or op_spec["idempotency"] != "creating":
        errors.append(
            "success_criteria[%d] (evaluator=%r): %s.action_i %r's op %r does not create "
            "anything (idempotency must be 'creating') -- created_by can only reference a "
            "create_room/create_room_separation/create_line_based_element/place_family action"
            % (idx, ev, label, action_i, action.get("op"))
        )
        return None, errors
    index = ref.get("index", 0)
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        errors.append("success_criteria[%d] (evaluator=%r): %s.index must be a non-negative int, "
                       "got %r" % (idx, ev, label, index))
        return None, errors
    return 1, errors


def _validate_success_criteria(sc, actions):
    """Shape-only, zero Revit access (CPython validates the document; Revit
    validates the world -- see this module's own docstring). Beyond the
    evaluator/op/target checks that already existed, this also validates
    each evaluator's own arg contract (ops_schema.EVALUATOR_ARGS) and target
    id-count (ops_schema.EXACT_TARGET_COUNT) -- pure JSON-shape checks, no
    Revit call. Deliberately NOT applying validate_plan's top-level
    unknown-key rejection here: constraints/assumptions/risks already tolerate
    free-form descriptive fields (id, source, evidence, severity...)
    elsewhere in this same schema, and a criterion's own id/unit/tolerance/
    binding-style metadata gets the same leniency, not the plan's own
    stricter document-level discipline.

    Operator-vs-actual-value-type compatibility (e.g. ">=" against a String
    parameter) is NOT checked here -- CPython never sees the live
    parameter's StorageType at propose time. That check is deferred to
    revit_mcp.eval_support.compare() at evaluation time.

    `target` (a single ref, unchanged since slice 1 -- "element" with a
    batch of ids, or new: "created_by") and `targets` (plural, new: a list
    of single-source refs, for a composed reference like an existing
    element paired with one a plan action creates) are mutually exclusive
    -- exactly one must be present. `actions` (already normalized to a
    list by validate_plan before this is called) is needed only to
    cross-reference a `created_by` ref's `action_i`."""
    errors = []
    if not isinstance(sc, list):
        return ["success_criteria must be a list"]
    by_i = dict((a.get("i"), a) for a in actions if isinstance(a, dict) and "i" in a)

    for idx, c in enumerate(sc):
        if not isinstance(c, dict):
            errors.append("success_criteria[%d] is not an object" % idx)
            continue

        ev = c.get("evaluator")
        ev_spec = ops_schema.EVALUATOR_ARGS.get(ev)
        if ev_spec is None:
            errors.append(
                "success_criteria[%d]: unknown evaluator %r (known: %s)"
                % (idx, ev, sorted(ops_schema.EVALUATORS))
            )

        if c.get("op") not in CRITERION_OPS:
            errors.append("success_criteria[%d]: op must be one of %s"
                           % (idx, sorted(CRITERION_OPS)))

        has_target = "target" in c
        has_targets = "targets" in c
        target_count = None
        if has_target == has_targets:  # both present, or neither
            errors.append(
                "success_criteria[%d]: exactly one of 'target' or 'targets' must be present, got %s"
                % (idx, "both" if has_target else "neither")
            )
        elif has_target:
            target_count, ref_errors = _validate_single_ref(c.get("target"), idx, ev, by_i, "target")
            errors.extend(ref_errors)
        else:
            targets_val = c.get("targets")
            if not isinstance(targets_val, list) or not targets_val:
                errors.append("success_criteria[%d]: 'targets' must be a non-empty list" % idx)
            else:
                counts = []
                for j, ref in enumerate(targets_val):
                    cnt, ref_errors = _validate_single_ref(ref, idx, ev, by_i, "targets[%d]" % j)
                    errors.extend(ref_errors)
                    if cnt is not None and cnt != 1:
                        errors.append(
                            "success_criteria[%d]: targets[%d] must resolve to exactly 1 id -- a "
                            "batch 'element' ref belongs in the singular 'target' field, not "
                            "inside 'targets'" % (idx, j)
                        )
                    counts.append(cnt)
                if all(cnt is not None for cnt in counts):
                    target_count = len(targets_val)

        if ev_spec is None:
            continue  # nothing more to check against an unknown evaluator's own arg contract

        missing = set(ev_spec["required"]) - set(c.keys())
        if missing:
            errors.append("success_criteria[%d] (evaluator=%r): missing required arg(s) %s"
                           % (idx, ev, sorted(missing)))

        # room_area_sqm/distance_mm's "value" is always numeric -- the unit is
        # part of the evaluator's own name. param_value's "value" is NOT
        # required to be numeric: it is compared against whatever StorageType
        # the target parameter actually has (String/Integer/Double/ElementId),
        # which CPython cannot know at propose time -- see eval_support.compare.
        if ev in ("room_area_sqm", "distance_mm") and "value" in c:
            v = c.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append("success_criteria[%d] (evaluator=%r): 'value' must be a number, got %r"
                               % (idx, ev, v))

        if "parameter_name" in ev_spec["required"] and "parameter_name" in c:
            pn = c.get("parameter_name")
            if not isinstance(pn, str) or not pn:
                errors.append("success_criteria[%d] (evaluator=%r): 'parameter_name' must be a "
                               "non-empty string" % (idx, ev))

        if "allow_type_fallback" in c and not isinstance(c["allow_type_fallback"], bool):
            errors.append("success_criteria[%d] (evaluator=%r): 'allow_type_fallback' must be a bool"
                           % (idx, ev))

        exact_n = ops_schema.EXACT_TARGET_COUNT.get(ev)
        if exact_n is not None and target_count is not None and target_count != exact_n:
            errors.append(
                "success_criteria[%d] (evaluator=%r): target(s) must resolve to exactly %d id(s), "
                "got %d" % (idx, ev, exact_n, target_count)
            )

    return errors


def _validate_policy(policy):
    if not isinstance(policy, dict):
        return ["policy must be an object"]
    errors = []
    for key in POLICY_KEYS:
        if policy.get(key) not in POLICY_VALUES:
            errors.append("policy.%s must be one of %s, got %r"
                           % (key, sorted(POLICY_VALUES), policy.get(key)))
    return errors


def _validate_expected_changes(ec):
    if not isinstance(ec, dict):
        return ["expected_changes must be an object"]
    errors = []
    for key in ("modified", "created", "deleted"):
        if key in ec and not isinstance(ec[key], list):
            errors.append("expected_changes.%s must be a list of element ids" % key)
    if "tolerated_side_effects" in ec and not isinstance(ec["tolerated_side_effects"], list):
        errors.append("expected_changes.tolerated_side_effects must be a list of category names")
    return errors


def find_needs_cycle(actions):
    """DFS cycle detection over the 'needs' graph. Returns a list
    describing one cycle (a walkable path back to its start), or None.
    Used both for validation and to feed compensation_order()."""
    graph = {}
    for a in actions:
        if not isinstance(a, dict):
            continue
        i = a.get("i")
        if i is None:
            continue
        graph[i] = list(a.get("needs") or [])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict((k, WHITE) for k in graph)
    path = []

    def visit(node):
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue  # an unknown dep is reported separately by _validate_actions
            if color[dep] == GRAY:
                return path[path.index(dep):] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for node in list(graph.keys()):
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def compensation_order(actions):
    """Reverse-topological order over 'needs', for undoing a plan: if
    action A needs B (A runs after B), compensating A must happen BEFORE
    compensating B. With no 'needs' declared anywhere, this is exactly
    plain reverse declared order. Assumes validate_plan already proved
    there is no cycle -- callers must validate first."""
    by_i = dict((a["i"], a) for a in actions if isinstance(a, dict) and "i" in a)
    visited = set()
    topo = []

    def visit(i):
        if i in visited or i not in by_i:
            return
        visited.add(i)
        for dep in (by_i[i].get("needs") or []):
            visit(dep)
        topo.append(i)

    for a in actions:
        if isinstance(a, dict) and "i" in a:
            visit(a["i"])

    return list(reversed(topo))


def plan_hash(plan):
    return _plan_hash(plan)
