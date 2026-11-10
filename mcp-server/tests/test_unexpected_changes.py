# -*- coding: utf-8 -*-
"""
Offline tests for tools/plan_tools.py::_check_unexpected_changes.

The "unknown must never be upgraded to foreign" fixture below is copied
from a REAL delta captured live on 2026-09-06 during end-to-end MCP
verification (deltas/d-20260906194248.json on the test machine): moving
wall 1658696 caused an adjacent room's boundary to shift as a genuine side
effect, and the tracker's own attribute() correctly recorded that uid as
"unknown" (no covering DocumentChanged event tied to a named transaction),
NOT "human". The first version of _check_unexpected_changes collapsed
"unknown" into the same bucket as "human"/"sync_incoming" ("foreign"),
which is exactly the mistake this project's tracker.attribute() itself is
built never to make one layer down. This file is the regression guard.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.plan_tools import _check_unexpected_changes  # noqa: E402


@pytest.fixture
def tracker_dir(tmp_path, monkeypatch):
    d = tmp_path / "revit-tracker"
    d.mkdir()
    monkeypatch.setenv("REVIT_TRACKER_DIR", str(d))
    return d


def _write_last_run(tracker_dir, run, delta_file, items_capped=False):
    obj = {"run": run, "delta_file": str(delta_file), "items_capped": items_capped}
    (tracker_dir / "last_run.json").write_text(json.dumps(obj), encoding="utf-8")


def _write_delta(tracker_dir, name, attribution, items):
    path = tracker_dir / name
    path.write_text(json.dumps({"run": "r-x", "attribution": attribution, "items": items}), encoding="utf-8")
    return path


def _progress(baseline_run="r-baseline", touched_uids=None):
    return {"baseline_run": baseline_run, "actions": [{"touched_uids": touched_uids or []}]}


# ---------------------------------------------------------------------------
# no tracker output at all
# ---------------------------------------------------------------------------

def test_no_last_run_file_reports_unknown(tracker_dir):
    result = _check_unexpected_changes(str(tracker_dir), {}, _progress())
    assert result["status"] == "unknown"


def test_no_checkpoint_since_baseline_refuses_the_comparison(tracker_dir):
    _write_last_run(tracker_dir, "r-same", tracker_dir / "nonexistent.json")
    result = _check_unexpected_changes(str(tracker_dir), {}, _progress(baseline_run="r-same"))
    assert result["status"] == "no_checkpoint_since_baseline"


def test_missing_delta_file_is_itself_a_finding(tracker_dir):
    _write_last_run(tracker_dir, "r-new", tracker_dir / "does_not_exist.json")
    result = _check_unexpected_changes(str(tracker_dir), {}, _progress(baseline_run="r-old"))
    assert result["status"] == "no_delta"


# ---------------------------------------------------------------------------
# the real, live-measured fixture -- the regression guard
# ---------------------------------------------------------------------------

REAL_ROOM_ITEM = {
    "cat": "Rooms", "fields": ["geo"], "id": "1660259", "kind": "modified",
    "lvl": "L1", "typ": None, "moved_ft": 0.6562,
    "geo_from": ["P", 5.9055, 26.9029, 0.0, 0.0], "geo_to": ["P", 5.2493, 26.9029, 0.0, 0.0],
    "uid": "818807a5-a8db-4fd2-bab0-9aebb88b2205-00195563",
}
REAL_ATTRIBUTION = {"818807a5-a8db-4fd2-bab0-9aebb88b2205-00195563": "unknown"}


def test_unknown_attribution_is_unexpected_not_foreign(tracker_dir):
    """The regression guard itself: 'unknown' must land in unexpected,
    never foreign - foreign means positive evidence of human/sync
    involvement, which the tracker explicitly did NOT have here."""
    delta_path = _write_delta(tracker_dir, "delta.json", REAL_ATTRIBUTION, [REAL_ROOM_ITEM])
    _write_last_run(tracker_dir, "r-new", delta_path)
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, _progress(baseline_run="r-old"))
    assert result["status"] == "compared"
    assert result["foreign_count"] == 0
    assert result["unexpected_count"] == 1
    assert result["unexpected"][0]["uid"] == REAL_ROOM_ITEM["uid"]


def test_unknown_attribution_becomes_side_effect_when_tolerated(tracker_dir):
    """The same real item, but the plan declared Rooms as a tolerated side
    effect - now it should be neither foreign nor unexpected."""
    delta_path = _write_delta(tracker_dir, "delta.json", REAL_ATTRIBUTION, [REAL_ROOM_ITEM])
    _write_last_run(tracker_dir, "r-new", delta_path)
    plan = {"expected_changes": {"tolerated_side_effects": ["Rooms"]}}
    result = _check_unexpected_changes(str(tracker_dir), plan, _progress(baseline_run="r-old"))
    assert result["side_effect_count"] == 1
    assert result["unexpected_count"] == 0
    assert result["foreign_count"] == 0


# ---------------------------------------------------------------------------
# the other three classification buckets
# ---------------------------------------------------------------------------

def test_touched_uid_is_accounted(tracker_dir):
    item = dict(REAL_ROOM_ITEM)
    delta_path = _write_delta(tracker_dir, "delta.json", {item["uid"]: "claude"}, [item])
    _write_last_run(tracker_dir, "r-new", delta_path)
    progress = _progress(baseline_run="r-old", touched_uids=[item["uid"]])
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, progress)
    assert result["accounted_count"] == 1
    assert result["unexpected_count"] == 0


def test_expected_id_is_accounted_even_if_not_directly_touched(tracker_dir):
    item = dict(REAL_ROOM_ITEM)
    delta_path = _write_delta(tracker_dir, "delta.json", {item["uid"]: "claude"}, [item])
    _write_last_run(tracker_dir, "r-new", delta_path)
    plan = {"expected_changes": {"modified": [1660259]}}
    result = _check_unexpected_changes(str(tracker_dir), plan, _progress(baseline_run="r-old"))
    assert result["accounted_count"] == 1


def test_human_attribution_is_genuinely_foreign(tracker_dir):
    item = dict(REAL_ROOM_ITEM)
    delta_path = _write_delta(tracker_dir, "delta.json", {item["uid"]: "human"}, [item])
    _write_last_run(tracker_dir, "r-new", delta_path)
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, _progress(baseline_run="r-old"))
    assert result["foreign_count"] == 1
    assert result["unexpected_count"] == 0


def test_sync_incoming_attribution_is_genuinely_foreign(tracker_dir):
    item = dict(REAL_ROOM_ITEM)
    delta_path = _write_delta(tracker_dir, "delta.json", {item["uid"]: "sync_incoming"}, [item])
    _write_last_run(tracker_dir, "r-new", delta_path)
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, _progress(baseline_run="r-old"))
    assert result["foreign_count"] == 1


def test_claude_attributed_but_undeclared_change_is_unexpected(tracker_dir):
    """Attributed to us, but not a touched uid, not an expected id, not a
    tolerated category - genuinely unexpected, not swept into 'accounted'
    just because attribution says claude."""
    item = dict(REAL_ROOM_ITEM)
    delta_path = _write_delta(tracker_dir, "delta.json", {item["uid"]: "claude"}, [item])
    _write_last_run(tracker_dir, "r-new", delta_path)
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, _progress(baseline_run="r-old"))
    assert result["unexpected_count"] == 1
    assert result["accounted_count"] == 0
    assert result["foreign_count"] == 0


# ---------------------------------------------------------------------------
# truncation flag
# ---------------------------------------------------------------------------

def test_items_capped_flag_is_surfaced_as_truncated(tracker_dir):
    delta_path = _write_delta(tracker_dir, "delta.json", {}, [])
    _write_last_run(tracker_dir, "r-new", delta_path, items_capped=True)
    result = _check_unexpected_changes(str(tracker_dir), {"expected_changes": {}}, _progress(baseline_run="r-old"))
    assert result["truncated"] is True
