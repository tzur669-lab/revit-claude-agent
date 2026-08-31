# -*- coding: utf-8 -*-
"""
A minimal fake `pyrevit` / `DB` so revit_mcp/utils.py - real IronPython
code written for a pyRevit host - can be imported and unit-tested under
plain CPython. Not a general Revit API shim: only the surface
revit_mcp/utils.py actually touches at import time or in the functions
these tests exercise (commit_verified, _FailureSwallower's base class).

Imported explicitly by the test modules that need it (not autouse via
conftest.py), since most of this project's offline tests need no Revit
surface at all and importing this unconditionally would be misleading
about what they actually depend on.
"""
import os
import sys
import types

REVIT_MCP_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp",
)


class _FakeTransactionStatus:
    """Mirrors the 7 real DB.TransactionStatus members, enumerated live
    from Revit on 2026-08-31 via System.Enum.GetNames(DB.TransactionStatus)
    - not guessed. Each value stringifies to its own name, matching the
    real engine's behaviour (verified live: str(status) == "Committed")."""

    def __init__(self, name, value):
        self._name = name
        self._value = value

    def __str__(self):
        return self._name

    def __repr__(self):
        return "TransactionStatus.%s" % self._name

    def __int__(self):
        return self._value

    def __eq__(self, other):
        return isinstance(other, _FakeTransactionStatus) and self._value == other._value

    def __hash__(self):
        return hash(self._value)


def _build_fake_db():
    db = types.ModuleType("DB")
    names_and_values = [
        ("Uninitialized", 0), ("Started", 1), ("RolledBack", 2),
        ("Committed", 3), ("Pending", 4), ("Error", 5), ("Proceed", 6),
    ]
    statuses = {}
    for name, value in names_and_values:
        statuses[name] = _FakeTransactionStatus(name, value)

    class _TransactionStatusNamespace(object):
        pass

    for name, status in statuses.items():
        setattr(_TransactionStatusNamespace, name, status)

    db.TransactionStatus = _TransactionStatusNamespace

    class _FakeIFailuresPreprocessor(object):
        """Base class only - _FailureSwallower's PreprocessFailures body is
        not exercised by these tests, only that the class is importable."""
        pass

    db.IFailuresPreprocessor = _FakeIFailuresPreprocessor

    class _FailureSeverity(object):
        Warning = "Warning"
        Error = "Error"

    db.FailureSeverity = _FailureSeverity

    class _FailureProcessingResult(object):
        Continue = "Continue"
        ProceedWithRollBack = "ProceedWithRollBack"

    db.FailureProcessingResult = _FailureProcessingResult

    class _StorageType(object):
        String = "String"
        Integer = "Integer"
        Double = "Double"
        ElementId = "ElementId"
        none = "none"

    db.StorageType = _StorageType

    class _FakeElementId(object):
        """Minimal stand-in for DB.ElementId: wraps one int, compares and
        hashes by that value - enough for the id-bookkeeping
        (verify_created_elements, param_read_matches's ElementId branch)
        these tests exercise."""
        def __init__(self, value):
            self.Value = int(value)
            self.IntegerValue = int(value)

        def __eq__(self, other):
            return isinstance(other, _FakeElementId) and self.Value == other.Value

        def __hash__(self):
            return hash(self.Value)

        def __repr__(self):
            return "ElementId(%d)" % self.Value

    db.ElementId = _FakeElementId

    return db


def install_fake_pyrevit():
    """Idempotent: safe to call from multiple test modules.

    `routes` and `revit` are empty stub submodules - present only so
    `from pyrevit import routes, revit, DB` (every revit_mcp/*.py handler
    file's import line) succeeds at import time. No test here calls through
    them; a test that needs `routes.make_response` monkeypatches the
    imported handler module's `routes` attribute directly (see
    test_impact_helpers.py), the same technique used for live verification
    against the real handler in this project's Revit sessions.
    """
    if "pyrevit" in sys.modules and getattr(sys.modules["pyrevit"], "_is_fake", False):
        return
    fake_db = _build_fake_db()
    fake_pyrevit = types.ModuleType("pyrevit")
    fake_pyrevit.DB = fake_db
    fake_pyrevit.routes = types.ModuleType("routes")
    fake_pyrevit.revit = types.ModuleType("revit")
    fake_pyrevit._is_fake = True
    sys.modules["pyrevit"] = fake_pyrevit
    sys.modules["DB"] = fake_db


def import_revit_mcp_utils():
    """Import revit_mcp/utils.py as a bare `utils` module - matching how
    pyRevit's own per-file sys.path injection lets every route handler do
    `from utils import ...` without a package prefix."""
    return import_revit_mcp_module("utils")


def import_revit_mcp_module(name):
    """Import any revit_mcp/<name>.py as a bare top-level module, under the
    same fake pyrevit/DB environment import_revit_mcp_utils sets up. Only
    handler modules whose module-level code needs nothing beyond what
    _build_fake_db already provides can be imported this way - a module
    exercising deeper DB.* surface (FilteredElementCollector,
    BuiltInCategory, etc.) at CALL time, not import time, is still fine:
    that surface only needs to exist if a test actually calls into it."""
    install_fake_pyrevit()
    if REVIT_MCP_DIR not in sys.path:
        sys.path.insert(0, REVIT_MCP_DIR)
    sys.modules.pop("utils", None)
    import utils  # noqa - ensures utils is the real revit_mcp/utils.py before anything else imports it
    sys.modules.pop(name, None)
    module = __import__(name)
    return module
