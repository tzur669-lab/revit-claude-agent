# -*- coding: utf-8 -*-
"""
Offline tests for revit_mcp/task_store.py -- zero Revit dependency, so no
conftest_revit_mcp.py fixtures are needed; this module is imported directly,
exactly as production CPython code (tools/plan_tools.py) and the IronPython
route handlers both will.
"""
import ast
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from revit_mcp import task_store as ts  # noqa: E402


TASK_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "revit_mcp", "task_store.py",
)


@pytest.fixture
def task_dir(tmp_path, monkeypatch):
    root = str(tmp_path / "tasks")
    monkeypatch.setattr(ts, "TASKS_DIR", root)
    d = os.path.join(root, "t-test-0001")
    ts._ensure_dir(d)
    return d


# ---------------------------------------------------------------------------
# AST guard -- the measured UTC+3 trap must never come back
# ---------------------------------------------------------------------------

def test_never_calls_datetime_parse():
    with open(TASK_STORE_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=TASK_STORE_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "Parse":
            pytest.fail(
                "task_store.py calls .Parse(...) somewhere -- System.DateTime.Parse "
                "on a 'Z'-suffixed string returns LOCAL time, making a stale lock "
                "look permanently fresh on any machine ahead of UTC. Use epoch_now()."
            )
    # The AST walk above is the real guard (code, not prose) -- it correctly
    # ignores this module's own comment explaining why .Parse is avoided.


# ---------------------------------------------------------------------------
# atomic write / read-with-fallback
# ---------------------------------------------------------------------------

def test_write_json_atomic_round_trips(task_dir):
    p = os.path.join(task_dir, "x.json")
    ts.write_json_atomic(p, {"a": 1, "heb": u"\u05e7\u05e8\u05e7\u05e2"})
    obj, src = ts.read_json_with_fallback(p)
    assert src == "primary"
    assert obj["a"] == 1
    assert obj["heb"] == u"\u05e7\u05e8\u05e7\u05e2"


def test_write_json_atomic_is_pure_ascii_on_disk(task_dir):
    p = os.path.join(task_dir, "x.json")
    ts.write_json_atomic(p, {"heb": u"\u05e9\u05dc\u05d5\u05dd"})
    with open(p, "rb") as f:
        raw = f.read()
    assert all(b < 128 for b in raw)  # ensure_ascii=True -- \\uXXXX escapes only
    assert b"\\u05e9" in raw


def test_read_falls_back_to_bak_and_declares_it(task_dir):
    p = os.path.join(task_dir, "x.json")
    ts.write_json_atomic(p, {"v": 1})
    ts.write_json_atomic(p, {"v": 2})  # first write becomes .bak
    with open(p, "w") as f:
        f.write("{not valid json")
    obj, src = ts.read_json_with_fallback(p)
    assert src == "bak"
    assert obj["v"] == 1


def test_read_neither_parses_returns_none_not_a_guess(task_dir):
    p = os.path.join(task_dir, "nope.json")
    obj, src = ts.read_json_with_fallback(p)
    assert obj is None
    assert src == "none"


def test_interrupted_write_leaves_original_intact(task_dir):
    p = os.path.join(task_dir, "x.json")
    ts.write_json_atomic(p, {"v": "original"})
    # simulate a crash mid-write: a .tmp file with no completed replace
    with open(p + ".tmp", "w") as f:
        f.write("{incomplete")
    obj, src = ts.read_json_with_fallback(p)
    assert src == "primary"
    assert obj["v"] == "original"


# ---------------------------------------------------------------------------
# ledger: append, monotonic seq, corruption handling
# ---------------------------------------------------------------------------

def test_append_ledger_assigns_monotonic_seq(task_dir):
    r1 = ts.append_ledger(task_dir, {"kind": "note", "msg": "a"})
    r2 = ts.append_ledger(task_dir, {"kind": "note", "msg": "b"})
    r3 = ts.append_ledger(task_dir, {"kind": "note", "msg": "c"})
    assert [r1["seq"], r2["seq"], r3["seq"]] == [1, 2, 3]
    records, meta = ts.read_ledger(task_dir)
    assert [r["seq"] for r in records] == [1, 2, 3]
    assert meta == {"truncated_tail": False, "stopped_at_line": None}


def test_append_ledger_stamps_lv_and_t(task_dir):
    r = ts.append_ledger(task_dir, {"kind": "note"})
    assert r["lv"] == ts.LEDGER_SCHEMA_V
    assert "t" in r


def test_trailing_partial_line_is_dropped_and_noted(task_dir):
    ts.append_ledger(task_dir, {"kind": "note", "msg": "a"})
    ts.append_ledger(task_dir, {"kind": "note", "msg": "b"})
    with open(ts.ledger_path(task_dir), "a", encoding="utf-8") as f:
        f.write('{"kind": "note", "msg": "trunc')  # no closing, no newline
    records, meta = ts.read_ledger(task_dir)
    assert len(records) == 2
    assert meta["truncated_tail"] is True
    assert meta["stopped_at_line"] is None


def test_mid_file_corruption_stops_there_not_after(task_dir):
    ts.append_ledger(task_dir, {"kind": "note", "msg": "a"})
    with open(ts.ledger_path(task_dir), "a", encoding="utf-8") as f:
        f.write("not json at all\n")
    ts.append_ledger(task_dir, {"kind": "note", "msg": "c"})
    records, meta = ts.read_ledger(task_dir)
    assert len(records) == 1  # only "a" -- corruption is in the middle, not the tail
    assert meta["truncated_tail"] is False
    assert meta["stopped_at_line"] == 1


def test_next_seq_survives_a_corrupted_tail(task_dir):
    ts.append_ledger(task_dir, {"kind": "note"})
    with open(ts.ledger_path(task_dir), "a", encoding="utf-8") as f:
        f.write('{"kind": "note", "seq": 99, "incomple')
    r = ts.append_ledger(task_dir, {"kind": "note"})
    assert r["seq"] == 2  # based on the last VALID record (seq=1), not the garbage


def test_find_open_action_detects_begin_without_end(task_dir):
    ts.append_ledger(task_dir, {"kind": "action_begin", "i": 0, "op": "move_elements"})
    open_action = ts.find_open_action(task_dir)
    assert open_action is not None
    assert open_action["i"] == 0
    ts.append_ledger(task_dir, {"kind": "action_end", "i": 0, "status": "done"})
    assert ts.find_open_action(task_dir) is None


def test_find_open_action_flags_more_than_one_as_corrupt(task_dir):
    ts.append_ledger(task_dir, {"kind": "action_begin", "i": 0})
    ts.append_ledger(task_dir, {"kind": "action_begin", "i": 1})
    result = ts.find_open_action(task_dir)
    assert result["kind"] == "corrupt"
    assert result["open_indices"] == [0, 1]


# ---------------------------------------------------------------------------
# created_ids_by_action -- Phase B slice 2's "created_by" resolution source
# ---------------------------------------------------------------------------

def test_created_ids_by_action_single_id():
    records = [{"kind": "action_end", "i": 0, "status": "done", "created_ids": [1660300]}]
    assert ts.created_ids_by_action(records) == {0: [1660300]}


def test_created_ids_by_action_multi_id_create_room_separation_shape():
    records = [{"kind": "action_end", "i": 0, "status": "done", "created_ids": [101, 102, 103]}]
    assert ts.created_ids_by_action(records) == {0: [101, 102, 103]}


def test_created_ids_by_action_skipped_with_recovered_ids_counts():
    """create_room's probe-recovery fix: a crash-resume skip CAN carry a
    real recovered id, and it must be treated identically to a normal run."""
    records = [{"kind": "action_end", "i": 0, "status": "skipped", "created_ids": [1660300]}]
    assert ts.created_ids_by_action(records) == {0: [1660300]}


def test_created_ids_by_action_skipped_without_recovered_ids_is_empty():
    """The other three creating ops' case: probe always ("unknown", None),
    so a crash-resume skip contributes nothing -- honest, not a guess."""
    records = [{"kind": "action_end", "i": 0, "status": "skipped", "created_ids": []}]
    assert ts.created_ids_by_action(records) == {0: []}


def test_created_ids_by_action_excludes_failed_and_unrelated_kinds():
    records = [
        {"kind": "action_end", "i": 0, "status": "failed", "created_ids": [999]},
        {"kind": "action_begin", "i": 1, "created_ids": [888]},  # wrong kind, never counted
        {"kind": "action_end", "i": 2, "status": "done", "created_ids": [1660300]},
    ]
    assert ts.created_ids_by_action(records) == {2: [1660300]}


def test_created_ids_by_action_non_creating_action_contributes_empty_list():
    """move_elements/set_parameters etc. always report created_ids: []
    (ops.py's own convention) -- present in the map with an empty list,
    not absent, so a caller can distinguish "this action ran and created
    nothing" from "this action_i doesn't exist in the ledger at all"."""
    records = [{"kind": "action_end", "i": 0, "status": "done", "created_ids": []}]
    assert ts.created_ids_by_action(records) == {0: []}


def test_created_ids_by_action_later_record_for_same_i_wins():
    """Defensive determinism, matching find_open_action's own established
    last-one-wins rule for 'more than one record for the same i' -- never
    actually reachable in a non-corrupt ledger (at most one action_end per
    i), but the rule must still be deterministic if it ever is."""
    records = [
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [111], "seq": 5},
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [222], "seq": 9},
    ]
    assert ts.created_ids_by_action(records) == {0: [222]}


