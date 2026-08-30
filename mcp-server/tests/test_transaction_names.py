# -*- coding: utf-8 -*-
"""
The single highest-value test in this project's regression set.

The tracker (memory-system/tracker/tracker.py) decides whether a model
change was made by Claude or by a human using this exact rule
(_tx_is_ours): the transaction name either contains "MCP", or is one of
the five names in MCP_TX_NAMES - a closed list, derived from this exact
source, for the routes whose transaction name does NOT happen to contain
"MCP" (Create Levels, Create Line-Based Elements, Create Surface-Based
Elements, Color Elements by Parameter, Clear Element Colors).

This coupling between the two halves of the project (mcp-server/ and
memory-system/) was previously maintained entirely by hand, in two
separate files, with no way to check it automatically - the exact
scenario that produced the original bug (23 of 28 transaction names
contained "MCP", 5 did not, and the 5 were silently attributed to
"human"). Both halves now live in one repo, so the invariant is finally
checkable: every DB.Transaction(doc, ...) name found in revit_mcp/*.py
must satisfy the tracker's own rule, or Claude's own work will be
mislabelled as a human's - the one error this system must not make.
"""
import ast
import glob
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REVIT_MCP_DIR = os.path.join(REPO_ROOT, "mcp-server", "revit_mcp")
TRACKER_DIR = os.path.join(REPO_ROOT, "memory-system", "tracker")

sys.path.insert(0, TRACKER_DIR)
import tracker  # noqa: E402  (path must be set up first)


def _string_value(node):
    """Return the literal string value of an ast constant node, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _static_string_content(node):
    """The static, guaranteed-present text of a name expression, or None.

    Handles three shapes actually used in revit_mcp/*.py:
      - a plain string literal: "Create Room via MCP"
      - "...{}...".format(x) - the string being formatted always appears
        verbatim in the real runtime name, regardless of what x is
        (code_execution.py: "MCP Code Execution: {}".format(description))
      - an f-string (JoinedStr) - concatenates only the literal Constant
        segments, skipping interpolated {expr} parts, which is exactly the
        text guaranteed present no matter what the expr evaluates to

    This is deliberately a check against the STATIC part only - if that
    part alone doesn't satisfy _tx_is_ours, the real interpolated value
    can't rescue it, since "MCP" either is or isn't in the fixed prefix.
    """
    direct = _string_value(node)
    if direct is not None:
        return direct

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            base = _string_value(func.value)
            if base is not None:
                return base

    if isinstance(node, ast.JoinedStr):
        parts = [v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if parts:
            return "".join(parts)

    return None


def _is_db_transaction_call(node):
    """True if `node` is a Call whose func is `DB.Transaction`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "Transaction"
        and isinstance(func.value, ast.Name)
        and func.value.id == "DB"
    )


def extract_transaction_names(source, filename):
    """Every DB.Transaction(doc, <name>) name in one file's source.

    <name> is usually a string literal, but placement.py assigns it to a
    local variable first (`transaction_name = "..."; t = DB.Transaction(doc,
    transaction_name)`) - handled with a simple single-pass "last string
    assigned to this name so far" lookup, sufficient for the one file that
    does this. Raises AssertionError (with the file/line) if a transaction
    name can't be resolved at all, rather than silently skipping it - an
    unresolvable name is exactly the kind of thing this test exists to catch.
    """
    tree = ast.parse(source, filename=filename)
    names = []
    last_string_assignment = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            val = _static_string_content(node.value)
            if val is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        last_string_assignment[target.id] = val

        if _is_db_transaction_call(node) and len(node.args) >= 2:
            name_arg = node.args[1]
            direct = _static_string_content(name_arg)
            if direct is not None:
                names.append((direct, node.lineno))
                continue
            if isinstance(name_arg, ast.Name) and name_arg.id in last_string_assignment:
                names.append((last_string_assignment[name_arg.id], node.lineno))
                continue
            raise AssertionError(
                "%s:%d - DB.Transaction(doc, ...) name is neither a string "
                "literal nor a variable with a known string value; this test "
                "cannot verify it, which is itself worth fixing" % (filename, node.lineno)
            )

    return names


def all_revit_mcp_files():
    return sorted(glob.glob(os.path.join(REVIT_MCP_DIR, "*.py")))


def test_revit_mcp_directory_exists_and_has_files():
    files = all_revit_mcp_files()
    assert len(files) >= 15, "expected at least 15 route-handler files, found %d - path wrong?" % len(files)


def test_every_transaction_name_satisfies_tx_is_ours():
    """The invariant. Every real transaction name found in the source must
    be attributable to Claude by the tracker's own rule."""
    violations = []
    total_checked = 0

    for path in all_revit_mcp_files():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for name, lineno in extract_transaction_names(source, path):
            total_checked += 1
            if not tracker._tx_is_ours([name]):
                violations.append("%s:%d  %r" % (os.path.basename(path), lineno, name))

    assert total_checked >= 25, (
        "expected at least 25 transaction names across revit_mcp/*.py "
        "(the project's own count is 28); found %d - extraction may be "
        "broken, not just the invariant" % total_checked
    )
    assert not violations, (
        "the following transaction names would be misattributed to a human "
        "by the tracker's _tx_is_ours() - either the name needs \"MCP\" in "
        "it, or it must be added to tracker.MCP_TX_NAMES (derived from this "
        "exact source, never guessed):\n  " + "\n  ".join(violations)
    )


def test_mcp_tx_names_are_all_still_real():
    """The other direction: every name in the closed list must actually
    exist as a real transaction somewhere, or MCP_TX_NAMES is stale."""
    found_names = set()
    for path in all_revit_mcp_files():
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        for name, _lineno in extract_transaction_names(source, path):
            found_names.add(name)

    stale = [n for n in tracker.MCP_TX_NAMES if n not in found_names]
    assert not stale, (
        "tracker.MCP_TX_NAMES contains names no longer used by any route "
        "handler - re-derive the list from source rather than leaving stale "
        "entries: %r" % stale
    )


def test_tx_is_ours_never_upgrades_by_substring():
    """A direct regression test for the original bug: exact-match against
    MCP_TX_NAMES, not substring. A name that merely CONTAINS one of the
    closed-list names (but isn't equal to it) must not match unless it
    also contains "MCP"."""
    for close_call_name in tracker.MCP_TX_NAMES:
        decoy = close_call_name + " (renamed)"
        assert not tracker._tx_is_ours([decoy]), (
            "_tx_is_ours matched %r by substring against the closed list "
            "entry %r - it must be exact-match; a wrong match here "
            "attributes human work to Claude" % (decoy, close_call_name)
        )
