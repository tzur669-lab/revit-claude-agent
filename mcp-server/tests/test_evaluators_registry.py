# -*- coding: utf-8 -*-
"""
Drift guard between tools/ops_schema.py's EVALUATOR_ARGS (CPython data,
importable directly) and revit_mcp/evaluators.py's real EVALUATOR_FUNCS
dict (IronPython, imports pyrevit/DB at module level -- NOT importable
under plain CPython, the same already-established limitation ops.py has;
see test_ops_registry.py's own docstring for the precedent this follows).

Extracts EVALUATOR_FUNCS' keys purely by AST -- no import required, no fake
needed, and nothing here can go stale silently because the drift check runs
in both directions.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools import ops_schema  # noqa: E402

EVALUATORS_PY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "evaluators.py",
)


def _tree():
    with open(EVALUATORS_PY_PATH, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=EVALUATORS_PY_PATH)


def extract_evaluator_funcs_keys():
    """String literal keys of the top-level `EVALUATOR_FUNCS = {...}` dict
    in revit_mcp/evaluators.py, by AST."""
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "EVALUATOR_FUNCS" in targets and isinstance(node.value, ast.Dict):
                keys = []
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.append(k.value)
                    elif isinstance(k, ast.Str):  # pragma: no cover -- py2-shaped AST fallback
                        keys.append(k.s)
                return keys
    raise AssertionError("no top-level EVALUATOR_FUNCS = {...} dict found in evaluators.py")


def test_every_ops_schema_evaluator_exists_in_evaluators_py():
    real_keys = set(extract_evaluator_funcs_keys())
    missing = ops_schema.EVALUATORS - real_keys
    assert not missing, (
        "tools/ops_schema.py declares evaluator(s) with no matching entry in "
        "revit_mcp/evaluators.py: %s" % sorted(missing)
    )


def test_every_evaluators_py_entry_is_declared_in_ops_schema():
    real_keys = set(extract_evaluator_funcs_keys())
    extra = real_keys - ops_schema.EVALUATORS
    assert not extra, (
        "revit_mcp/evaluators.py has evaluator(s) missing from tools/ops_schema.py: %s"
        % sorted(extra)
    )


def test_ops_schema_has_exactly_five_v1_evaluators():
    # A derived fact this repo happens to satisfy today, not an assumption
    # of exactly 5 forever -- the same style test_ops_registry.py's own
    # "exactly eight v1 ops" check uses. Update deliberately if this grows.
    assert len(ops_schema.EVALUATORS) == 5


def test_every_evaluator_has_an_arg_contract_with_required_and_optional():
    for name, spec in ops_schema.EVALUATOR_ARGS.items():
        assert "required" in spec and "optional" in spec, name
        assert isinstance(spec["required"], tuple), name
        assert isinstance(spec["optional"], tuple), name


def test_exact_target_count_only_names_known_evaluators():
    unknown = set(ops_schema.EXACT_TARGET_COUNT) - ops_schema.EVALUATORS
    assert not unknown, "EXACT_TARGET_COUNT names unknown evaluator(s): %s" % sorted(unknown)