# ---------------------------------------------------------------------------
# commit_result_evidence -- Phase B slice 3's Level-1 authoritative
# evidence. Never "last one wins": a disagreement is a conflict, not a
# tiebreak.
# ---------------------------------------------------------------------------

def test_commit_result_evidence_absent_when_none_recorded():
    assert ts.commit_result_evidence([], 0) == ("absent", None, None)
    records = [{"kind": "action_end", "i": 0, "status": "done", "created_ids": [1]}]
    assert ts.commit_result_evidence(records, 0) == ("absent", None, None)


def test_commit_result_evidence_single_record_is_ok():
    records = [{"kind": "commit_result", "i": 0, "op": "create_room",
                "created_ids": [1660300], "touched_ids": []}]
    kind, payload, reason = ts.commit_result_evidence(records, 0)
    assert kind == "ok"
    assert reason is None
    assert payload == {"op": "create_room", "created_ids": [1660300], "touched_ids": []}


def test_commit_result_evidence_empty_created_ids_is_ok_not_absent():
    """A committed creating action that produced nothing is authoritative
    evidence too -- must not be conflated with 'no Level-1 evidence
    exists at all' (B5)."""
    records = [{"kind": "commit_result", "i": 0, "op": "create_room_separation",
                "created_ids": [], "touched_ids": []}]
    kind, payload, _ = ts.commit_result_evidence(records, 0)
    assert kind == "ok"
    assert payload["created_ids"] == []


