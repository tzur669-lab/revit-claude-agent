# -*- coding: utf-8 -*-
"""
Unit tests for tracker.py's pure logic - the diff, attribution, and
identity-normalization functions that carry the most intricate reasoning
in the project and were, until Milestone 2's Python-3 syntax fix,
protected by nothing at all (the file could not even be imported by a
test runner). No Revit is required: none of the functions tested here
touch doc/DB/revit/System at the paths exercised.
"""
import os
import sys

TRACKER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "memory-system", "tracker",
)
sys.path.insert(0, TRACKER_DIR)
import tracker  # noqa: E402


# ---------------------------------------------------------------------------
# diff_snapshots
# ---------------------------------------------------------------------------

def _rest(cat="1", typ="2", lvl="3", geo="g1", par="p1", elid="100", blob="{}"):
    """Build one snapshot row in load_tsv's format: uid maps to everything
    AFTER the uid, i.e. cat\ttyp\tlvl\tgeo\tpar\telid\tblob (7 fields) -
    matching tsv_line()'s output with the leading uid stripped."""
    return u"\t".join([cat, typ, lvl, geo, par, elid, blob])


def test_diff_snapshots_detects_added_and_deleted():
    prev = {"uidA": _rest()}
    cur = {"uidB": _rest()}
    d = tracker.diff_snapshots(prev, cur)
    assert d["added"] == ["uidB"]
    assert d["deleted"] == ["uidA"]
    assert d["modified"] == []


def test_diff_snapshots_detects_modified_field():
    prev = {"uid1": _rest(geo="g1")}
    cur = {"uid1": _rest(geo="g2")}
    d = tracker.diff_snapshots(prev, cur)
    assert d["added"] == [] and d["deleted"] == []
    assert len(d["modified"]) == 1
    assert d["modified"][0]["uid"] == "uid1"
    assert d["modified"][0]["fields"] == ["geo"]


def test_diff_snapshots_identical_rows_produce_no_diff():
    row = _rest()
    prev = {"uid1": row}
    cur = {"uid1": row}
    d = tracker.diff_snapshots(prev, cur)
    assert d == {"added": [], "deleted": [], "modified": []}


def test_diff_snapshots_blob_only_change_is_not_a_diff():
    """The precedent this project relies on: the pv-blob (field index 6)
    was enriched without a SNAPSHOT_FORMAT bump specifically because
    diff_snapshots excludes it from comparison. If this regresses, every
    blob-only enrichment becomes a phantom mass-modification event."""
    prev = {"uid1": _rest(blob="{}")}
    cur = {"uid1": _rest(blob='{"pv": {"some": "enriched data"}}')}
    d = tracker.diff_snapshots(prev, cur)
    assert d == {"added": [], "deleted": [], "modified": []}, (
        "a change confined to the blob field must never appear as a diff"
    )


def test_diff_snapshots_element_id_only_change_is_not_a_diff():
    """Same principle, different field: elid (index 5) moving alone - e.g.
    an element recreated with a new ElementId but the same UniqueId and
    otherwise identical content - must not read as a modification."""
    prev = {"uid1": _rest(elid="100")}
    cur = {"uid1": _rest(elid="999")}
    d = tracker.diff_snapshots(prev, cur)
    assert d == {"added": [], "deleted": [], "modified": []}


def test_diff_snapshots_multiple_field_changes_are_all_reported():
    prev = {"uid1": _rest(cat="1", geo="g1")}
    cur = {"uid1": _rest(cat="9", geo="g2")}
    d = tracker.diff_snapshots(prev, cur)
    assert set(d["modified"][0]["fields"]) == {"cat", "geo"}


# ---------------------------------------------------------------------------
# attribute()
# ---------------------------------------------------------------------------

def _diff(added=(), deleted=(), modified=()):
    return {
        "added": list(added),
        "deleted": list(deleted),
        "modified": [{"uid": u} for u in modified],
    }


