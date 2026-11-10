# -*- coding: utf-8 -*-
"""
Unit tests for the pure, duck-typed helpers in revit_mcp/impact.py
(_describe, _dependents). These two touch no DB.* symbols directly - only
element/doc duck typing - so they are tested with plain fake objects rather
than a heavier Revit API shim; analyze_relationships' and
preview_delete_impact's remaining helpers (_joined_with, _host_info,
_hosted_elements, _room_boundary_map, _in_room) call DB.FilteredElementCollector,
DB.JoinGeometryUtils, DB.SpatialElementBoundaryOptions etc. directly and were
proven instead through the real wired handlers against the live model (see
docs/operation-contracts.md) - building a FilteredElementCollector/
BuiltInCategory shim just to re-prove what live verification already proved
would be exactly the general Revit-API-shim overreach this project's own
mocking strategy avoids.
"""
from conftest_revit_mcp import import_revit_mcp_module

impact = import_revit_mcp_module("impact")


class _FakeCategory(object):
    def __init__(self, name):
        self.Name = name


class _FakeElement(object):
    def __init__(self, id_value, name="Some Element", category_name="Walls"):
        self.Id = impact.DB.ElementId(id_value)
        self.Name = name
        self.Category = _FakeCategory(category_name) if category_name is not None else None


# ---------------------------------------------------------------------------
# _describe
# ---------------------------------------------------------------------------

def test_describe_none_is_none():
    assert impact._describe(None) is None


def test_describe_returns_id_name_category():
    elem = _FakeElement(42, name="Basic Wall", category_name="Walls")
    result = impact._describe(elem)
    assert result["id"] == 42
    assert result["name"] == "Basic Wall"
    assert result["category"] == "Walls"


def test_describe_missing_category_falls_back_to_unknown():
    """An element whose Category is None (observed for some resource-like
    elements) must not raise - it must fail closed to 'Unknown', never crash
    the whole relationship report over one element."""
    elem = _FakeElement(7, category_name=None)
    result = impact._describe(elem)
    assert result["category"] == "Unknown"
    assert result["id"] == 7


# ---------------------------------------------------------------------------
# _dependents
# ---------------------------------------------------------------------------

class _FakeDocForDependents(object):
    def __init__(self, elements_by_id):
        self._elements = elements_by_id

    def GetElement(self, element_id):
        return self._elements.get(element_id.Value)


class _FakeElementWithDeps(_FakeElement):
    def __init__(self, id_value, dependent_ids):
        _FakeElement.__init__(self, id_value)
        self._dependent_ids = dependent_ids

    def GetDependentElements(self, elem_filter):
        return list(self._dependent_ids)


def test_dependents_excludes_self_id():
    """The exact case the docstring calls out: a dependent list that
    includes the element's own id must not report itself as its own
    dependent."""
    self_id = impact.DB.ElementId(1)
    other = impact.DB.ElementId(2)
    doc = _FakeDocForDependents({2: _FakeElement(2, category_name="Doors")})
    el = _FakeElementWithDeps(1, [self_id, other])
    result = impact._dependents(doc, el, max_items=50)
    assert result["total"] == 1
    assert result["by_category"] == {"Doors": [{"id": 2, "name": "Some Element", "category": "Doors"}]}


def test_dependents_groups_by_category():
    doc = _FakeDocForDependents({
        2: _FakeElement(2, category_name="Doors"),
        3: _FakeElement(3, category_name="Doors"),
        4: _FakeElement(4, category_name="Dimensions"),
    })
    el = _FakeElementWithDeps(1, [impact.DB.ElementId(x) for x in (2, 3, 4)])
    result = impact._dependents(doc, el, max_items=50)
    assert result["total"] == 3
    assert len(result["by_category"]["Doors"]) == 2
    assert len(result["by_category"]["Dimensions"]) == 1
    assert result["truncated"] is False


def test_dependents_respects_max_items_and_reports_truncated():
    doc = _FakeDocForDependents({i: _FakeElement(i, category_name="Doors") for i in range(2, 7)})
    el = _FakeElementWithDeps(1, [impact.DB.ElementId(x) for x in range(2, 7)])
    result = impact._dependents(doc, el, max_items=2)
    assert result["total"] == 5
    assert result["truncated"] is True
    assert len(result["by_category"]["Doors"]) == 2


def test_dependents_unresolvable_dependent_is_not_a_crash():
    """A dependent id that doesn't resolve via doc.GetElement (already gone,
    or a non-element reference) must fail closed to an id-only entry under
    'Unknown', never raise and abort the whole report."""
    doc = _FakeDocForDependents({})  # id 2 resolves to nothing
    el = _FakeElementWithDeps(1, [impact.DB.ElementId(2)])
    result = impact._dependents(doc, el, max_items=50)
    assert result["total"] == 1
    assert result["by_category"] == {"Unknown": [{"id": 2}]}


def test_dependents_exception_fails_closed_with_reason():
    class _BrokenElement(_FakeElement):
        def GetDependentElements(self, elem_filter):
            raise RuntimeError("boom")

    doc = _FakeDocForDependents({})
    el = _BrokenElement(1)
    result = impact._dependents(doc, el, max_items=50)
    assert result["ok"] is False
    assert "boom" in result["reason"]
