# -*- coding: utf-8 -*-
"""
Drift guard between tools/ops_schema.py (CPython data, importable directly)
and revit_mcp/ops.py's real OPS dict (IronPython, imports System.* at
module level via its editing/building/placement/rooms/transforms
dependencies -- NOT importable under plain CPython, matching this
project's own established rule that FilteredElementCollector-touching code
"stays unfaked... proven live, not shimmed").

So this file extracts revit_mcp/ops.py's OPS dict keys purely by AST,
exactly the technique tools/ast_facts.py already uses for tools/*_tools.py
-- no fake needed, and nothing here can go stale silently because
test_ops_registry drift checks run in both directions.
"""
import ast
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools import ops_schema  # noqa: E402

OPS_PY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "ops.py",
)


def _ops_py_tree():
    with open(OPS_PY_PATH, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=OPS_PY_PATH)


def extract_ops_dict_keys():
    """String literal keys of the top-level `OPS = {...}` dict in
    revit_mcp/ops.py, by AST -- no import required."""
    tree = _ops_py_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "OPS" in targets and isinstance(node.value, ast.Dict):
                keys = []
                for k in node.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.append(k.value)
                    elif isinstance(k, ast.Str):  # pragma: no cover -- py2-shaped AST fallback
                        keys.append(k.s)
                return keys
    raise AssertionError("no top-level OPS = {...} dict found in ops.py")


def extract_ops_entry_fields(op_name):
    """{'risk':..., 'idempotency':..., 'fidelity':...} for one op entry,
    read as string constants directly from the dict literal (each entry is
    itself a dict literal with string-constant values for these three
    keys) -- by AST, no import."""
    tree = _ops_py_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "OPS" not in targets or not isinstance(node.value, ast.Dict):
                continue
            for k, v in zip(node.value.keys, node.value.values):
                key_name = k.value if isinstance(k, ast.Constant) else getattr(k, "s", None)
                if key_name != op_name or not isinstance(v, ast.Dict):
                    continue
                out = {}
                for ek, ev in zip(v.keys, v.values):
                    ek_name = ek.value if isinstance(ek, ast.Constant) else getattr(ek, "s", None)
                    if ek_name in ("risk", "idempotency", "fidelity") and isinstance(ev, ast.Constant):
                        out[ek_name] = ev.value
                return out
    return {}


def test_every_ops_schema_op_exists_in_ops_py():
    real_keys = set(extract_ops_dict_keys())
    missing = ops_schema.SUPPORTED_OPS - real_keys
    assert not missing, "tools/ops_schema.py declares op(s) with no matching entry in revit_mcp/ops.py: %s" % sorted(missing)


def test_every_ops_py_op_is_declared_in_ops_schema():
    real_keys = set(extract_ops_dict_keys())
    extra = real_keys - ops_schema.SUPPORTED_OPS
    assert not extra, "revit_mcp/ops.py has op(s) missing from tools/ops_schema.py: %s" % sorted(extra)


@pytest.mark.parametrize("op_name", sorted(ops_schema.SUPPORTED_OPS))
def test_risk_idempotency_fidelity_agree_between_schema_and_real_registry(op_name):
    real = extract_ops_entry_fields(op_name)
    schema = ops_schema.OPS[op_name]
    assert real.get("risk") == schema["risk"], op_name
    assert real.get("idempotency") == schema["idempotency"], op_name
    assert real.get("fidelity") == schema["fidelity"], op_name


def test_ops_schema_has_exactly_eight_v1_ops():
    # Not an assumption of exactly 8 forever -- a derived fact this repo
    # happens to satisfy today, the way test_tool_registry.py treats its
    # own tool/route count equality. If Phase B adds ops, update this
    # number deliberately rather than let it drift silently.
    assert len(ops_schema.SUPPORTED_OPS) == 8


def test_every_op_has_a_valid_idempotency_class():
    for name, spec in ops_schema.OPS.items():
        assert spec["idempotency"] in ("absolute", "creating"), name


def test_creating_ops_all_have_a_probe_in_the_real_registry():
    """Every 'creating'-class op MUST implement probe() -- an 'unknown'
    action outcome for a creating op must never be blindly re-run."""
    tree = _ops_py_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "OPS" not in targets or not isinstance(node.value, ast.Dict):
                continue
            for k, v in zip(node.value.keys, node.value.values):
                op_name = k.value if isinstance(k, ast.Constant) else getattr(k, "s", None)
                if ops_schema.OPS.get(op_name, {}).get("idempotency") != "creating":
                    continue
                probe_val = None
                for ek, ev in zip(v.keys, v.values):
                    ek_name = ek.value if isinstance(ek, ast.Constant) else getattr(ek, "s", None)
                    if ek_name == "probe":
                        probe_val = ev
                assert probe_val is not None and not (
                    isinstance(probe_val, ast.Constant) and probe_val.value is None
                ), "creating op %r has no probe() in revit_mcp/ops.py" % op_name


def test_absolute_ops_all_have_no_probe_in_the_real_registry():
    """Symmetric check: an 'absolute' op declaring probe=None is a
    deliberate choice (resume logic never consults it), not an omission --
    guard that this stays intentional and doesn't silently drift into a
    half-implemented probe."""
    tree = _ops_py_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "OPS" not in targets or not isinstance(node.value, ast.Dict):
                continue
            for k, v in zip(node.value.keys, node.value.values):
                op_name = k.value if isinstance(k, ast.Constant) else getattr(k, "s", None)
                if ops_schema.OPS.get(op_name, {}).get("idempotency") != "absolute":
                    continue
                for ek, ev in zip(v.keys, v.values):
                    ek_name = ek.value if isinstance(ek, ast.Constant) else getattr(ek, "s", None)
                    if ek_name == "probe":
                        assert isinstance(ev, ast.Constant) and ev.value is None, op_name