def test_commit_result_evidence_duplicate_identical_records_is_ok():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
    ]
    kind, payload, reason = ts.commit_result_evidence(records, 0)
    assert kind == "ok"
    assert reason is None
    assert payload["created_ids"] == [5]


def test_commit_result_evidence_conflicting_created_ids_fails_closed():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [6], "touched_ids": []},
    ]
    assert ts.commit_result_evidence(records, 0) == ("conflict", None, "commit_result_conflict")


def test_commit_result_evidence_conflicting_touched_ids_fails_closed():
    records = [
        {"kind": "commit_result", "i": 0, "op": "move_elements", "created_ids": [], "touched_ids": [1]},
        {"kind": "commit_result", "i": 0, "op": "move_elements", "created_ids": [], "touched_ids": [2]},
    ]
    kind, _, reason = ts.commit_result_evidence(records, 0)
    assert kind == "conflict" and reason == "commit_result_conflict"


def test_commit_result_evidence_op_mismatch_fails_closed():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "commit_result", "i": 0, "op": "place_family", "created_ids": [5], "touched_ids": []},
    ]
    kind, _, reason = ts.commit_result_evidence(records, 0)
    assert kind == "conflict" and reason == "commit_result_conflict"


def test_commit_result_evidence_scoped_to_the_requested_action_index():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [1], "touched_ids": []},
        {"kind": "commit_result", "i": 1, "op": "place_family", "created_ids": [2], "touched_ids": []},
    ]
    assert ts.commit_result_evidence(records, 1)[1]["created_ids"] == [2]
    assert ts.commit_result_evidence(records, 5) == ("absent", None, None)


