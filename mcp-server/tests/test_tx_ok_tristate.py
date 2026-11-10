# -*- coding: utf-8 -*-
"""
Static guard for the tx_ok tri-state rule (Milestone 3 of the M1-M5
architecture upgrade). commit_verified() returns True/False/None -
None means "#!notx, self-managed, not observable" and must never be
treated as a pass or a fail. Only 1 of 28 commit_verified call sites used
to check this correctly (`if tx_ok is False:`, code_execution.py); the
other 27 used `if not tx_ok:`, which is only correct today because those
27 handlers always build a real DB.Transaction (tx_ok is never actually
None there) - a fragile invariant nothing was defending. All 27 were
normalized to `is False` as part of this milestone; this test is what
keeps a future handler from reintroducing the fragile form.

Checks every `if` statement's condition in every revit_mcp/*.py file
against the exact truthiness patterns that would collapse None into
True or False: bare `tx_ok`, `not tx_ok`, `bool(tx_ok)`, and the same
three forms through a dict/attribute lookup ending in "tx_ok" (e.g.
`result["tx_ok"]`, `result.get("tx_ok")`). `tx_ok is False` / `is None` /
`is True` / `== False` are all fine and never flagged.
"""
import ast
import glob
import os

REVIT_MCP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp"
)


def _ends_in_tx_ok(node):
    """True if `node` is an expression that plausibly evaluates to a
    tx_ok-named value: the bare name, or any subscript/attribute/call
    access whose final key/attr is "tx_ok" (result["tx_ok"],
    result.get("tx_ok"), response.tx_ok, ...)."""
    if isinstance(node, ast.Name):
        return node.id == "tx_ok"
    if isinstance(node, ast.Attribute):
        return node.attr == "tx_ok"
    if isinstance(node, ast.Subscript):
        sl = node.slice
        # Py3.9+: node.slice is the value directly (no ast.Index wrapper)
        if isinstance(sl, ast.Constant):
            return sl.value == "tx_ok"
        return False
    if isinstance(node, ast.Call):
        # result.get("tx_ok")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "tx_ok"
        ):
            return True
    return False


def _is_unsafe_truthiness_check(test_node):
    """True if `test_node` (an `if` statement's condition) collapses a
    tx_ok-shaped value's tri-state into a plain boolean:
      - bare tx_ok-shaped expression used directly as the condition
      - `not <tx_ok-shaped>`
      - `bool(<tx_ok-shaped>)`
    Explicitly NOT flagged: `<tx_ok-shaped> is False`, `is True`, `is None`,
    `== False`, etc. - those are exactly the safe forms."""
    if _ends_in_tx_ok(test_node):
        return True
    if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
        if _ends_in_tx_ok(test_node.operand):
            return True
    if (
        isinstance(test_node, ast.Call)
        and isinstance(test_node.func, ast.Name)
        and test_node.func.id == "bool"
        and test_node.args
        and _ends_in_tx_ok(test_node.args[0])
    ):
        return True
    return False


def all_revit_mcp_files():
    return sorted(glob.glob(os.path.join(REVIT_MCP_DIR, "*.py")))


def find_unsafe_tx_ok_checks(source, filename):
    tree = ast.parse(source, filename=filename)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_unsafe_truthiness_check(node.test):
            violations.append(node.lineno)
    return violations


def test_revit_mcp_directory_has_files():
    files = all_revit_mcp_files()
    assert len(files) >= 15, "expected at least 15 route-handler files, found %d" % len(files)


def test_every_revit_mcp_file_compiles():
    """Cheap insurance given this machine's pyRevit extension folder is a
    junction into this repo's mcp-server/ (see the M1-M5 architecture
    upgrade's live-verification notes) - a syntax error in any
    revit_mcp/*.py file breaks every route in the running Revit session on
    the next reload. compile() proves the file parses under CPython; it is
    NOT proof of IronPython 2.7 compatibility (see this milestone's own R5
    guardrail) - only that it is not outright broken Python syntax."""
    errors = []
    for path in all_revit_mcp_files():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        try:
            compile(source, path, "exec")
        except SyntaxError as e:
            errors.append("%s: %s" % (path, e))
    assert not errors, "\n".join(errors)


def test_no_unsafe_tx_ok_truthiness_checks():
    violations = []
    for path in all_revit_mcp_files():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for lineno in find_unsafe_tx_ok_checks(source, path):
            violations.append("%s:%d" % (os.path.basename(path), lineno))

    assert not violations, (
        "unsafe tx_ok truthiness check(s) found - these collapse the "
        "tri-state None ('#!notx', not observable) into True or False. "
        "Use 'tx_ok is False' (or 'is None' / 'is True') instead: %s"
        % violations
    )


# ---------------------------------------------------------------------------
# The detector itself, proven against synthetic examples - so a change to
# _is_unsafe_truthiness_check that silently stops detecting anything is
# caught here, not just by the (currently clean) real-file scan above.
# ---------------------------------------------------------------------------

def _parse_if_test(condition_src):
    tree = ast.parse("if {}:\n    pass".format(condition_src), filename="<test>")
    return tree.body[0].test


def test_detector_flags_bare_name():
    assert _is_unsafe_truthiness_check(_parse_if_test("tx_ok"))


def test_detector_flags_not_name():
    assert _is_unsafe_truthiness_check(_parse_if_test("not tx_ok"))


def test_detector_flags_bool_call():
    assert _is_unsafe_truthiness_check(_parse_if_test("bool(tx_ok)"))


def test_detector_flags_dict_subscript():
    assert _is_unsafe_truthiness_check(_parse_if_test('not result["tx_ok"]'))


def test_detector_flags_dict_get():
    assert _is_unsafe_truthiness_check(_parse_if_test('not result.get("tx_ok")'))


def test_detector_flags_attribute_access():
    assert _is_unsafe_truthiness_check(_parse_if_test("not response.tx_ok"))


def test_detector_does_not_flag_is_false():
    assert not _is_unsafe_truthiness_check(_parse_if_test("tx_ok is False"))


def test_detector_does_not_flag_is_none():
    assert not _is_unsafe_truthiness_check(_parse_if_test("tx_ok is None"))


def test_detector_does_not_flag_dict_is_false():
    assert not _is_unsafe_truthiness_check(_parse_if_test('result.get("tx_ok") is False'))


def test_detector_does_not_flag_unrelated_condition():
    assert not _is_unsafe_truthiness_check(_parse_if_test("not doc"))
    assert not _is_unsafe_truthiness_check(_parse_if_test("element_id"))
