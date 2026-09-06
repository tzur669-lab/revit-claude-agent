# -*- coding: utf-8 -*-
"""
Every evaluator is read-only by contract (see evaluators.py's own module
docstring and the approved Phase B slice 1 plan's audit point #10): no
evaluator may open a transaction, commit, roll back, or mutate the
document. Same AST-guard technique as test_ops_no_transactions.py -- a
static walk over the whole module, not a live behavioural test, so it
catches the mistake even before any function is ever called.
"""
import ast
import os

EVALUATORS_PY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "evaluators.py",
)

FORBIDDEN_CALL_NAMES = frozenset(["Transaction", "TransactionGroup", "commit_verified"])
FORBIDDEN_ATTRS = frozenset(["Commit", "RollBack", "Assimilate", "Start", "Delete", "Set"])


def _tree():
    with open(EVALUATORS_PY_PATH, "r", encoding="utf-8") as f:
        return ast.parse(f.read(), filename=EVALUATORS_PY_PATH)


def test_no_transaction_or_group_constructed_anywhere_in_the_module():
    violations = []
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.attr if isinstance(callee, ast.Attribute) else (
                callee.id if isinstance(callee, ast.Name) else None)
            if name in FORBIDDEN_CALL_NAMES:
                violations.append(name)
    assert not violations, (
        "evaluators.py must never open a transaction -- it is read-only by "
        "contract. Violations: %s" % violations
    )


def test_no_commit_rollback_start_assimilate_delete_or_set_call_anywhere():
    """.Set(...) is included because a Set() call on a Parameter would be a
    mutation -- evaluators only ever READ a parameter's value
    (read_param_native), never write one."""
    violations = []
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_ATTRS:
                violations.append(".%s(...)" % node.func.attr)
    assert not violations, (
        "evaluators.py must never mutate the document. Violations: %s" % violations
    )


def test_evaluate_success_criteria_never_raises_by_construction():
    """Static shape check: the dispatcher wraps its per-criterion work in a
    try/except -- a real behavioural proof that it never raises needs the
    live model (evaluators.py cannot be imported under CPython), but this
    at least guards that the try/except structure itself isn't removed."""
    tree = _tree()
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate_success_criteria"),
        None,
    )
    assert fn is not None, "evaluate_success_criteria() not found in evaluators.py"
    has_try = any(isinstance(node, ast.Try) for node in ast.walk(fn))
    assert has_try, "evaluate_success_criteria must wrap per-criterion evaluation in try/except"
