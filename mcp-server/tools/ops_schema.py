# -*- coding: utf-8 -*-
"""
Pure, Revit-free description of the Phase-A v1 op set -- arg schemas,
idempotency class, fidelity, risk. This is the CPython-side data twin of
revit_mcp/ops.py's OPS registry (the module that actually runs inside
Revit). tests/test_ops_registry.py AST-extracts revit_mcp/ops.py's OPS dict
keys and asserts set equality against OPS here, in BOTH directions -- the
same drift discipline tools/registry.py already runs against
tools/*_tools.py (see tests/test_tool_registry.py).

Why two files describing the "same" registry instead of one shared module:
revit_mcp/ops.py's entries carry live callables (run/verify/inverse/probe
functions that touch DB/doc) and cannot be imported under plain CPython.
This module carries only the DATA half -- arg shape, idempotency class,
fidelity, risk -- which both propose_plan (validation) and get_task_status
(rendering) need without ever touching Revit.

idempotency:
    absolute  -- re-applying converges to the same end state; safe to
                 re-run on an unknown outcome (a crash inside the
                 transaction window with no recorded end).
    creating  -- re-applying makes a duplicate; MUST probe() on resume,
                 and an inconclusive probe blocks rather than guesses.

fidelity (of the op's OWN inverse, not of Revit's response to it):
    exact          -- inverse restores the exact prior state.
    approximate    -- inverse restores the captured state but not every
                       side effect Revit itself recomputed (e.g. joins).
    compensatable  -- inverse undoes the op via a different op (e.g.
                       delete the thing this op created).
    irreversible   -- no inverse exists; Revit cannot recreate an element
                       from an id.
"""

IDEMPOTENCY_CLASSES = frozenset(["absolute", "creating"])
FIDELITY_LEVELS = frozenset(["exact", "approximate", "compensatable", "irreversible", "unknown"])
# An op always mutates, so its risk is a subset of tools/registry.py's
# RISK_LEVELS that excludes "read" -- there is no read-only op.
RISK_LEVELS = frozenset(["additive", "modifying", "destructive"])
REF_KINDS = frozenset(["element"])  # Phase B adds room/unit/level/zone/...
EVALUATORS = frozenset([
    "room_area_sqm", "param_value", "element_exists", "element_absent", "distance_mm",
])


def _op(risk, idempotency, fidelity, required, optional, registry_tool):
    assert risk in RISK_LEVELS, risk
    assert idempotency in IDEMPOTENCY_CLASSES, idempotency
    assert fidelity in FIDELITY_LEVELS, fidelity
    return {
        "risk": risk,
        "idempotency": idempotency,
        "fidelity": fidelity,
        "args": {"required": tuple(required), "optional": tuple(optional)},
        "registry_tool": registry_tool,
    }


OPS = {
    # move_elements is rewritten from RELATIVE to ABSOLUTE at plan time
    # (revision-4's central idempotency fix): the plan stores the target
    # point (captured 'before' + the requested vector), and the op applies
    # target - current at run time. Re-applying then converges instead of
    # accumulating -- this is what makes it safe to classify "absolute"
    # even though the underlying Revit API (ElementTransformUtils.MoveElement)
    # is itself relative.
    "move_elements": _op(
        "modifying", "absolute", "exact",
        required=("vector_mm",), optional=(),
        registry_tool="transform_elements",
    ),
    "set_parameters": _op(
        "modifying", "absolute", "exact",
        required=("parameters",), optional=("allow_type_fallback",),
        registry_tool="modify_element",
    ),
    # Measured live (docs/operation-contracts.md): a hosted door's cut
    # position falling outside a reassigned wall curve can Commit() as
    # Committed with an EMPTY failure-message list and the curve reading
    # back byte-identical -- the proof verification_ok must exist
    # separately from execution_ok. Approximate fidelity: the inverse
    # restores the endpoints, not the joins/hosted-openings Revit itself
    # recomputes around a curve change.
    "set_wall_curve": _op(
        "modifying", "absolute", "approximate",
        required=("start_mm", "end_mm"), optional=(),
        registry_tool=None,
    ),
    "delete_elements": _op(
        "destructive", "absolute", "irreversible",
        required=(), optional=(),
        registry_tool="delete_elements",
    ),
    "create_room": _op(
        "additive", "creating", "compensatable",
        required=("level_id", "point_mm"), optional=("name", "number"),
        registry_tool="create_room",
    ),
    "create_room_separation": _op(
        "additive", "creating", "compensatable",
        required=("level_id", "curve_mm"), optional=(),
        registry_tool="create_room_separation",
    ),
    "create_line_based_element": _op(
        "additive", "creating", "compensatable",
        required=("type_id", "curve_mm"), optional=("level_id", "height_mm"),
        registry_tool="create_line_based_element",
    ),
    "place_family": _op(
        "additive", "creating", "compensatable",
        required=("symbol_id", "point_mm"), optional=("level_id", "host_id"),
        registry_tool="place_family",
    ),
}

SUPPORTED_OPS = frozenset(OPS.keys())


def is_reversible(op_name):
    """The single place 'reversible' is derived from, so plan_schema.py's
    validator and any future renderer agree by construction rather than by
    convention. An op is reversible unless its fidelity is exactly
    'irreversible'."""
    spec = OPS.get(op_name)
    if spec is None:
        return False
    return spec["fidelity"] != "irreversible"
