# -*- coding: utf-8 -*-
"""
Characterization + regression tests for the three new shared post-condition
helpers in revit_mcp/utils.py (verify_elements_exist, verify_element_named,
verify_file_written) - Milestone 3 of the M1-M5 architecture upgrade.

These consolidate 8 near-identical inline blocks that existed across
annotation.py, tags.py, interop.py (x2), transforms.py, mep.py,
view_management.py, and documentation.py. Per this milestone's own rule
(characterize -> consolidate -> verify, never rewrite-and-hope), each test
here documents the EXACT observable shape each original inline block
produced, verified by reading every original call site before writing this
file (see the M3 section of the implementation report for the per-site
comparison). Two small, deliberate, noted behavior changes are called out
explicitly below rather than silently characterized as "correct":
  - failures is now capped at 50 everywhere (three of the four
    element_exists sites had no cap before)
  - make_element_id (not a bare DB.ElementId(int)) is used uniformly (three
    of the four element_exists sites used the bare form before)

Also verifies the fail-closed rule directly: verification must never report
success when it could not actually check something.
"""
from conftest_revit_mcp import import_revit_mcp_utils, FakeDoc

utils = import_revit_mcp_utils()


# ---------------------------------------------------------------------------
# verified_ok / verified_failed / verified_not_checked - the three builders
# ---------------------------------------------------------------------------

def test_verified_ok_shape():
    v = utils.verified_ok("element_exists", {"count": 2}, {"count_ok": 2})
    assert v == {
        "ok": True, "method": "element_exists",
        "expected": {"count": 2}, "actual": {"count_ok": 2},
    }


def test_verified_failed_shape_without_reason_or_failures():
    v = utils.verified_failed("element_exists", {"count": 2}, {"count_ok": 1})
    assert v == {
        "ok": False, "method": "element_exists",
        "expected": {"count": 2}, "actual": {"count_ok": 1},
    }
    assert "reason" not in v
    assert "failures" not in v


def test_verified_failed_shape_with_reason_and_failures():
    v = utils.verified_failed(
        "element_exists", {"count": 2}, {"count_ok": 1},
        reason="something", failures=[{"id": 5}],
    )
    assert v["reason"] == "something"
    assert v["failures"] == [{"id": 5}]


def test_verified_failed_caps_failures_at_50():
    many = [{"id": i} for i in range(200)]
    v = utils.verified_failed("element_exists", {}, {}, failures=many)
    assert len(v["failures"]) == 50


def test_verified_not_checked_shape():
    v = utils.verified_not_checked("no fixed contract")
    assert v == {"ok": None, "status": "not_checked", "reason": "no fixed contract"}


# ---------------------------------------------------------------------------
# verify_elements_exist - characterized from annotation.py/tags.py/
# interop.py/transforms.py's original inline blocks
# ---------------------------------------------------------------------------

def test_verify_elements_exist_all_resolve():
    doc = FakeDoc(existing_ids=[1, 2, 3])
    v = utils.verify_elements_exist(doc, [1, 2, 3])
    assert v["ok"] is True
    assert v["method"] == "element_exists"
    assert v["expected"] == {"count": 3}
    assert v["actual"] == {"count_ok": 3}
    assert "failures" not in v


def test_verify_elements_exist_some_missing():
    doc = FakeDoc(existing_ids=[1, 3])
    v = utils.verify_elements_exist(doc, [1, 2, 3])
    assert v["ok"] is False
    assert v["expected"] == {"count": 3}
    assert v["actual"] == {"count_ok": 2}
    assert v["failures"] == [{"id": 2}]


def test_verify_elements_exist_none_resolve():
    doc = FakeDoc(existing_ids=[])
    v = utils.verify_elements_exist(doc, [1, 2])
    assert v["ok"] is False
    assert v["actual"] == {"count_ok": 0}
    assert v["failures"] == [{"id": 1}, {"id": 2}]


def test_verify_elements_exist_empty_batch_is_not_checked():
    """Matches three of the four original inline blocks (annotation.py,
    tags.py, interop.py's None-id branch): an empty batch is ok=None, not
    a vacuous pass. transforms.py's copy branch never actually reached
    this case in the original code (guarded by `and new_element_ids`), so
    this is not an observable change for any real call site."""
    doc = FakeDoc(existing_ids=[])
    v = utils.verify_elements_exist(doc, [])
    assert v["ok"] is None
    assert v["status"] == "not_checked"


def test_verify_elements_exist_caps_failures_at_50_for_a_large_batch():
    """Deliberate, noted behavior change for transforms.py's copy branch,
    which previously capped at 50 (unchanged there) - and a genuine
    improvement for annotation.py/tags.py's batch checks, which previously
    had no cap at all."""
    doc = FakeDoc(existing_ids=[])
    ids = list(range(200))
    v = utils.verify_elements_exist(doc, ids)
    assert len(v["failures"]) == 50


