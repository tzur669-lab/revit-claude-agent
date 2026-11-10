# -*- coding: utf-8 -*-
"""
R1 (revision-4 ADR): an op's run() must NEVER construct a transaction or
call commit_verified/.Commit()/.RollBack() -- the caller (plan_exec.py's
slice loop) owns the transaction. Same AST-guard technique as
test_tx_ok_tristate.py -- a static walk over every _*_run function's body,
not a live behavioural test, so it catches the mistake even before the
function is ever called.
"""
import ast
import os

OPS_PY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "ops.py",
)

FORBIDDEN_CALL_NAMES = frozenset(["Transaction", "TransactionGroup", "commit_verified"])
FORBIDDEN_ATTRS = frozenset(["Commit", "RollBack", "Assimilate", "Start"])


def _tree():
    with open(OPS_PY_PATH, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=OPS_PY_PATH)


def _run_functions():
    """Every top-level function whose name ends in '_run' -- the naming
    convention every op's run() implementation in this file follows."""
    tree = _tree()
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.endswith("_run")]


def test_ops_py_has_at_least_eight_run_functions():
    assert len(_run_functions()) >= 8


def test_no_run_function_constructs_a_transaction_or_group():
    violations = []
    for fn in _run_functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                callee = node.func
                name = None
                if isinstance(callee, ast.Attribute):
                    name = callee.attr
                elif isinstance(callee, ast.Name):
                    name = callee.id
                if name in FORBIDDEN_CALL_NAMES:
                    violations.append("%s calls %s(...)" % (fn.name, name))
    assert not violations, (
        "run() must never open its own transaction -- the caller (plan_exec.py's "
        "slice loop) owns it. Violations: %s" % violations
    )


def test_no_run_function_calls_commit_or_rollback_or_start_or_assimilate():
    violations = []
    for fn in _run_functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_ATTRS:
                    violations.append("%s calls .%s(...)" % (fn.name, node.func.attr))
    assert not violations, (
        "run() must never manage transaction lifecycle itself. Violations: %s" % violations
    )


def test_inverse_functions_take_no_doc_argument():
    """R4: inverse(args, targets, before, result) -- PURE, no doc. A doc
    parameter here would mean an inverse implicitly depends on live model
    state, breaking "immune to the model changing since" (revision-4 ADR)."""
    tree = _tree()
    inverse_fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.endswith("_inverse")]
    assert len(inverse_fns) >= 8
    for fn in inverse_fns:
        arg_names = [a.arg for a in fn.args.args]
        assert "doc" not in arg_names, "%s must not take a doc argument" % fn.name
        assert arg_names == ["args", "targets", "before", "result"], (
            "%s has signature %s, expected (args, targets, before, result)" % (fn.name, arg_names)
        )