def test_attribute_claude_state():
    diff = _diff(modified=["uidA"])
    cur_ids = {"100": "uidA"}
    events = [{"ours": True, "m": [100], "tx": ["MCP Code Execution: x"]}]
    by, counts = tracker.attribute(diff, cur_ids, {}, events)
    assert by["uidA"]["by"] == "claude"
    assert counts == {"claude": 1, "human": 0, "sync_incoming": 0, "unknown": 0}


def test_attribute_human_state():
    diff = _diff(modified=["uidA"])
    cur_ids = {"100": "uidA"}
    events = [{"ours": False, "sync": False, "m": [100], "tx": ["Move"]}]
    by, counts = tracker.attribute(diff, cur_ids, {}, events)
    assert by["uidA"]["by"] == "human"
    assert by["uidA"]["tx"] == ["Move"]
    assert counts["human"] == 1


def test_attribute_sync_incoming_state():
    diff = _diff(modified=["uidA"])
    cur_ids = {"100": "uidA"}
    events = [{"ours": False, "sync": True, "m": [100], "tx": []}]
    by, counts = tracker.attribute(diff, cur_ids, {}, events)
    assert by["uidA"]["by"] == "sync_incoming"
    assert counts["sync_incoming"] == 1


def test_attribute_unknown_state_when_no_event_covers_it():
    """The central non-negotiable rule: an element with no covering event
    is unknown, and unknown must NEVER be upgraded to human by guessing -
    this is the one mistake the whole system is built to avoid."""
    diff = _diff(modified=["uidA"])
    by, counts = tracker.attribute(diff, {"100": "uidA"}, {}, events=[])
    assert by["uidA"]["by"] == "unknown"
    assert by["uidA"]["by"] != "human"
    assert counts["unknown"] == 1


def test_attribute_deleted_element_resolved_via_prev_ids_not_cur_ids():
    """A deleted element is gone from the current snapshot, so it can only
    be named through prev_ids - cur_ids must not be consulted for it."""
    diff = _diff(deleted=["uidGone"])
    prev_ids = {"100": "uidGone"}
    cur_ids = {}  # deliberately does not contain uidGone
    events = [{"ours": True, "d": [100], "tx": ["MCP Delete Elements"]}]
    by, counts = tracker.attribute(diff, cur_ids, prev_ids, events)
    assert by["uidGone"]["by"] == "claude"


def test_attribute_later_event_wins():
    """Claude created it, then a human moved it - the human's move is the
    attribution that should stick, matching the actual sequence of events."""
    diff = _diff(modified=["uidA"])
    cur_ids = {"100": "uidA"}
    events = [
        {"ours": True, "m": [100], "tx": ["MCP Create Room"]},
        {"ours": False, "sync": False, "m": [100], "tx": ["Move"]},
    ]
    by, counts = tracker.attribute(diff, cur_ids, {}, events)
    assert by["uidA"]["by"] == "human"
    assert by["uidA"]["tx"] == ["Move"]


def test_attribute_derived_copy_with_no_tx_falls_to_unknown():
    """A camera/view-pair element that changes as a side effect can have
    ours truthy with no matching id at all covering it in cur_ids - the
    id-to-uid bridge simply has nothing for it, so it must land in
    unknown, not be guessed as human or claude."""
    diff = _diff(modified=["uidCamera"])
    cur_ids = {}  # uidCamera's element id never appears in the index
    events = [{"ours": True, "m": [999], "tx": ["Move"]}]  # unrelated id
    by, counts = tracker.attribute(diff, cur_ids, {}, events)
    assert by["uidCamera"]["by"] == "unknown"


# ---------------------------------------------------------------------------
# canon_path / safe_folder_name
# ---------------------------------------------------------------------------

def test_canon_path_empty_string():
    assert tracker.canon_path("") == u""
    assert tracker.canon_path(None) == u""


def test_canon_path_falls_back_to_lower_without_system(monkeypatch):
    """canon_path prefers System.IO.Path.GetFullPath but must degrade
    gracefully to a plain .lower() when System isn't available - which is
    exactly this test environment (no Revit host injecting System)."""
    assert "System" not in tracker.__dict__ or True  # documents the assumption
    result = tracker.canon_path("C:\\Some\\MixedCase\\Path.rvt")
    assert result == "c:\\some\\mixedcase\\path.rvt"


