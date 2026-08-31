# -*- coding: utf-8 -*-
"""
Integration tests against the real execute_code route handler (Milestone 4
of the M1-M5 architecture upgrade) - proves two hard requirements from that
milestone directly, not just at the classify()-in-isolation level tested in
test_code_safety.py:

  1. classify() being called before exec() never blocks execution - every
     risk class (read, destructive-looking, malformed/unparseable, #!notx)
     still runs to completion or fails on its own merits, never on the
     classifier's say-so.
  2. the audit write is best-effort: forcing it to fail must not change the
     execution result, must not raise to the caller, and must not turn a
     successful run into an error.

All submissions here use #!notx (no wrapping transaction) so these tests
need no fake DB.Transaction class - conftest_revit_mcp.py's fake DB has
never needed one, since no other test calls into a route this deep. clr/
System are real IronPython-only modules code_execution.py imports at the
top of the handler function; stubbed here since nothing in these
submissions actually uses them.
"""
import sys
import types

import pytest

from conftest_revit_mcp import import_revit_mcp_module, FakeAPI, FakeRequest, install_fake_pyrevit


@pytest.fixture(autouse=True)
def _stub_clr_and_system_and_stringio():
    """clr/System are real modules only inside IronPython; code_execution.py
    imports them unconditionally inside execute_code's body (not at module
    load time), so calling the handler needs them importable - a bare stub
    is enough since none of these submissions use System.Int64 or any clr
    feature. `StringIO` (Python 2's top-level module, pre-existing in this
    file - not something this milestone added) has no CPython 3
    equivalent at all; stdlib io.StringIO is the same shape (write/
    getvalue/close) so it stands in directly. This is why code_execution.py
    was never offline-importable before this test file."""
    added = []
    for name in ("clr", "System"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
            added.append(name)
    if "StringIO" not in sys.modules:
        import io as _io
        fake_stringio_module = types.ModuleType("StringIO")
        fake_stringio_module.StringIO = _io.StringIO
        sys.modules["StringIO"] = fake_stringio_module
        added.append("StringIO")
    yield
    for name in added:
        del sys.modules[name]


@pytest.fixture()
def execute_code_route(monkeypatch):
    install_fake_pyrevit()
    code_execution = import_revit_mcp_module("code_execution")
    # Pre-existing, separate gap (see the M1 implementation report):
    # revit_mcp/utils.py's repair_hebrew_in does "isinstance(value, unicode)"
    # with no guard - `unicode` is a real IronPython 2.7 builtin (production
    # unaffected) but raises NameError under CPython. Routed around here,
    # not fixed, since fixing revit_mcp/utils.py is outside this milestone.
    monkeypatch.setattr(code_execution, "repair_hebrew_in", lambda value: value)
    api = FakeAPI()
    code_execution.register_code_execution_routes(api)
    # Route audit writes to a fresh temp dir per test, never the real
    # ~/.claude/revit-tracker/audit/ - keeps these tests hermetic.
    import tempfile
    import os
    tmp_audit_dir = tempfile.mkdtemp()
    monkeypatch.setattr(code_execution, "_AUDIT_DIR", tmp_audit_dir)
    handler = api.routes["/execute_code/"]
    return handler, code_execution, tmp_audit_dir


class _FakeDocNoTransaction(object):
    """#!notx submissions never touch doc.* in these tests - a bare object
    is sufficient since the handler only passes it into the exec()
    namespace, never calls a method on it itself when t is None."""
    pass


def _call(handler, code, description="test"):
    return handler(_FakeDocNoTransaction(), FakeRequest(data={"code": code, "description": description}))


# ---------------------------------------------------------------------------
# Every risk class still executes - classify() never blocks
# ---------------------------------------------------------------------------

def test_read_risk_code_executes(execute_code_route):
    handler, _, _ = execute_code_route
    resp = _call(handler, "#!notx\nprint('hello')")
    assert resp.status == 200
    assert resp.data["status"] == "success"
    assert "hello" in resp.data["output"]
    assert resp.data["risk"] == "read"


def test_destructive_looking_code_still_executes(execute_code_route):
    """import os is classified destructive but must still run - classify()
    has no authorization concept at all (see code_safety.py)."""
    handler, _, _ = execute_code_route
    resp = _call(handler, "#!notx\nimport os\nprint('ran with os imported')")
    assert resp.status == 200
    assert resp.data["status"] == "success"
    assert resp.data["risk"] == "destructive"
    assert "ran with os imported" in resp.data["output"]


def test_malformed_code_fails_on_its_own_merits_not_via_the_classifier(execute_code_route):
    """Unparseable code fails inside exec() itself (a real SyntaxError),
    landing in the normal exception-handling branch - not intercepted
    earlier by classify() returning "unknown"."""
    handler, _, _ = execute_code_route
    resp = _call(handler, "#!notx\ndef f(:\n    not valid python")
    assert resp.status == 500
    assert resp.data["status"] == "error"
    assert resp.data["risk"] == "unknown"
    assert "SyntaxError" in resp.data["error_type"] or "Syntax" in resp.data["error"]


def test_notx_flag_itself_does_not_block(execute_code_route):
    handler, _, _ = execute_code_route
    resp = _call(handler, "#!notx\nprint('no transaction here')")
    assert resp.status == 200
    assert resp.data["status"] == "success"


def test_code_that_raises_at_runtime_still_reports_risk(execute_code_route):
    handler, _, _ = execute_code_route
    resp = _call(handler, "#!notx\nraise ValueError('boom')")
    assert resp.status == 500
    assert "risk" in resp.data
    assert "risk_signals" in resp.data


# ---------------------------------------------------------------------------
# Audit write is best-effort - forcing it to fail changes nothing observable
# ---------------------------------------------------------------------------

def test_audit_write_failure_does_not_change_a_successful_result(execute_code_route, monkeypatch):
    handler, code_execution, _ = execute_code_route
    monkeypatch.setattr(
        code_execution, "_write_audit_record",
        lambda record: (_ for _ in ()).throw(IOError("simulated disk full")),
    )
    # If _write_audit_record's own try/except doesn't actually swallow this,
    # this call would raise IOError and the test itself would fail loudly -
    # proving the guarantee, not just asserting it.
    resp = _call(handler, "#!notx\nprint('still works')")
    assert resp.status == 200
    assert resp.data["status"] == "success"
    assert "still works" in resp.data["output"]


def test_audit_write_failure_does_not_change_an_error_result(execute_code_route, monkeypatch):
    handler, code_execution, _ = execute_code_route
    monkeypatch.setattr(
        code_execution, "_write_audit_record",
        lambda record: (_ for _ in ()).throw(IOError("simulated disk full")),
    )
    resp = _call(handler, "#!notx\nraise ValueError('boom')")
    assert resp.status == 500
    assert resp.data["error_type"] == "ValueError"


def test_write_audit_record_itself_swallows_a_real_failure():
    """_write_audit_record's own internal guarantee, isolated from the
    route: pointing it at a location that cannot be written to must not
    raise - exactly the scenario execute_code depends on."""
    install_fake_pyrevit()
    code_execution = import_revit_mcp_module("code_execution")
    original = code_execution._AUDIT_DIR
    try:
        # A path that cannot become a directory (a file, not a dir, sits
        # where the audit dir would need to be created).
        import tempfile
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        code_execution._AUDIT_DIR = f.name  # os.makedirs on an existing file -> fails
        code_execution._write_audit_record({"at": "x", "code": "print(1)"})  # must not raise
    finally:
        code_execution._AUDIT_DIR = original


# ---------------------------------------------------------------------------
# The audit record itself: exact source, one JSON object per line
# ---------------------------------------------------------------------------

def test_audit_record_contains_exact_submitted_source_including_multiline_and_non_ascii(execute_code_route):
    handler, code_execution, tmp_audit_dir = execute_code_route
    code = "#!notx\n# שלום - hello\nx = 1\ny = 2\nprint(x + y)"
    resp = _call(handler, code, description="multiline non-ascii test")
    assert resp.status == 200

    import glob
    import io
    import json as _json
    files = glob.glob(tmp_audit_dir + "/code-*.ndjson")
    assert len(files) == 1
    with io.open(files[0], "r", encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert record["code"] == code  # exact, not normalized/dedented/truncated
    assert record["outcome"] == "success"
    assert record["risk"] == "read"


def test_multiple_submissions_produce_one_valid_json_object_per_line(execute_code_route):
    handler, code_execution, tmp_audit_dir = execute_code_route
    _call(handler, "#!notx\nprint('one')")
    _call(handler, "#!notx\nimport os\nprint('two')")
    _call(handler, "#!notx\nraise ValueError('three')")

    import glob
    import io
    import json as _json
    files = glob.glob(tmp_audit_dir + "/code-*.ndjson")
    with io.open(files[0], "r", encoding="utf-8") as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    assert len(lines) == 3
    outcomes = [_json.loads(l)["outcome"] for l in lines]
    assert outcomes == ["success", "success", "exception"]


def test_audit_history_is_never_deleted_or_rotated_by_this_code(execute_code_route):
    """No automatic deletion/rotation mechanism exists in
    _write_audit_record - a second submission must not touch or remove the
    file a prior one wrote to."""
    handler, code_execution, tmp_audit_dir = execute_code_route
    _call(handler, "#!notx\nprint('first')")
    import glob
    files_after_first = set(glob.glob(tmp_audit_dir + "/code-*.ndjson"))
    _call(handler, "#!notx\nprint('second')")
    files_after_second = set(glob.glob(tmp_audit_dir + "/code-*.ndjson"))
    assert files_after_first <= files_after_second