# ---------------------------------------------------------------------------
# ledger_conflicts -- surfaced anomaly channel, agreement is NOT reported
# ---------------------------------------------------------------------------

def test_ledger_conflicts_empty_when_nothing_present():
    assert ts.ledger_conflicts([]) == []


def test_ledger_conflicts_agreement_between_commit_result_and_action_end_is_silent():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [5]},
    ]
    assert ts.ledger_conflicts(records) == []


def test_ledger_conflicts_disagreement_between_commit_result_and_action_end_is_reported():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [6]},
    ]
    conflicts = ts.ledger_conflicts(records)
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "commit_result_vs_action_end"
    assert conflicts[0]["commit_result_ids"] == [5]
    assert conflicts[0]["action_end_ids"] == [6]


def test_ledger_conflicts_commit_result_only_no_action_end_yet_is_silent():
    """The normal open-action window: a commit_result exists but the
    action hasn't finished (no action_end yet) -- not a conflict, just
    incomplete, and must not be reported as one."""
    records = [{"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []}]
    assert ts.ledger_conflicts(records) == []


def test_ledger_conflicts_multiple_disagreeing_commit_results_reported_as_conflict():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [6], "touched_ids": []},
    ]
    conflicts = ts.ledger_conflicts(records)
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "commit_result_conflict"


def test_ledger_conflicts_multiple_actions_only_disagreeing_one_reported():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [1], "touched_ids": []},
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [1]},
        {"kind": "commit_result", "i": 1, "op": "place_family", "created_ids": [2], "touched_ids": []},
        {"kind": "action_end", "i": 1, "status": "done", "created_ids": [999]},
    ]
    conflicts = ts.ledger_conflicts(records)
    assert len(conflicts) == 1
    assert conflicts[0]["i"] == 1


def test_commit_result_in_known_ledger_kinds():
    assert "commit_result" in ts.KNOWN_LEDGER_KINDS


