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

    `routes` and `revit` are stub submodules - present so
    `from pyrevit import routes, revit, DB` (every revit_mcp/*.py handler
    file's import line) succeeds at import time. `routes.make_response`
    is a real, working fake (verified against the actual signature in
    pyRevit's own source, pyrevitlib/pyrevit/routes/server/__init__.py:
    make_response(data, status=200) -> Response(status=status, data=data)) -
    every offline test that invokes a full route handler function (not just
    a private helper) needs this to get a real `.status`/`.data` back,
    exactly like the live handler does.
    """
    if "pyrevit" in sys.modules and getattr(sys.modules["pyrevit"], "_is_fake", False):
        return
    fake_db = _build_fake_db()
    fake_pyrevit = types.ModuleType("pyrevit")
    fake_pyrevit.DB = fake_db
    fake_routes = types.ModuleType("routes")

    class _FakeResponse(object):
        """Mirrors pyrevit.routes.server.base.Response exactly: .status,
        .data, .headers - the only attributes any revit_mcp/*.py handler
        or test reads."""
        def __init__(self, status=200, data=None, headers=None):
            self.status = status
            self.data = data
            self._headers = headers or {}

        @property
        def headers(self):
            return self._headers

    def _fake_make_response(data, status=200, headers=None):
        return _FakeResponse(status=status, data=data, headers=headers)

    fake_routes.make_response = _fake_make_response
    fake_routes.Response = _FakeResponse
    fake_pyrevit.routes = fake_routes
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


EXTENSION_ROOT = os.path.dirname(REVIT_MCP_DIR)


def import_revit_mcp_module_as_package(name):
    """Import revit_mcp/<name>.py as revit_mcp.<name> - a genuine package
    submodule, not a bare top-level module.

    A handful of revit_mcp/*.py files (colors.py is the one this project
    has hit so far) mix "from utils import X" (bare) with
    "from .utils import Y" (package-relative) in the same file - a
    pre-existing quirk of how pyRevit's own loader makes both forms resolve
    inside Revit, which import_revit_mcp_module()'s bare-import technique
    cannot reproduce (a package-relative import raises ValueError: Attempted
    relative import in non-package outside a real package). This mirrors the
    fresh-reimport technique proven live against the running Revit process:
    put both the extension root (so "revit_mcp.<name>" resolves) and
    revit_mcp/ itself (so the sibling bare "from utils import ..." also
    resolves) on sys.path, then import the dotted name."""
    install_fake_pyrevit()
    for path in (EXTENSION_ROOT, REVIT_MCP_DIR):
        if path not in sys.path:
            sys.path.insert(0, path)
    sys.modules.pop("utils", None)
    import utils  # noqa - see import_revit_mcp_module's identical comment
    dotted = "revit_mcp." + name
    for stale in (dotted, "revit_mcp"):
        sys.modules.pop(stale, None)
    module = __import__(dotted, fromlist=[name])
    return module


class FakeCategory(object):
    """Minimal stand-in for a DB.Category: just a Name and an Id, which is
    all clear_element_colors/color_elements_by_parameter read before ever
    reaching a real element collection."""
    def __init__(self, name, id_value=1):
        self.Name = name
        self.Id = id_value


class FakeCategories(object):
    """Minimal stand-in for doc.Settings.Categories: iterable of
    FakeCategory, matching the "for cat in categories: if cat.Name == ..."
    linear-scan pattern every category-lookup in colors.py/placement.py
    uses."""
    def __init__(self, categories):
        self._categories = list(categories)

    def __iter__(self):
        return iter(self._categories)


class FakeSettings(object):
    def __init__(self, categories):
        self.Categories = FakeCategories(categories)


class FakeDoc(object):
    """Minimal stand-in for a Revit Document - only what these tests'
    handler functions read before they would need a real
    FilteredElementCollector (which stays unfaked, per this project's own
    mocking-strategy note in test_validation_helpers.py: DB-collection
    behaviour is proven live, not re-shimmed offline)."""
    def __init__(self, categories):
        self.Settings = FakeSettings(categories)


class FakeAPI(object):
    """Minimal stand-in for pyRevit's routes.API: @api.route(path, ...)
    just records the decorated function under its path and returns it
    unchanged, so a route module's register_*_routes(api) can be called
    directly and its handlers invoked like any other function - the same
    technique proven live against the running Revit process."""
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def decorator(fn):
            self.routes[path] = fn
            return fn
        return decorator


class FakeRequest(object):
    """Minimal stand-in for pyRevit's routes.Request: .data for a POST body,
    .query_params for a GET query string - the only two attributes any
    revit_mcp/*.py handler reads (verified against the real
    pyrevit/routes/server/base.py Request class, C:\\Program
    Files\\pyRevit-Master\\pyrevitlib\\pyrevit\\routes\\server\\base.py)."""
    def __init__(self, data=None, query_params=None):
        self.data = data if data is not None else {}
        self.query_params = query_params if query_params is not None else {}
