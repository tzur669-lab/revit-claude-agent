# -*- coding: utf-8 -*-
"""
Unit tests for the shared level-2 post-condition helpers in
revit_mcp/utils.py: param_read_matches (set_parameter/modify_element) and
verify_created_elements (the create_* handlers). The case this file exists
to guard, per the project's own plan: a committed-but-wrong result must
never read as a pass, and a not_checked result must never silently render
as success - both are distinct from True and must stay distinct.
"""
from conftest_revit_mcp import import_revit_mcp_utils

utils = import_revit_mcp_utils()


class _FakeParam(object):
    def __init__(self, storage_type, value):
        self._storage_type = storage_type
        self._value = value

    @property
    def StorageType(self):
        return self._storage_type

    def AsString(self):
        return self._value

    def AsInteger(self):
        return self._value

    def AsDouble(self):
        return self._value

    def AsElementId(self):
        return self._value


# ---------------------------------------------------------------------------
# param_read_matches
# ---------------------------------------------------------------------------

def test_param_read_matches_string_exact():
    p = _FakeParam(utils.DB.StorageType.String, "hello")
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.String, "hello")
    assert ok is True and actual == "hello"


def test_param_read_matches_string_mismatch_is_committed_but_wrong():
    """The exact case the plan calls out: Set() didn't raise, Commit()
    said Committed, but the value that actually stuck is not what was
    requested - this must read as ok: False, never as a pass."""
    p = _FakeParam(utils.DB.StorageType.String, "actual-value")
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.String, "requested-value")
    assert ok is False
    assert actual == "actual-value"


def test_param_read_matches_double_type_aware_not_string_comparison():
    """Requesting "5" (a string, as arrives from JSON) for a Double
    parameter that correctly reads back 5.0 must NOT be reported as a
    mismatch - the comparison is by typed value, not raw string equality."""
    p = _FakeParam(utils.DB.StorageType.Double, 5.0)
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.Double, "5")
    assert ok is True
    assert actual == "5.0"


def test_param_read_matches_double_within_tolerance():
    p = _FakeParam(utils.DB.StorageType.Double, 5.0000001)
    ok, _ = utils.param_read_matches(p, utils.DB.StorageType.Double, 5.0)
    assert ok is True


def test_param_read_matches_double_outside_tolerance_fails():
    p = _FakeParam(utils.DB.StorageType.Double, 5.1)
    ok, _ = utils.param_read_matches(p, utils.DB.StorageType.Double, 5.0)
    assert ok is False


def test_param_read_matches_integer_exact():
    p = _FakeParam(utils.DB.StorageType.Integer, 7)
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.Integer, "7")
    assert ok is True and actual == "7"


def test_param_read_matches_element_id():
    p = _FakeParam(utils.DB.StorageType.ElementId, utils.DB.ElementId(42))
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.ElementId, 42)
    assert ok is True and actual == "42"


def test_param_read_matches_unsupported_storage_type_fails_closed():
    """A storage type this function doesn't know about must fail closed
    (False), never silently pass as if it had been verified."""
    p = _FakeParam(utils.DB.StorageType.none, None)
    ok, actual = utils.param_read_matches(p, utils.DB.StorageType.none, "x")
    assert ok is False
    assert "unsupported" in actual.lower()


def test_param_read_matches_read_exception_fails_closed():
    class _BrokenParam(object):
        StorageType = utils.DB.StorageType.String

        def AsString(self):
            raise RuntimeError("boom")

    ok, actual = utils.param_read_matches(_BrokenParam(), utils.DB.StorageType.String, "x")
    assert ok is False
    assert "read failed" in actual


# ---------------------------------------------------------------------------
# verify_created_elements
# ---------------------------------------------------------------------------

class _FakeElement(object):
    def __init__(self, category_id):
        class _Cat(object):
            def __init__(self, cid):
                self.Id = utils.DB.ElementId(cid)
        self.Category = _Cat(category_id) if category_id is not None else None


class _FakeDoc(object):
    def __init__(self, elements_by_id):
        self._elements = elements_by_id

    def GetElement(self, element_id):
        return self._elements.get(element_id.Value)


def test_verify_created_elements_all_resolve_and_match_category():
    doc = _FakeDoc({1: _FakeElement(100), 2: _FakeElement(100)})
    result = utils.verify_created_elements(doc, [(1, 100), (2, 100)])
    assert result["ok"] is True
    assert "failures" not in result


def test_verify_created_elements_missing_element_is_not_a_pass():
    """A created id that does not resolve after commit must not read as
    ok - this is the exact 'Commit() said yes but nothing is there'
    failure mode the create_* post-conditions exist to catch."""
    doc = _FakeDoc({1: _FakeElement(100)})  # id 2 was never actually created
    result = utils.verify_created_elements(doc, [(1, 100), (2, 100)])
    assert result["ok"] is False
    assert result["failures"] == [{"id": 2, "reason": "does not resolve after commit"}]


def test_verify_created_elements_wrong_category_is_not_a_pass():
    """The element exists, but it's the wrong kind of thing - e.g. a
    create_room_separation call whose id resolves to something that isn't
    actually an OST_RoomSeparationLines element. Existence alone must not
    be treated as verification."""
    doc = _FakeDoc({1: _FakeElement(999)})  # created, but wrong category
    result = utils.verify_created_elements(doc, [(1, 100)])
    assert result["ok"] is False
    assert result["failures"][0]["reason"] == "category mismatch"
    assert result["failures"][0]["expected_category"] == 100
    assert result["failures"][0]["actual_category"] == 999


def test_verify_created_elements_empty_batch_is_not_checked_not_a_pass():
    """Nothing was created at all - this must be an honest not_checked
    (ok: None), never silently rendered as ok: True just because there
    was nothing to fail on."""
    doc = _FakeDoc({})
    result = utils.verify_created_elements(doc, [])
    assert result["ok"] is None
    assert result["ok"] is not True
    assert result.get("status") == "not_checked"


def test_verify_created_elements_none_category_skips_category_check():
    """Passing None as the expected category means 'only check existence',
    not 'automatically pass' - an element that doesn't resolve must still
    fail even when no category was specified for it."""
    doc = _FakeDoc({})
    result = utils.verify_created_elements(doc, [(1, None)])
    assert result["ok"] is False