def test_fold_progress_surfaces_ledger_conflicts_field():
    records = [
        {"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []},
        {"kind": "action_end", "i": 0, "status": "done", "created_ids": [6]},
    ]
    progress = ts.fold_progress(records)
    assert progress["ledger_conflicts"] and progress["ledger_conflicts"][0]["kind"] == "commit_result_vs_action_end"


def test_fold_progress_ledger_conflicts_empty_for_a_clean_ledger():
    progress = ts.fold_progress(_sample_records())
    assert progress["ledger_conflicts"] == []


def test_fold_progress_does_not_flag_commit_result_as_an_unknown_kind():
    records = [{"kind": "commit_result", "i": 0, "op": "create_room", "created_ids": [5], "touched_ids": []}]
    progress = ts.fold_progress(records)
    assert "commit_result" not in progress["unknown_ledger_kinds"]


# ---------------------------------------------------------------------------
# projection fold -- determinism, unknown kinds, verdict rollup
# ---------------------------------------------------------------------------

def _sample_records():
    return [
        {"kind": "task_meta", "task_id": "t-x", "plan_hash": "abc123", "canon": "c:\\x.rvt"},
        {"kind": "state", "from": None, "to": "proposed", "ok": True},
        {"kind": "state", "from": "proposed", "to": "approved", "ok": True},
        {"kind": "state", "from": "approved", "to": "running", "ok": True},
        {"kind": "slice_open", "n": 1, "group": "MCP Plan Slice: t-x #1", "budget_ms": 20000},
        {"kind": "action_begin", "i": 0, "op": "move_elements", "phase": 0},
        {"kind": "stage", "i": 0, "stage": "applying"},
        {"kind": "action_end", "i": 0, "status": "done", "tx_status": "Committed",
         "execution_ok": True, "verified": {"ok": True}, "touched_ids": [1], "touched_uids": ["u1"]},
        {"kind": "slice_close", "n": 1, "slice_atomicity": "assimilated", "elapsed_ms": 500.0},
        {"kind": "state", "from": "running", "to": "finished", "ok": True, "outcome": "completed"},
    ]


def test_fold_is_deterministic():
    records = _sample_records()
    p1 = ts.fold_progress(records)
    p2 = ts.fold_progress(records)
    p1.pop("updated_epoch")
    p2.pop("updated_epoch")
    assert p1 == p2


def test_fold_replay_matches_incremental_projection(task_dir):
    for rec in _sample_records():
        ts.append_ledger(task_dir, rec)
    records, _ = ts.read_ledger(task_dir)
    replayed = ts.fold_progress(records)
    assert replayed["lifecycle"] == "finished"
    assert replayed["outcome"] == "completed"
    assert replayed["task_id"] == "t-x"
    assert replayed["plan_hash"] == "abc123"
    assert replayed["counts"] == {"total": 1, "done": 1, "failed": 0, "skipped": 0, "pending": 0}
    assert replayed["verdicts"]["execution_ok"] is True
    assert replayed["verdicts"]["verification_ok"] is True
    assert len(replayed["slices"]) == 1
    assert replayed["slices"][0]["slice_atomicity"] == "assimilated"


def test_fold_flags_unknown_kind_without_dropping_the_record():
    records = [{"kind": "task_meta", "task_id": "t-x"}, {"kind": "some_future_kind", "x": 1}]
    p = ts.fold_progress(records)
    assert p["unknown_ledger_kinds"] == ["some_future_kind"]


def test_fold_flags_higher_schema_without_crashing():
    records = [{"kind": "task_meta", "task_id": "t-x", "lv": 99}]
    p = ts.fold_progress(records)
    assert 99 in p["higher_ledger_schema_seen"]


def test_fold_surfaces_open_slice_as_open_not_hidden():
    records = _sample_records()[:6]  # everything up to action_begin, no slice_close
    p = ts.fold_progress(records)
    assert len(p["slices"]) == 1
    assert p["slices"][0]["slice_atomicity"] == "open"


def test_fold_execution_ok_false_on_any_failed_action():
    records = [
        {"kind": "action_begin", "i": 0, "op": "x"},
        {"kind": "action_end", "i": 0, "status": "failed", "execution_ok": False},
        {"kind": "action_begin", "i": 1, "op": "y"},
        {"kind": "action_end", "i": 1, "status": "done", "execution_ok": True},
    ]
    p = ts.fold_progress(records)
    assert p["verdicts"]["execution_ok"] is False


def test_fold_execution_ok_null_while_actions_pending():
    records = [{"kind": "action_begin", "i": 0, "op": "x"}]
    p = ts.fold_progress(records)
    assert p["verdicts"]["execution_ok"] is None
    assert p["current_action"] == 0


def test_fold_design_ok_none_with_no_criterion_records():
    p = ts.fold_progress([{"kind": "action_begin", "i": 0, "op": "x"}])
    assert p["verdicts"]["design_ok"] is None
    assert p["criteria"] == []


def test_fold_design_ok_true_when_all_criteria_passed():
    records = [
        {"kind": "criterion", "k": 0, "evaluator": "room_area_sqm", "op": ">=", "status": "passed",
         "ok": True, "expected": 9.0, "actual": 9.5, "reason": None, "used_type_fallback": False},
        {"kind": "criterion", "k": 1, "evaluator": "element_exists", "op": "==", "status": "passed",
         "ok": True, "expected": None, "actual": None, "reason": None, "used_type_fallback": False},
    ]
    p = ts.fold_progress(records)
    assert p["verdicts"]["design_ok"] is True
    assert [c["k"] for c in p["criteria"]] == [0, 1]


def test_fold_design_ok_delegates_to_eval_support_aggregate_design_ok():
    """Not a reimplementation of eval_support's own aggregation tests --
    just proving fold_progress calls through to the one real function
    (failure dominates an unrelated unknown), so the two cannot drift
    apart. The full rule matrix is tested directly in
    tests/test_eval_support.py."""
    records = [
        {"kind": "criterion", "k": 0, "evaluator": "room_area_sqm", "op": ">=", "status": "failed",
         "ok": False, "expected": 50.0, "actual": 9.5, "reason": None, "used_type_fallback": False},
        {"kind": "criterion", "k": 1, "evaluator": "param_value", "op": "==", "status": "missing_parameter",
         "ok": None, "expected": "x", "actual": None, "reason": "not found", "used_type_fallback": False},
    ]
    p = ts.fold_progress(records)
    assert p["verdicts"]["design_ok"] is False  # failed dominates the unrelated unknown


def test_fold_criteria_are_ordered_by_k_regardless_of_ledger_order():
    records = [
        {"kind": "criterion", "k": 1, "evaluator": "b", "op": "==", "status": "passed", "ok": True,
         "expected": None, "actual": None, "reason": None, "used_type_fallback": False},
        {"kind": "criterion", "k": 0, "evaluator": "a", "op": "==", "status": "passed", "ok": True,
         "expected": None, "actual": None, "reason": None, "used_type_fallback": False},
    ]
    p = ts.fold_progress(records)
    assert [c["evaluator"] for c in p["criteria"]] == ["a", "b"]


def test_summarize_for_response_carries_criteria_and_design_ok_through_unbounded():
    records = [
        {"kind": "criterion", "k": 0, "evaluator": "room_area_sqm", "op": ">=", "status": "passed",
         "ok": True, "expected": 9.0, "actual": 9.5, "reason": None, "used_type_fallback": False},
    ]
    p = ts.fold_progress(records)
    out = ts.summarize_for_response(p)
    assert out["criteria"] == p["criteria"]
    assert out["verdicts"]["design_ok"] is True


def test_summarize_for_response_bounds_size_and_keeps_failures():
    actions = [{"i": i, "status": "done"} for i in range(30)]
    actions[5]["status"] = "failed"  # outside the last-10 tail
    progress = {"actions": actions}
    summary = ts.summarize_for_response(progress)
    assert summary["actions_total"] == 30
    ids = [a["i"] for a in summary["actions"]]
    assert 5 in ids  # the failure survives even though it's not in the tail
    assert set(range(20, 30)).issubset(set(ids))  # last 10


# ---------------------------------------------------------------------------
# progress.json write/read, rebuild-from-ledger fallback
# ---------------------------------------------------------------------------

def test_write_and_read_progress_round_trip(task_dir):
    ts.write_progress(task_dir, {"lifecycle": "running", "v": 1})
    obj, meta = ts.read_progress(task_dir)
    assert obj["lifecycle"] == "running"
    assert meta["read_from"] == "primary"


def test_read_progress_rebuilds_from_ledger_when_file_is_gone(task_dir):
    for rec in _sample_records():
        ts.append_ledger(task_dir, rec)
    # never call write_progress -- simulate progress.json having never
    # existed (or been lost), the ledger must still answer correctly
    obj, meta = ts.read_progress(task_dir)
    assert meta["read_from"] == "rebuilt"
    assert obj["lifecycle"] == "finished"


def test_read_progress_rebuilds_when_both_json_files_are_corrupt(task_dir):
    for rec in _sample_records():
        ts.append_ledger(task_dir, rec)
    ts.write_progress(task_dir)
    p = ts.progress_path(task_dir)
    with open(p, "w") as f:
        f.write("{broken")
    with open(p + ".bak", "w") as f:
        f.write("{also broken")
    obj, meta = ts.read_progress(task_dir)
    assert meta["read_from"] == "rebuilt"
    assert obj["lifecycle"] == "finished"  # ledger is still authoritative


# ---------------------------------------------------------------------------
# lock: fresh vs stale, atomic acquire
# ---------------------------------------------------------------------------

def test_lock_acquire_and_release(task_dir):
    path, rec = ts.acquire_task_lock(task_dir, "run-1")
    assert path is not None
    assert rec["run"] == "run-1"
    assert os.path.isfile(path)
    ts.release_task_lock(path)
    assert not os.path.isfile(path)


def test_lock_fresh_blocks_a_second_acquire(task_dir, monkeypatch):
    monkeypatch.setattr(ts, "epoch_now", lambda: 1000.0)
    path1, _ = ts.acquire_task_lock(task_dir, "run-1")
    assert path1 is not None
    monkeypatch.setattr(ts, "epoch_now", lambda: 1000.0 + 119.0)  # < 120s
    path2, rec2 = ts.acquire_task_lock(task_dir, "run-2")
    assert path2 is None
    assert rec2["run"] == "run-1"


def test_lock_stale_at_121s_is_taken_over(task_dir, monkeypatch):
    monkeypatch.setattr(ts, "epoch_now", lambda: 1000.0)
    ts.acquire_task_lock(task_dir, "run-1")
    monkeypatch.setattr(ts, "epoch_now", lambda: 1000.0 + 121.0)  # > 120s
    path2, rec2 = ts.acquire_task_lock(task_dir, "run-2")
    assert path2 is not None
    assert rec2["run"] == "run-2"


def test_lock_hb_epoch_is_a_plain_number_never_a_parsed_string(task_dir):
    path, rec = ts.acquire_task_lock(task_dir, "run-1")
    assert isinstance(rec["hb_epoch"], float)


# ---------------------------------------------------------------------------
# plan_hash: material vs non-material, ops_version
# ---------------------------------------------------------------------------

def _base_plan():
    return {
        "schema_v": 1, "objective": "widen room", "scope": {},
        "constraints": [{"id": "C1", "statement": "x"}],
        "phases": [{"n": 0, "name": "p1"}],
        "actions": [{"i": 0, "op": "move_elements", "args": {"vector_mm": {"x": 1}}}],
        "expected_changes": {"modified": [1]},
        "success_criteria": [{"id": "S1", "evaluator": "room_area_sqm"}],
        "policy": {"on_action_failure": "abort"},
        "reasoning": {"selected": "A"},
        "risks": [{"statement": "low risk"}],
        "assumptions": [{"id": "A1", "statement": "x"}],
        "confidence": {"level": "medium"},
        "intent": "free text",
        "created": "2026-09-01T00:00:00",
    }


def test_plan_hash_stable_under_non_material_edits():
    p1 = _base_plan()
    h1 = ts.plan_hash(p1)
    p2 = _base_plan()
    p2["risks"] = [{"statement": "totally different risk"}]
    p2["assumptions"] = []
    p2["confidence"] = {"level": "low"}
    p2["intent"] = "completely different free text"
    p2["created"] = "2099-01-01T00:00:00"
    assert ts.plan_hash(p2) == h1


@pytest.mark.parametrize("key,new_value", [
    ("objective", "a different objective"),
    ("scope", {"floor": "4"}),
    ("constraints", []),
    ("phases", []),
    ("actions", []),
    ("expected_changes", {}),
    ("success_criteria", []),
    ("policy", {"on_action_failure": "continue"}),
    ("reasoning", {"selected": "B"}),
    ("schema_v", 2),
])
def test_plan_hash_changes_for_every_material_field(key, new_value):
    p1 = _base_plan()
    h1 = ts.plan_hash(p1)
    p2 = _base_plan()
    p2[key] = new_value
    assert ts.plan_hash(p2) != h1, "material field %r did not change the hash" % key


def test_plan_hash_is_deterministic_regardless_of_key_order():
    p1 = _base_plan()
    p2 = dict(reversed(list(_base_plan().items())))
    assert ts.plan_hash(p1) == ts.plan_hash(p2)


def test_ops_version_reads_real_file_when_present(tmp_path):
    f = tmp_path / "ops.py"
    f.write_text(u"OPS = {}\n", encoding="utf-8")
    v = ts.ops_version(str(f))
    assert v is not None
    assert len(v) == 12


def test_ops_version_changes_when_source_changes(tmp_path):
    f = tmp_path / "ops.py"
    f.write_text(u"OPS = {}\n", encoding="utf-8")
    v1 = ts.ops_version(str(f))
    f.write_text(u"OPS = {'x': 1}\n", encoding="utf-8")
    v2 = ts.ops_version(str(f))
    assert v1 != v2


def test_ops_version_returns_none_never_a_guess_when_unreadable():
    assert ts.ops_version("C:\\does\\not\\exist\\ops.py") is None


# ---------------------------------------------------------------------------
# state machine: legal / illegal transitions, all recorded
# ---------------------------------------------------------------------------

def test_init_task_starts_proposed(task_dir):
    ts.init_task("t-x", "hash1", canon="c:\\x.rvt")
    lifecycle, outcome, reasons, _ = ts.current_state(task_dir)
    assert lifecycle == "proposed"
    assert outcome is None


def test_legal_transition_chain(task_dir):
    ts.init_task("t-x", "hash1")
    ts.transition(task_dir, "approved", "user approved")
    ts.transition(task_dir, "running", "slice 1 start")
    ts.transition(task_dir, "finished", "slice 1 done", outcome="completed")
    lifecycle, outcome, _, _ = ts.current_state(task_dir)
    assert lifecycle == "finished"
    assert outcome == "completed"


def test_illegal_transition_raises_and_is_still_recorded(task_dir):
    ts.init_task("t-x", "hash1")
    with pytest.raises(ts.IllegalTransition):
        ts.transition(task_dir, "running", "skip approval")  # proposed -> running is not allowed
    records, _ = ts.read_ledger(task_dir)
    illegal = [r for r in records if r.get("kind") == "state" and r.get("ok") is False]
    assert len(illegal) == 1
    assert illegal[0]["from"] == "proposed"
    assert illegal[0]["to"] == "running"
    # and the lifecycle did NOT actually move
    lifecycle, _, _, _ = ts.current_state(task_dir)
    assert lifecycle == "proposed"


def test_finished_is_terminal(task_dir):
    ts.init_task("t-x", "hash1")
    ts.transition(task_dir, "approved", "x")
    ts.transition(task_dir, "running", "x")
    ts.transition(task_dir, "finished", "x", outcome="completed")
    with pytest.raises(ts.IllegalTransition):
        ts.transition(task_dir, "running", "cannot resume a finished task")


def test_blocked_requires_explicit_resume(task_dir):
    ts.init_task("t-x", "hash1")
    ts.transition(task_dir, "approved", "x")
    ts.transition(task_dir, "running", "x")
    ts.transition(task_dir, "blocked", "unknown outcome", review_reasons=["unknown_action_outcome"])
    lifecycle, _, reasons, _ = ts.current_state(task_dir)
    assert lifecycle == "blocked"
    assert reasons == {"unknown_action_outcome"}
    ts.transition(task_dir, "running", "human resumed")
    lifecycle, _, reasons, _ = ts.current_state(task_dir)
    assert lifecycle == "running"
    assert reasons == set()  # cleared on leaving blocked


def test_amend_outcome_only_on_finished_task(task_dir):
    ts.init_task("t-x", "hash1")
    with pytest.raises(ts.IllegalTransition):
        ts.amend_outcome(task_dir, "rolled_back", "compensation completed")
    ts.transition(task_dir, "approved", "x")
    ts.transition(task_dir, "running", "x")
    ts.transition(task_dir, "finished", "x", outcome="failed")
    ts.amend_outcome(task_dir, "rolled_back", "compensation task t-y completed")
    lifecycle, outcome, _, _ = ts.current_state(task_dir)
    assert lifecycle == "finished"  # amendment never changes lifecycle
    assert outcome == "rolled_back"


def test_amend_outcome_does_not_use_a_transitions_edge(task_dir):
    # "finished" -> "finished" is not in TRANSITIONS at all (empty set) --
    # amend_outcome must not rely on it being one.
    assert ts.TRANSITIONS["finished"] == frozenset()


def test_proposed_can_be_rejected_without_ever_being_approved(task_dir):
    ts.init_task("t-x", "hash1")
    ts.transition(task_dir, "finished", "user rejected", outcome="rejected")
    lifecycle, outcome, _, _ = ts.current_state(task_dir)
    assert lifecycle == "finished"
    assert outcome == "rejected"


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------

def test_cancel_request_and_check(task_dir):
    assert ts.check_cancel(task_dir) is None
    ts.request_cancel(task_dir, mode="rollback_slice", reason="user asked to stop")
    req = ts.check_cancel(task_dir)
    assert req["mode"] == "rollback_slice"
    ts.clear_cancel(task_dir)
    assert ts.check_cancel(task_dir) is None


def test_cancel_rejects_unknown_mode(task_dir):
    with pytest.raises(ValueError):
        ts.request_cancel(task_dir, mode="not_a_real_mode")


# ---------------------------------------------------------------------------
# ledger append failure is fail-stop (simulated)
# ---------------------------------------------------------------------------

def test_ledger_append_failure_raises_fail_stop(task_dir, monkeypatch):
    def _boom(path, line):
        raise IOError("disk full")
    monkeypatch.setattr(ts, "_append_line", _boom)
    with pytest.raises(ts.LedgerAppendFailed):
        ts.append_ledger(task_dir, {"kind": "note"})
