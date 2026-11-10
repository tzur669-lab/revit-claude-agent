# -*- coding: utf-8 -*-
"""
Regression guard for a real, small bug the Phase B slice 1 audit found:
rooms.py and validation.py each carried their own independently-drifted
sqft->sqm approximation (0.092903 and 0.0929 respectively). Both now import
the single canonical utils.SQFT_TO_SQM -- this test makes sure they cannot
silently re-drift.

validation.py has no `System.Collections.Generic` import, so it is
importable under the CPython fake-API harness (conftest_revit_mcp.py) like
tests/test_validation_helpers.py already does. rooms.py DOES import from
System.Collections.Generic (unfaked, by this project's own established
rule -- see revit_mcp/ops.py's own docstring on the same limitation), so it
cannot be imported under CPython at all; its half of this guard is an AST
check instead, matching the project's existing ast_facts.py pattern.
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from conftest_revit_mcp import import_revit_mcp_module  # noqa: E402

utils = import_revit_mcp_module("utils")

ROOMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "rooms.py",
)


def test_utils_sqft_to_sqm_is_exact():
    assert utils.SQFT_TO_SQM == 0.3048 ** 2


def test_validation_imports_the_canonical_constant_not_its_own():
    """Value equality, not `is`: import_revit_mcp_module gives each module
    its own fresh exec() namespace (this project's own established
    fresh-reimport technique), so validation.py's `from utils import
    SQFT_TO_SQM` binds to a float object from A SEPARATE utils execution
    than the standalone import below -- equal in value, never the same
    object, by the harness's own design. Value equality is exactly the
    property this guard cares about: that validation.py no longer carries
    its own independently-typed approximation."""
    validation = import_revit_mcp_module("validation")
    assert validation.SQFT_TO_SQM == utils.SQFT_TO_SQM
    assert validation.SQFT_TO_SQM == 0.3048 ** 2


def test_rooms_py_imports_sqft_to_sqm_from_utils_and_no_longer_hardcodes_it():
    """AST-based, since rooms.py cannot be imported under CPython (its own
    `from System.Collections.Generic import List` -- unfaked by this
    project's own rule)."""
    with open(ROOMS_PATH, "r") as f:
        source = f.read()
    tree = ast.parse(source)

    imports_it = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "utils":
            if any(alias.name == "SQFT_TO_SQM" for alias in node.names):
                imports_it = True
    assert imports_it, "rooms.py must import SQFT_TO_SQM from utils"

    # the old, independently-drifted literal must not still be there
    assert "0.092903" not in source
