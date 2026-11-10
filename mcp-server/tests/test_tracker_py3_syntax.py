# -*- coding: utf-8 -*-
"""
Guards Milestone 2 (docs/../memory-system/tracker/tracker.py's Python-2-to-3
compatible syntax fix) against silently regressing. tracker.py takes no
Revit imports at module level - the host injects doc/DB/revit/clr/System/
print into globals at execfile time - so its pure logic is importable and
unit-testable under plain CPython, but only once its syntax is valid there.

Two families were fixed and must never come back:
  - `except X, e:` (Python 2 only) instead of `except X as e:`
  - `System.IO.FileShare.None` (None cannot be an attribute name in Python 3)

Both are also valid IronPython 2.7 syntax in their fixed form, so this is a
pure syntax guard, not a behaviour change to protect.
"""
import ast
import os

TRACKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "memory-system", "tracker", "tracker.py",
)


def _source():
    with open(TRACKER_PATH, "r", encoding="ascii") as f:
        return f.read()


def test_tracker_py_exists():
    assert os.path.isfile(TRACKER_PATH), "tracker.py not found at expected path: %s" % TRACKER_PATH


def test_tracker_py_parses_under_python3():
    # The real regression guard: if Python-2-only syntax comes back, this
    # raises SyntaxError before any of the checks below even run.
    ast.parse(_source(), filename=TRACKER_PATH)


def test_no_comma_style_except_clauses():
    source = _source()
    tree = ast.parse(source, filename=TRACKER_PATH)
    # A comma-style "except X, e:" is a SyntaxError in Python 3 (caught by
    # test_tracker_py_parses_under_python3 above) - so if we got this far,
    # there are none. This test instead guards the fixed form is actually
    # present in enough places that the fix wasn't accidentally reverted to
    # something else entirely (e.g. all handlers deleted).
    except_handlers_with_name = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.name is not None
    ]
    assert len(except_handlers_with_name) >= 11, (
        "expected at least 11 'except X as name:' clauses (the count fixed "
        "in Milestone 2); found %d - the fix may have been reverted or "
        "handlers removed" % len(except_handlers_with_name)
    )


def test_no_fileshare_none_attribute_access():
    source = _source()
    assert "System.IO.FileShare.None" not in source, (
        "System.IO.FileShare.None reintroduced - None cannot be an attribute "
        "name in Python 3. Use getattr(System.IO.FileShare, \"None\") instead."
    )
    assert 'getattr(System.IO.FileShare, "None")' in source, (
        "expected the getattr()-based FileShare.None workaround; it appears "
        "to have been removed rather than replaced"
    )
