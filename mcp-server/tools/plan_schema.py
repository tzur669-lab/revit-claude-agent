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

    errors.extend(_validate_success_criteria(plan.get("success_criteria")))
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


def _validate_success_criteria(sc):
    errors = []
    if not isinstance(sc, list):
        return ["success_criteria must be a list"]
    for idx, c in enumerate(sc):
        if not isinstance(c, dict):
            errors.append("success_criteria[%d] is not an object" % idx)
            continue
        ev = c.get("evaluator")
        if ev not in ops_schema.EVALUATORS:
            errors.append(
                "success_criteria[%d]: unknown evaluator %r (known: %s)"
                % (idx, ev, sorted(ops_schema.EVALUATORS))
            )
        if c.get("op") not in CRITERION_OPS:
            errors.append("success_criteria[%d]: op must be one of %s"
                           % (idx, sorted(CRITERION_OPS)))
        target = c.get("target")
        if not isinstance(target, dict) or target.get("kind") not in REF_KINDS:
            errors.append("success_criteria[%d]: target.kind must be one of %s"
                           % (idx, sorted(REF_KINDS)))
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