def test_safe_folder_name_strips_illegal_characters():
    assert tracker.safe_folder_name(u'a<b>c:d"e/f\\g|h?i*j') == u"a_b_c_d_e_f_g_h_i_j"


def test_safe_folder_name_strips_control_characters():
    assert tracker.safe_folder_name(u"a\x01b\x1fc") == u"a_b_c"


def test_safe_folder_name_caps_length_at_60():
    long_name = u"x" * 200
    result = tracker.safe_folder_name(long_name)
    assert len(result) <= 60


def test_safe_folder_name_empty_input_falls_back_to_project():
    assert tracker.safe_folder_name(u"") == u"project"
    assert tracker.safe_folder_name(u"   ") == u"project"


def test_safe_folder_name_preserves_hebrew():
    """Explicitly documented in the source: Hebrew is allowed here, this
    is a data path (a folder name shown to the user), not source code."""
    assert tracker.safe_folder_name(u"פרויקט 01") == u"פרויקט 01"


# ---------------------------------------------------------------------------
# tsv_line / load_tsv round-trip
# ---------------------------------------------------------------------------

def test_tsv_line_round_trips_through_load_tsv(monkeypatch):
    """load_tsv's own I/O (read_lines) goes through System.IO.File, which
    silently fails-closed to None outside a Revit host (file_exists()
    catches the NameError from a missing System and returns False, so
    read_lines never even tries to read). Mocking read_lines - the I/O
    boundary - rather than faking System.IO itself tests load_tsv's real
    parsing logic (split on the first tab, build the uid->rest dict)
    without needing to fake the whole System.IO surface for one test."""
    line = tracker.tsv_line("uid-1", "cat", "typ", "lvl", "geo", "par", "100", "{}")
    monkeypatch.setattr(tracker, "read_lines", lambda path: [line])
    loaded = tracker.load_tsv("irrelevant-path")
    assert loaded == {"uid-1": "cat\ttyp\tlvl\tgeo\tpar\t100\t{}"}


def test_load_tsv_returns_none_when_file_absent(monkeypatch):
    monkeypatch.setattr(tracker, "read_lines", lambda path: None)
    assert tracker.load_tsv("does-not-exist") is None


def test_tsv_line_rejects_embedded_newline():
    import pytest
    with pytest.raises(tracker.TrackerAbort):
        tracker.tsv_line("uid-1", "cat\n", "typ", "lvl", "geo", "par", "100", "{}")


def test_tsv_line_rejects_wrong_field_count():
    """A sanity guard on tsv_line itself: exactly 7 tabs (8 fields) or it
    aborts loudly rather than silently writing a corrupt snapshot row."""
    import pytest
    # Sneaking an extra tab into a field would silently shift every field
    # after it - tsv_line must catch this by counting tabs, not trust the
    # caller passed clean fields.
    with pytest.raises(tracker.TrackerAbort):
        tracker.tsv_line("uid-1", "cat\textra", "typ", "lvl", "geo", "par", "100", "{}")


# ---------------------------------------------------------------------------
# geo_distance
# ---------------------------------------------------------------------------

def test_geo_distance_point_type():
    a = ["P", 0.0, 0.0, 0.0, 0.0]
    b = ["P", 3.0, 4.0, 0.0, 0.0]
    assert tracker.geo_distance(a, b) == 5.0


def test_geo_distance_mismatched_geometry_type_returns_none():
    a = ["P", 0.0, 0.0, 0.0, 0.0]
    b = ["C", 0.0, 0.0, 0.0, 0.0, 0.0]
    assert tracker.geo_distance(a, b) is None


def test_geo_distance_none_input_returns_none():
    assert tracker.geo_distance(None, ["P", 0, 0, 0, 0]) is None
    assert tracker.geo_distance(["P", 0, 0, 0, 0], None) is None