def test_verify_elements_exist_empty_batch_uses_custom_reason():
    doc = FakeDoc(existing_ids=[])
    v = utils.verify_elements_exist(doc, [], empty_reason="No dimension was created")
    assert v["reason"] == "No dimension was created"


def test_verify_elements_exist_uses_make_element_id_not_bare_constructor():
    """Regression guard for the id-construction unification: a caller
    passing a numpy-int-like or string-numeric id must still resolve via
    the same path make_element_id itself supports."""
    doc = FakeDoc(existing_ids=[42])
    v = utils.verify_elements_exist(doc, ["42"])  # string, like JSON round-trips
    assert v["ok"] is True


# ---------------------------------------------------------------------------
# verify_element_named - characterized from mep.py/view_management.py
# ---------------------------------------------------------------------------

def test_verify_element_named_resolves_and_matches():
    v = utils.verify_element_named(object(), "Living Room", "Living Room", subject="Room")
    assert v["ok"] is True
    assert v["method"] == "element_exists_and_name"
    assert v["expected"] == {"name": "Living Room"}
    assert v["actual"] == {"name": "Living Room", "resolves": True}


def test_verify_element_named_resolves_but_name_mismatches():
    v = utils.verify_element_named(object(), "Living Room", "Kitchen", subject="View")
    assert v["ok"] is False
    assert v["actual"] == {"name": "Kitchen", "resolves": True}
    assert v["reason"] == "View does not resolve or its name does not match what was requested"


def test_verify_element_named_does_not_resolve():
    v = utils.verify_element_named(None, "Living Room", "", subject="System")
    assert v["ok"] is False
    assert v["actual"] == {"name": "", "resolves": False}
    assert v["reason"] == "System does not resolve or its name does not match what was requested"


def test_verify_element_named_default_subject_wording():
    v = utils.verify_element_named(None, "X", "")
    assert v["reason"] == "Element does not resolve or its name does not match what was requested"


# ---------------------------------------------------------------------------
# verify_file_written - characterized from documentation.py/interop.py
# ---------------------------------------------------------------------------

def test_verify_file_written_nonexistent_path():
    v = utils.verify_file_written("Z:\\definitely\\does\\not\\exist.pdf", min_bytes=1024)
    assert v["ok"] is False
    assert v["actual"]["exists"] is False
    assert v["reason"] == "Export reported success but the output file does not exist or is empty"


def test_verify_file_written_none_path_does_not_raise():
    """documentation.py's original guarded bool(file_path); interop.py's
    did not (would have raised inside its own try/except: pass, so already
    behaviorally a silent not-exists) - the shared helper guards explicitly
    for both callers now."""
    v = utils.verify_file_written(None, min_bytes=1024)
    assert v["ok"] is False
    assert v["actual"]["exists"] is False


def test_verify_file_written_real_small_file_below_threshold(tmp_path):
    p = tmp_path / "tiny.txt"
    p.write_bytes(b"x" * 100)  # under 1024 bytes -> size_kb truncates to 0
    v = utils.verify_file_written(str(p), min_bytes=1024)
    assert v["ok"] is False
    assert v["actual"]["exists"] is True
    assert v["actual"]["size_kb"] == 0


def test_verify_file_written_real_file_above_threshold(tmp_path):
    p = tmp_path / "real.pdf"
    p.write_bytes(b"x" * 2048)
    v = utils.verify_file_written(str(p), min_bytes=1024)
    assert v["ok"] is True
    assert v["actual"]["exists"] is True
    assert v["actual"]["size_kb"] == 2


def test_verify_file_written_default_min_bytes_is_permissive(tmp_path):
    """The helper's own default (min_bytes=1) is looser than what the two
    existing callers pass (1024) - documented explicitly so a future
    caller does not assume the two are the same."""
    p = tmp_path / "tiny.txt"
    p.write_bytes(b"x")
    v = utils.verify_file_written(str(p))
    assert v["ok"] is True


# ---------------------------------------------------------------------------
# Fail-closed rule, exercised directly against all three helpers
# ---------------------------------------------------------------------------

def test_fail_closed_element_lookup_that_raises_does_not_report_success():
    class _RaisingDoc(object):
        def GetElement(self, eid):
            raise RuntimeError("simulated Revit API failure")

    try:
        utils.verify_elements_exist(_RaisingDoc(), [1])
    except RuntimeError:
        pass  # propagating is acceptable; silently reporting ok=True is not
    else:
        raise AssertionError(
            "verify_elements_exist swallowed a lookup failure and returned "
            "normally - it must never do that silently"
        )


def test_fail_closed_missing_element_is_failed_not_ok():
    doc = FakeDoc(existing_ids=[])
    v = utils.verify_elements_exist(doc, [1])
    assert v["ok"] is False, "a missing element must never read as ok=True"


def test_fail_closed_unresolvable_named_element_is_failed_not_ok():
    v = utils.verify_element_named(None, "X", "X")  # name coincidentally "matches" but nothing resolved
    assert v["ok"] is False, "an unresolved element must never read as ok=True even if actual_name happens to equal expected_name"
