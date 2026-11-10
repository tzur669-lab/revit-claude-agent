# -*- coding: utf-8 -*-
"""
Unit tests for revit_mcp/colors.py's transaction-failure response shape
(the M1 fix for D4 in the M1-M5 architecture upgrade): a transaction-commit
failure inside color_elements_by_parameter/clear_element_colors used to be
reported as {"status": "error", "message": ...} and always wrapped at
HTTP 200 by routes.make_response(data=result) - so a caller saw a
misleading "Error: Unknown error occurred" (format_response reads "error",
not "message") at an HTTP status that looks like success. Both routes now
use an "error" key and the outer route wraps it at HTTP 500 specifically
when tx_ok is False.

Per this project's own established mocking strategy (see
test_validation_helpers.py's docstring: DB-collection behaviour is proven
live, not re-shimmed offline), these tests do NOT attempt to fake
DB.FilteredElementCollector or a real element loop - color_splash and
clear_colors's success path and their true tx_ok=False path (via a real,
always-rolled-back Transaction) were both proven live against the running
Revit process instead (see the M1 implementation report).

What IS safely, precisely testable offline without any DB shim is exactly
the code this fix touches at the route level: given whatever dict the
inner function returns, does the route wrap it at the right HTTP status?
So these tests monkeypatch the inner function itself (not Revit's API) and
assert on the route's own decision.

Separately-noted finding, NOT part of this fix: revit_mcp/utils.py's
repair_hebrew_in - called unconditionally as the first line of every POST
route handler, including both routes here - does
"if isinstance(value, unicode):" with no guard. `unicode` is a real
IronPython 2.7 builtin (production is unaffected), but does not exist
under CPython 3, so this raises NameError immediately on any non-empty
dict/list under the offline test suite. That is a real, separate,
pre-existing gap (repair_hebrew_in has never been offline-testable), not
something this fix touches - routed around here with a narrow monkeypatch
rather than changed, since changing revit_mcp/utils.py is outside this
fix's scope.
"""
from conftest_revit_mcp import import_revit_mcp_module_as_package, FakeAPI, FakeRequest

colors = import_revit_mcp_module_as_package("colors")


def _register(monkeypatch):
    monkeypatch.setattr(colors, "repair_hebrew_in", lambda value: value)
    api = FakeAPI()
    colors.register_color_routes(api)
    return api


def test_color_splash_route_returns_500_when_tx_ok_is_false(monkeypatch):
    fake_result = {
        "status": "error",
        "tx_status": "RolledBack",
        "tx_ok": False,
        "error": "Transaction did not commit (tx_status=RolledBack) - no colors were applied.",
    }
    monkeypatch.setattr(colors, "color_elements_by_parameter", lambda *a, **k: fake_result)
    api = _register(monkeypatch)
    resp = api.routes["/color_splash/"](
        doc=object(),
        request=FakeRequest(data={"category_name": "Windows", "parameter_name": "Mark"}),
    )
    assert resp.status == 500
    assert resp.data["error"] == fake_result["error"]
    assert "message" not in resp.data


def test_clear_colors_route_returns_500_when_tx_ok_is_false(monkeypatch):
    fake_result = {
        "status": "error",
        "tx_status": "RolledBack",
        "tx_ok": False,
        "error": "Transaction did not commit (tx_status=RolledBack) - colors were not cleared.",
    }
    monkeypatch.setattr(colors, "clear_element_colors", lambda *a, **k: fake_result)
    api = _register(monkeypatch)
    resp = api.routes["/clear_colors/"](
        doc=object(), request=FakeRequest(data={"category_name": "Windows"})
    )
    assert resp.status == 500
    assert resp.data["error"] == fake_result["error"]


def test_color_splash_route_stays_200_on_genuine_success(monkeypatch):
    """Characterizes the behavior this fix must NOT change: a normal
    successful result (tx_ok True, or no tx_ok key at all) still gets the
    default HTTP 200 - this fix is scoped narrowly to tx_ok is False."""
    fake_result = {"status": "success", "tx_ok": True, "message": "Successfully colored 4 elements"}
    monkeypatch.setattr(colors, "color_elements_by_parameter", lambda *a, **k: fake_result)
    api = _register(monkeypatch)
    resp = api.routes["/color_splash/"](
        doc=object(),
        request=FakeRequest(data={"category_name": "Windows", "parameter_name": "Mark"}),
    )
    assert resp.status == 200
    assert resp.data is fake_result


def test_clear_colors_route_stays_200_on_pre_existing_error_status_without_tx_ok(monkeypatch):
    """Characterizes a second thing this fix deliberately does NOT change:
    the function's OTHER "status": "error" paths (category not found, no
    elements found) have no "tx_ok" key at all and are a separate,
    pre-existing issue outside this fix's scope (see the M1 implementation
    report) - they must keep returning HTTP 200, unchanged, rather than
    this fix accidentally widening to any "status": "error" dict."""
    api = _register(monkeypatch)
    resp = api.routes["/clear_colors/"](
        doc=None,  # deliberately no elements/category lookup reached below
        request=FakeRequest(data={"category_name": ""}),
    )
    # category_name is empty -> the route's own 400 short-circuit, before
    # clear_element_colors is ever called.
    assert resp.status == 400
    assert "category_name is required" in resp.data["error"]


def test_clear_colors_route_stays_200_when_inner_function_reports_category_not_found(monkeypatch):
    fake_result = {"status": "error", "message": "Category 'Bogus' not found"}
    monkeypatch.setattr(colors, "clear_element_colors", lambda *a, **k: fake_result)
    api = _register(monkeypatch)
    resp = api.routes["/clear_colors/"](doc=object(), request=FakeRequest(data={"category_name": "Bogus"}))
    assert resp.status == 200
    assert resp.data is fake_result
