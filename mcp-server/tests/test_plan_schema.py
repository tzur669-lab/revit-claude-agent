# -*- coding: utf-8 -*-
"""
Offline tests for tools/plan_schema.py + tools/ops_schema.py -- pure
validation logic, zero Revit dependency.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools import plan_schema as ps  # noqa: E402
from tools import ops_schema as ops  # noqa: E402


def _valid_plan():
    return {
        "schema_v": 1,
        "task_id": "t-20260901-000000-abcd",
        "created": "2026-09-01T00:00:00",
        "objective": "Move the east wall of room 102 by 400mm.",
        "scope": {},
        "constraints": [{"id": "C1", "statement": "Do not move core walls.", "source": "user"}],
        "assumptions": [{"id": "A1", "statement": "Wall 1658707 bounds room 1660265.",
                          "source": "measured", "evidence": "analyze_relationships"}],
        "phases": [{"n": 0, "name": "Wall modifications"}],
        "actions": [
            {
                "i": 0, "phase": 0, "op": "move_elements",
                "targets": {"kind": "element", "ids": [1658707]},
                "args": {"vector_mm": {"x": 400, "y": 0, "z": 0}},
                "intent": "move the east wall", "reversible": True,
                "needs": [],
            },
        ],
        "expected_changes": {"modified": [1658707], "created": [], "deleted": [],
                              "tolerated_side_effects": ["Rooms"]},
        "success_criteria": [
            {"id": "S1", "evaluator": "room_area_sqm",
             "target": {"kind": "element", "ids": [1660265]},
             "op": ">=", "value": 12.5, "unit": "sqm", "tolerance": 0.05, "binding": True},
        ],
        "risks": [{"statement": "low risk", "severity": "low", "mitigation": "verify_move"}],
        "policy": {"on_action_failure": "abort", "on_verify_failure": "abort"},
        "confidence": {"level": "medium", "why": "A1 is measured"},
        "reasoning": {"user_request": "widen the room", "interpretation": "…",
                      "strategies_considered": [], "selected": "A", "why": "…"},
    }


# ---------------------------------------------------------------------------
# ops_schema sanity
# ---------------------------------------------------------------------------

def test_ops_schema_every_entry_has_valid_fields():
    for name, spec in ops.OPS.items():
        assert spec["risk"] in ops.RISK_LEVELS, name
        assert spec["idempotency"] in ops.IDEMPOTENCY_CLASSES, name
        assert spec["fidelity"] in ops.FIDELITY_LEVELS, name


def test_ops_schema_irreversible_op_is_not_reversible():
    assert ops.is_reversible("delete_elements") is False


def test_ops_schema_exact_fidelity_op_is_reversible():
    assert ops.is_reversible("move_elements") is True


def test_ops_schema_unknown_op_is_not_reversible_not_a_crash():
    assert ops.is_reversible("no_such_op") is False


# ---------------------------------------------------------------------------
# validate_plan: happy path
# ---------------------------------------------------------------------------

def test_valid_plan_passes():
    ok, errors = ps.validate_plan(_valid_plan())
    assert ok, errors
    assert errors == []


def test_plan_must_be_a_dict():
    ok, errors = ps.validate_plan(["not", "a", "dict"])
    assert not ok
    assert "object" in errors[0]


# ---------------------------------------------------------------------------
# unknown / missing top-level keys
# ---------------------------------------------------------------------------

def test_unknown_top_level_key_rejected():
    plan = _valid_plan()
    plan["totally_made_up_field"] = 123
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("unknown top-level key" in e for e in errors)


def test_missing_required_top_level_key_rejected():
    plan = _valid_plan()
    del plan["objective"]
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("missing required top-level key" in e for e in errors)


def test_wrong_schema_v_rejected():
    plan = _valid_plan()
    plan["schema_v"] = 2
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("schema_v" in e for e in errors)


def test_optional_top_level_keys_accepted():
    plan = _valid_plan()
    plan["revision_of"] = "abc123def456"
    plan["derived_from"] = {"task_id": "t-x", "relation": "compensation"}
    ok, errors = ps.validate_plan(plan)
    assert ok, errors


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def test_empty_actions_rejected():
    plan = _valid_plan()
    plan["actions"] = []
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("non-empty" in e for e in errors)


def test_unknown_op_rejected():
    plan = _valid_plan()
    plan["actions"][0]["op"] = "reticulate_splines"
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("unknown op" in e for e in errors)


def test_duplicate_action_index_rejected():
    plan = _valid_plan()
    dup = copy.deepcopy(plan["actions"][0])
    dup["i"] = 0
    plan["actions"].append(dup)
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("duplicate 'i'" in e for e in errors)


def test_missing_required_arg_rejected():
    plan = _valid_plan()
    plan["actions"][0]["args"] = {}
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("missing required arg" in e for e in errors)


def test_unexpected_arg_rejected():
    plan = _valid_plan()
    plan["actions"][0]["args"]["not_a_real_arg"] = 1
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("unexpected arg" in e for e in errors)


def test_bad_target_kind_rejected():
    plan = _valid_plan()
    plan["actions"][0]["targets"] = {"kind": "unit", "ref": "4B"}  # Phase B only
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("targets.kind" in e for e in errors)


def test_reversible_must_match_ops_table():
    plan = _valid_plan()
    plan["actions"][0]["reversible"] = False  # move_elements IS reversible
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("disagrees with op" in e for e in errors)


def test_reversible_omitted_is_fine():
    plan = _valid_plan()
    del plan["actions"][0]["reversible"]
    ok, errors = ps.validate_plan(plan)
    assert ok, errors


def test_irreversible_op_declaring_reversible_true_is_rejected():
    plan = _valid_plan()
    plan["actions"][0] = {
        "i": 0, "phase": 0, "op": "delete_elements",
        "targets": {"kind": "element", "ids": [1]}, "args": {},
        "reversible": True,
    }
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("disagrees with op" in e for e in errors)


def test_needs_referencing_unknown_action_rejected():
    plan = _valid_plan()
    plan["actions"][0]["needs"] = [999]
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("needs references unknown action" in e for e in errors)


# ---------------------------------------------------------------------------
# needs cycle detection + compensation order
# ---------------------------------------------------------------------------

def _actions_chain(with_cycle=False):
    a = [
        {"i": 0, "op": "move_elements", "needs": []},
        {"i": 1, "op": "move_elements", "needs": [0]},
        {"i": 2, "op": "move_elements", "needs": [1]},
    ]
    if with_cycle:
        a[0]["needs"] = [2]  # 0 -> 2 -> 1 -> 0
    return a


def test_no_cycle_in_a_simple_chain():
    assert ps.find_needs_cycle(_actions_chain()) is None


def test_cycle_detected():
    cyc = ps.find_needs_cycle(_actions_chain(with_cycle=True))
    assert cyc is not None


def test_plan_with_needs_cycle_rejected():
    plan = _valid_plan()
    plan["actions"] = _actions_chain(with_cycle=True)
    for a in plan["actions"]:
        a["targets"] = {"kind": "element", "ids": [1]}
        a["args"] = {"vector_mm": {"x": 1, "y": 0, "z": 0}}
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("cycle" in e for e in errors)


def test_compensation_order_is_reverse_topological():
    # 0 <- 1 <- 2  (2 needs 1, 1 needs 0) => forward exec order 0,1,2
    # compensation must undo 2 first, then 1, then 0
    order = ps.compensation_order(_actions_chain())
    assert order == [2, 1, 0]


def test_compensation_order_with_no_needs_is_plain_reverse():
    actions = [{"i": 0}, {"i": 1}, {"i": 2}]
    assert ps.compensation_order(actions) == [2, 1, 0]


# ---------------------------------------------------------------------------
# success_criteria
# ---------------------------------------------------------------------------

def test_unknown_evaluator_rejected():
    plan = _valid_plan()
    plan["success_criteria"][0]["evaluator"] = "vibes_check"
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("unknown evaluator" in e for e in errors)


def test_bad_criterion_op_rejected():
    plan = _valid_plan()
    plan["success_criteria"][0]["op"] = "~="
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("op must be one of" in e for e in errors)


def _minimal_criterion(ev):
    """A criterion satisfying evaluator ev's own arg contract and target
    id-count exactly -- element_exists/element_absent take no extra args and
    any non-empty id count; room_area_sqm/param_value need exactly 1 id;
    distance_mm needs exactly 2."""
    n = ops.EXACT_TARGET_COUNT.get(ev, 1)
    c = {"id": "S1", "evaluator": ev, "target": {"kind": "element", "ids": list(range(1, n + 1))},
         "op": "=="}
    if ev in ("room_area_sqm", "distance_mm"):
        c["value"] = 1
    elif ev == "param_value":
        c["parameter_name"] = "Comments"
        c["value"] = "x"
    return c


def test_all_five_v1_evaluators_accepted():
    plan = _valid_plan()
    for ev in sorted(ops.EVALUATORS):
        plan["success_criteria"] = [_minimal_criterion(ev)]
        ok, errors = ps.validate_plan(plan)
        assert ok, (ev, errors)


def test_criterion_decorative_fields_are_tolerated():
    """id/unit/tolerance/binding etc. are not part of any evaluator's arg
    contract but are not rejected either -- the same descriptive-metadata
    leniency constraints/assumptions/risks already get elsewhere in this
    schema (their own id/source/evidence/severity fields are never
    whitelisted), not the stricter top-level 'unknown key' discipline
    validate_plan applies to the plan document itself."""
    plan = _valid_plan()
    c = _minimal_criterion("room_area_sqm")
    c.update({"unit": "sqm", "tolerance": 0.05, "binding": True, "notes": "why this matters"})
    plan["success_criteria"] = [c]
    ok, errors = ps.validate_plan(plan)
    assert ok, errors


def test_missing_required_arg_rejected_per_evaluator():
    for ev in ("room_area_sqm", "distance_mm", "param_value"):
        plan = _valid_plan()
        c = _minimal_criterion(ev)
        # drop every required arg this evaluator declares
        for key in ops.EVALUATOR_ARGS[ev]["required"]:
            c.pop(key, None)
        plan["success_criteria"] = [c]
        ok, errors = ps.validate_plan(plan)
        assert not ok, ev
        assert any("missing required arg" in e for e in errors), (ev, errors)


def test_value_must_be_a_number_not_a_bool_or_string():
    plan = _valid_plan()
    c = _minimal_criterion("room_area_sqm")
    c["value"] = "12.5"  # a string, not a number
    plan["success_criteria"] = [c]
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("'value' must be a number" in e for e in errors)

    plan2 = _valid_plan()
    c2 = _minimal_criterion("distance_mm")
    c2["value"] = True  # bool is not accepted as a number here either
    plan2["success_criteria"] = [c2]
    ok2, errors2 = ps.validate_plan(plan2)
    assert not ok2
    assert any("'value' must be a number" in e for e in errors2)


def test_parameter_name_must_be_a_non_empty_string():
    plan = _valid_plan()
    c = _minimal_criterion("param_value")
    c["parameter_name"] = ""
    plan["success_criteria"] = [c]
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("'parameter_name' must be" in e for e in errors)


def test_allow_type_fallback_must_be_a_bool():
    plan = _valid_plan()
    c = _minimal_criterion("param_value")
    c["allow_type_fallback"] = "yes"
    plan["success_criteria"] = [c]
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("'allow_type_fallback' must be a bool" in e for e in errors)


def test_distance_mm_requires_exactly_two_target_ids():
    for n in (1, 3):
        plan = _valid_plan()
        c = _minimal_criterion("distance_mm")
        c["target"]["ids"] = list(range(1, n + 1))
        plan["success_criteria"] = [c]
        ok, errors = ps.validate_plan(plan)
        assert not ok, n
        assert any("exactly 2 id(s)" in e for e in errors), (n, errors)


def test_room_area_sqm_and_param_value_require_exactly_one_target_id():
    for ev in ("room_area_sqm", "param_value"):
        plan = _valid_plan()
        c = _minimal_criterion(ev)
        c["target"]["ids"] = [1, 2]
        plan["success_criteria"] = [c]
        ok, errors = ps.validate_plan(plan)
        assert not ok, ev
        assert any("exactly 1 id(s)" in e for e in errors), (ev, errors)


def test_empty_target_ids_rejected_for_every_evaluator():
    for ev in sorted(ops.EVALUATORS):
        plan = _valid_plan()
        c = _minimal_criterion(ev)
        c["target"]["ids"] = []
        plan["success_criteria"] = [c]
        ok, errors = ps.validate_plan(plan)
        assert not ok, ev
        assert any("target.ids must be a non-empty list" in e for e in errors), (ev, errors)


# ---------------------------------------------------------------------------
# policy / expected_changes
# ---------------------------------------------------------------------------

def test_bad_policy_value_rejected():
    plan = _valid_plan()
    plan["policy"]["on_action_failure"] = "yolo"
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("policy." in e for e in errors)


def test_expected_changes_wrong_type_rejected():
    plan = _valid_plan()
    plan["expected_changes"]["modified"] = "not-a-list"
    ok, errors = ps.validate_plan(plan)
    assert not ok
    assert any("expected_changes" in e for e in errors)


# ---------------------------------------------------------------------------
# plan_hash re-export
# ---------------------------------------------------------------------------

def test_plan_hash_reexport_matches_task_store():
    from revit_mcp.task_store import plan_hash as ts_hash
    plan = _valid_plan()
    assert ps.plan_hash(plan) == ts_hash(plan)


def test_plan_hash_ignores_task_id_and_created():
    plan1 = _valid_plan()
    plan2 = _valid_plan()
    plan2["task_id"] = "t-different"
    plan2["created"] = "2099-01-01T00:00:00"
    assert ps.plan_hash(plan1) == ps.plan_hash(plan2)
