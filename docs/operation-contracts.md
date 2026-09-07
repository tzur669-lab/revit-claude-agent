# Operation contracts — level-1 and level-2 verification

What every mutating MCP tool actually checks before reporting success, and what it
deliberately does not. See `docs/architecture.md` for how the three verification
levels (transaction / post-condition / intent) fit together; this document is the
matrix Milestone 1 required before any level-2 code was written.

Written *after* implementation and live testing on 2026-08-31, not strictly before
it as originally planned — the two empirical negative-test cases below (the silent
wall/door no-op, and the extreme-distance rollback) were discovered while building
the level-1 helper and materially changed what this matrix says. The matrix is
still authoritative for level-2 scope going forward: a future change to any
post-condition below must update this file first, then the code.

**All tolerances marked "provisional" are starting values, not documented Revit
constants.** They come from a first pass of live testing, not a spec. See each row.

---

## Level 1 — transaction (`commit_verified`, `revit_mcp/utils.py`)

Applies uniformly to every mutating handler. `System.Enum.GetNames(DB.TransactionStatus)`
was read from the live Revit session on 2026-08-31, not assumed — it returned **seven**
members, not the five originally guessed while planning this milestone:

| `Commit()` returns | `tx_ok` | Meaning |
|---|---|---|
| `Committed` | `true` | database accepted the change — proceed to level 2, this is not yet success |
| `RolledBack` | `false` | the failure preprocessor (`_FailureSwallower`) returned `ProceedWithRollBack` on a genuine error-severity failure |
| `Pending` | `false` | still open; never report a pending write as done |
| `Started` | `false` | commit did not take effect |
| `Uninitialized` | `false` | commit did not take effect |
| `Error` | `false` | not a documented steady-state `Commit()` result; fail closed |
| `Proceed` | `false` | not a documented steady-state `Commit()` result; fail closed |
| `t is None` (`#!notx`) | `null` | the handler delegated its own transaction management; **not observable, not a pass** |

`tx_ok` is tri-state. A caller must branch on `tx_ok is False`, never `not tx_ok` —
the latter also catches `None` and would wrongly fail every `#!notx` call. This was
caught live while wiring `code_execution.py` and is the reason this note exists.

`commit_verified(t)` returns `(tx_ok, tx_status)` only. It does not construct a
response or decide handler policy — each of the 20 route-handler files below maps
the outcome into its own existing response shape.

---

## Level 2 — post-condition, by operation

Every response that reaches level 2 carries a `verified` object in one shape:

```jsonc
{"ok": true,  "method": "...", "expected": {...}, "actual": {...}}
{"ok": false, "method": "...", "expected": {...}, "actual": {...}, "reason": "..."}
{"ok": null,  "status": "not_checked", "reason": "..."}
```

`ok` is the only field a caller needs to branch on: `true`, `false`, `null` — never
absent, never a bare string. A batch operation additionally carries `failures: [...]`
(capped at 50) when some but not all items failed.

### `transform_elements` — move / copy / rotate / mirror (`transforms.py`)

```
move(element_ids, vector)
  pre    : every id resolves; none pinned
  post   : actual_displacement == requested_vector, per element
  method : re-read LocationPoint.Point / LocationCurve.GetEndPoint(0) before
           and after commit, compare the delta to the requested vector
  tol    : LOCATION_TOLERANCE_FT = 1e-6 ft. Provisional - validated only
           against a rigid-body MoveElement with no host constraint; not
           yet measured for a constrained element that is expected to
           move less than requested.
  limits : MEASURED 2026-08-31, wall 1658707: assigning a Curve that would
           make a hosted door's cut position fall outside the wall's new
           extent silently did not apply - Commit() returned Committed,
           the failure-message list was EMPTY (no Warning, no Error - this
           bypasses the failure pipeline entirely), and the wall's curve
           read back byte-identical to before the attempt. This is why
           level 2 exists independently of level 1: a transaction-status
           check alone would have reported total success.
  limits : moving one wall changed an adjacent Room's LocationPoint (its
           tag anchor), and moving the wall back did NOT restore the
           room's original point - Revit recomputed a new anchor rather
           than reversing the old one. Verified live 2026-08-31 (room
           1660265): the room's boundary/area/name/number were all still
           correct: this is a genuine, permanent side effect of the
           operation, not damage and not a bug in this tool.

copy(element_ids, vector)
  post   : every id in new_element_ids resolves to a live element
  method : element_exists
  limits : does not verify the copy's location, only its existence

rotate / mirror
  post   : not_checked
  reason : no per-element geometric verification implemented in this
           milestone for arbitrary rotation angle / mirror plane; level 1
           (tx_status) still applies
```

**Level 1 negative case, measured live 2026-08-31**, wall 1658707, translation
`(50000000, 0, 0)` ft (~20x Revit's own ~20-mile distance-from-origin limit): the
failure-message list carried `["Can't keep elements joined." (Error), "Highlighted
walls overlap." (Warning), "Room is not in a properly enclosed region" (Warning)]`.
The preprocessor correctly returned `ProceedWithRollBack` on the Error; `Commit()`
returned `RolledBack`; the wall's position was confirmed unchanged after. This is
the live proof that `commit_verified` catches the documented "MoveElement returned
success and moved nothing" failure mode.

### `place_family` (`placement.py`)

```
place_family(family_name, type_name, location, level_name)
  pre    : symbol resolves and is active
  post   : instance resolves AND its location matches the requested point
  method : re-read the instance's LocationPoint after commit
  tol    : PLACEMENT_TOLERANCE_FT = 10mm-in-feet. Provisional; deliberately
           looser than transforms.py's move tolerance because placement
           legitimately snaps to family/host constraints in ways a rigid
           move does not - see limits below.
  limits : wall-hosted instances (windows/doors) have X/Y intentionally
           projected onto the host wall's line by Revit itself - checking
           X/Y there would false-positive on correct behaviour, so only Z
           (elevation) is checked for hosted instances. Z is not affected
           by host-wall projection and is exactly the axis both
           historical bugs (elevation added twice; WorkPlaneBased stuck
           at Z=0) live on - see revit-session/references for the
           original measurements (104 items in walls, 116 at double
           height, from insertion-point-offset and elevation-doubling).
  limits : this contract does NOT check "is the instance in the intended
           room" - a family whose insertion point sits at its back
           satisfies "location matches requested point" while still
           landing in the wrong physical spot. That is a level-3 (intent)
           question the checkpoint diff must answer, not this check.
```

### `set_parameter` (`parameters.py`) / `modify_element` (`editing.py`)

Both share `param_read_matches()` in `utils.py` rather than duplicating the
comparison — same four storage types, same semantics.

```
set_parameter(element_id, parameter_name, value)
  pre    : parameter exists (instance or type) and is not read-only
  post   : re-read AFTER commit equals the TYPED value requested
  method : param_read_back - compares by storage type, not by raw string,
           so requesting "5" for a Double that correctly reads back 5.0
           is not reported as a mismatch
  tol    : exact for String/Integer/ElementId; PARAM_DOUBLE_TOLERANCE =
           1e-6 for Double. Provisional - not yet measured against a
           parameter with real unit-conversion rounding (e.g. an
           imperial-display metric parameter).
  limits : Revit may legitimately round or clamp a constrained value and
           still commit; that shows up correctly as verified.ok: false,
           not as an exception, but this contract does not distinguish
           "rejected" from "rounded slightly outside tolerance" - both
           read the same way today.

modify_element(element_id, parameters: {name: value, ...})
  post   : same param_read_back check, once per parameter that Set() did
           not raise on, aggregated with per-parameter failures preserved
  limits : same as set_parameter, per parameter in the batch
```

Both handlers previously returned the *requested* value in the response's
`new_value`/`message` fields, not what was actually re-read - `set_parameter`
computed a post-commit re-read and then discarded it. Fixed as part of this
change: the response now reports the real re-read value, which is also what
`verified.actual` reports.

### `delete_elements` (`editing.py`)

```
post   : each ORIGINALLY-REQUESTED id no longer resolves
method : element_absent
limits : cascaded deletions (a hosted element removed along with its host)
         are reported but not part of this contract - they are a bonus
         side effect, not the operation's own guarantee
```

### `create_level` / `create_line_based_element` (wall, beam) /
`create_surface_based_element` (floor, ceiling, roof) — `building.py`

```
post   : each created id resolves AND carries the expected BuiltInCategory
method : element_category (verify_created_elements, utils.py - shared
         across building.py and rooms.py rather than duplicated, since
         both check "did create_* actually make what it claims")
limits : "ceiling" and "floor" both create via DB.Floor.Create - the
         verified category for both is OST_Floors, matching what Revit
         actually made, not the caller's semantic label. This is existing
         behaviour (Revit has no separate ceiling-creation API used here),
         not something this milestone changed.
limits : create_level does not verify elevation, only category+existence.
         Elevation is the operation's more meaningful contract; deferred.
```

### `create_room` (`rooms.py`)

```
post   : the room resolves, carries OST_Rooms, AND has Area > 0
method : room_area
limits : a Room element that "creates" successfully but is unplaced or
         not enclosed (Area == 0) is a real, previously-measured trap in
         this project's own room-definition domain - it passes a naive
         "does the id resolve?" check, which is exactly why this
         contract checks area, not just existence.
```

### `create_room_separation` (`rooms.py`), `create_dimensions` / `tag_walls`
(`annotation.py`), `create_detail_line` (`detail.py`), `create_grid` /
`create_structural_framing` (`structure.py`), `tag_elements` (`tags.py`),
`create_duct` / `create_pipe` (`mep.py`)

```
post   : each created id resolves (element_exists), plus category check
         where the category is unambiguous (grids, ducts, pipes, detail
         lines, structural framing)
limits : none of these verify placement/geometry beyond existence - e.g.
         a dimension's actual value, or a tag's actual leader position,
         are not checked. Existence is the cheap, honest floor; a
         real geometric contract for these is future work, not silently
         assumed.
```

### `create_mep_system` (`mep.py`), `create_view` (`view_management.py`)

```
post   : the element resolves AND its Name matches what was requested
method : element_exists_and_name
limits : create_mep_system's "rename an existing system" path and "create
         a new system" path share this same check; neither verifies the
         system actually contains the requested elements' connectors.
```

### `set_active_view` (`view_management.py`)

```
pre    : view_name must resolve to a non-template view in the model
tx     : none - uidoc.ActiveView is UI state, not a DB.Transaction, so
         there is no tx_ok/tx_status for this route at all
post   : uidoc.ActiveView, re-read after assignment, actually matches the
         requested view's id
method : active_view_matches
limits : previously this route had no post-condition at all and reported
         success unconditionally regardless of whether the assignment took
         effect - closed as part of the M1-M5 architecture upgrade's M3
         (see that upgrade's D6 finding). A verification failure here
         still returns HTTP 200/status "success" (the assignment statement
         itself did not raise) with verified.ok: false - consistent with
         every other create_*/route in this matrix, where tx_ok (or here,
         a raised exception) governs the HTTP status and verified.ok is a
         separate signal.
```

### `create_sheet` / `create_schedule` (`documentation.py`)

```
post   : resolves + category check (OST_Sheets / OST_Schedules)
limits : create_schedule does not verify the requested fields were all
         actually added - fields_failed already reports per-field misses
         separately; verified here is only about the schedule element
         itself existing.
```

### `export_document` (`documentation.py`), `export_ifc` (`interop.py`)

```
post   : the output file exists on disk AND has nonzero size
method : file_exists
limits : this is the clearest case of "the real product is a file, not
         model state" the plan called out in advance. Before this
         change, file_size_kb could silently read 0 with NO failure
         signal at all - a caller had to notice the number was zero
         themselves. Now verified.ok is false when the file is missing
         or empty, with the file path/size as evidence either way.
         Content correctness (is the PDF/IFC actually valid) is not
         checked - only that something was written.
```

### `link_file` (`interop.py`)

```
post   : the returned element id (link instance or imported geometry)
         resolves, when one was captured
limits : SAT/SKP/3DM imports and some link paths may not always yield a
         capturable out-param id (pre-existing limitation, not part of
         this change) - in that case verified is honestly not_checked
         rather than asserting a pass with nothing to check.
```

### `color_splash` / `clear_colors` (`colors.py`)

```
post   : not_checked
reason : these operations write per-view graphic overrides
         (View.SetElementOverrides), not model elements. Re-reading a
         view's override table and comparing it to the requested color
         is possible but not implemented in this milestone. Level 1
         (tx_status) still applies and is the only thing checked.
```

### `execute_revit_code` (`code_execution.py`)

```
post   : not_checked, structurally - not a gap
reason : this endpoint runs arbitrary submitted code with no fixed
         operation contract. A generic level-2 post-condition cannot be
         defined for "any Python/Revit-API code a caller might submit."
         Level 1 (tx_status) is the only thing that generalizes, and now
         applies here for the first time - previously this endpoint's
         success path did not check Commit()'s return value at all,
         despite being the tool this project's own tracker protocol
         (session_start/checkpoint) runs through on every call.
```

**Risk classification (`code_safety.py`) — advisory, not a gate.** Every
submission is classified `read`/`modify`/`destructive`/`unknown` by a static
AST scan (`revit_mcp/code_safety.py::classify`) before execution, and the
result (`risk`, `risk_signals`) is returned alongside the normal response on
every path — success, transaction failure, and exception. **Classification
never blocks execution**, on any risk level, including `unknown` (an
unparseable submission) — it is observability metadata, not a sandbox or a
security boundary, and does not claim to detect every dangerous operation a
piece of Python can perform. See `code_safety.py`'s own module docstring for
what is and is not detected.

**Audit trail — best-effort, append-only.** Every submission (all three
outcomes) is logged to `~/.claude/revit-tracker/audit/code-YYYY-MM.ndjson`:
timestamp, description, the **exact submitted source** (never normalized,
dedented, or truncated), `risk`/`risk_signals`, `tx_status`/`tx_ok` where
applicable, and the outcome. This file shares no lock with the tracker
(`.lock`), the scribe (`.scribe.lock`), or the lessons file
(`.lessons.lock`) — it is a separate, independent write path. A failure to
write the audit record (disk full, permission denied, an unserializable
value) is caught at two layers (inside the writer, and again at each call
site) and can never change the execution result, roll back the transaction,
or turn a successful run into an error. No automatic rotation/deletion of
audit history exists.

### `save_document` (`document.py`)

Not a `DB.Transaction` operation at all — `Save()`/`SaveAs()` are void calls with
no transaction to check, and the module's own header comment says they must not
run inside one. No `commit_verified` here; a parallel, purpose-built check instead:

```
post   : the file exists at the target path AND its mtime is recent
method : file_mtime
tol    : SAVE_RECENCY_SECONDS = 30s. Provisional - chosen to tolerate a
         slow disk or antivirus scan without papering over a genuine
         no-op; not measured against a real slow-save case.
limits : does not verify the saved file's content is valid/complete,
         only that something was written recently to the right path.
```

---

## The Phase A execution spine — `propose_plan` → `approve_plan` → `execute_plan` → `get_task_status`

A fourth contract, distinct from levels 1/2 above and from the read-only/dry-run
contract below: a **plan** is a validated, hashed, immutable JSON document
(`tools/plan_schema.py`) describing a coordinated multi-action change; a
**task** is that plan's execution record, living under
`~/.claude/revit-tracker/tasks/<task_id>/` as an append-only ledger
(`ledger.ndjson`, `revit_mcp/task_store.py`) plus a derived, rebuildable
`progress.json` projection. Three tools (`propose_plan`, `get_task_status`,
`rollback_task`) never call Revit at all — they only touch this filesystem
contract. `approve_plan` calls `/preflight_plan/` (read-only, opens no
transaction). Only `execute_plan` mutates the model.

### `execute_plan` (`revit_mcp/plan_exec.py`)

```
unit   : one HTTP call = one BUDGETED SLICE = one DB.TransactionGroup
         containing one DB.Transaction per action (revit_mcp/ops.py's
         "run" functions never open a transaction themselves - the slice
         loop in plan_exec.py owns every Start()/commit_verified()/
         RollBack()/Assimilate() call; tests/test_ops_no_transactions.py
         is an AST guard for exactly this).
level 1: commit_verified() per action, unchanged contract - Committed
         inside a group means "accepted by the database AND still
         revocable by the enclosing group until Assimilate()".
level 2: each op's own verify() (see the op table below), run after that
         action's transaction commits.
atomicity: Assimilate() merges the slice's inner transactions into ONE
         undo entry; a failed inner transaction does NOT poison the group
         - a later action in the same slice can still commit and the
         group can still Assimilate() cleanly (measured live, 2026-09-01).
         Cross-slice atomicity does NOT exist - a plan spanning multiple
         slices is disclosed as such (slice_atomicity per slice, task-level
         "atomicity" only meaningful at a terminal state) rather than
         implied to be all-or-nothing.
group  : never held open across HTTP requests - a group outliving its
         handler is owned by nobody, and a client timeout would strand
         the model with no code path left to close it.
budget : DEFAULT_SLICE_BUDGET_MS=20000, hard-capped at 25000; checked
         BEFORE starting each action, never mid-action. HTTP timeout for
         this one endpoint is 75s (main.py's ENDPOINT_TIMEOUTS) - the gap
         covers Assimilate(), ledger/progress writes, and UI-thread
         queueing, none of which the budget can bound.
```

**TransactionGroup mechanics, measured live before any of this was written
(2026-09-01):** `RollBack()` exists; `RollBackAll()` does not.
`TransactionGroup` exposes no `GetFailureHandlingOptions`/
`SetFailureHandlingOptions` and no `GetTransactionNames()` — `suppress_warnings()`
goes on the **inner** transactions only (calling it on the group silently
no-ops, since its own `try/except` swallows the resulting `AttributeError`,
while looking protected); group names are guarded statically instead
(`tests/test_transaction_names.py::test_every_transaction_group_name_satisfies_tx_is_ours`).

### The v1 op set (`revit_mcp/ops.py`, data twin `tools/ops_schema.py`)

Each op provides `capture`/`run`/`verify`/`inverse`/`probe`. `capture(doc, args,
targets)` runs before any mutation and is recorded into the ledger's
`action_begin` record; `inverse(args, targets, before, result)` is PURE (no
`doc`) and is computed **before commit** for `idempotency="absolute"` ops, or
**after commit** (using `result`, since the target didn't exist beforehand)
for `idempotency="creating"` ops.

| op | idempotency | fidelity | post-condition method |
|---|---|---|---|
| `move_elements` | absolute | exact | `location_displacement` (reuses `transforms.verify_move`) |
| `set_parameters` | absolute | exact | `param_read_back` (reuses `utils.param_read_matches`) |
| `set_wall_curve` | absolute | approximate | `curve_endpoints`, 1mm tolerance |
| `delete_elements` | absolute | **irreversible** | `element_absent` (reuses `editing.verify_delete`) |
| `create_room` | creating | compensatable | `room_area` (reuses `rooms.verify_room`) |
| `create_room_separation` | creating | compensatable | `element_category` |
| `create_line_based_element` | creating | compensatable | `element_category` (walls only, see `building.apply_create_wall`) |
| `place_family` | creating | compensatable | `location_point` / `location_z_only` (reuses `placement.verify_place_family`) |

**Idempotency classing is the fix for a real hazard, not decoration.**
`DB.ElementTransformUtils.MoveElement` is *relative* — applying it twice moves
twice. `move_elements` is made *absolute* by capturing each target's point
before the move and computing the applied translation as `target − current`
at run time (`target = captured_point + requested_vector`), so re-running
after a partial or complete prior application converges instead of
accumulating. `set_parameters`/`set_wall_curve`/`delete_elements` are
naturally absolute (setting the same value or deleting an already-deleted id
is a no-op the second time). The four `creating` ops make a duplicate on
reapplication, so a crash inside their transaction window (`action_begin`
logged, no matching `action_end`) is resolved by an evidence hierarchy
(Phase B slice 3 — see that section below for the full design and the live
findings that shaped it), checked in this order:

1. **Authoritative**: a `commit_result` ledger record for this action — written
   by the slice loop the instant `commit_verified()` reports the transaction
   `Committed`, scoped to `idempotency: "creating"` ops only. Present and
   non-conflicting → skip with those exact ids, no probe call at all.
2. **Conflicting** `commit_result` records for the same action, or one whose
   `op` disagrees with the action's own → fail closed: block for review,
   never silently pick one.
3. **No `commit_result` at all** → fall back to
   `probe(doc, args, before) -> (outcome, recovered_ids, reason)` — a bounded
   heuristic, never proof of causation. `"applied"` skips, `"not_applied"`
   re-runs, `"unknown"` (with a machine-readable `reason`) blocks the task for
   human review rather than guessing.

All four creating ops now have a real probe, each backed by
`eval_support.probe_outcome_from_universe` — a universe-diff, not a
candidate-diff: every element of the relevant category+type is captured,
matching or not, so an existing element **independently modified** into
matching a request can never be mistaken for one this action created (a real
bug, found and fixed in `create_room`'s own probe — see below). `"applied"`
is returned only when the universe shows exactly one new, matching element;
anything else — a captured element vanishing or changing, more than one new
match, a new element that doesn't match — is `"unknown"`, never guessed.

### Unexpected-change detection (`tools/plan_tools.py::_check_unexpected_changes`)

The executor never invokes the tracker itself — `tracker.main()` raises
`TrackerOK` by design to force a rollback (fatal if caught inside the
executor's own transaction machinery), `op_snapshot` calls the destructive
`drain_events()` which would consume the executor's own DocumentChanged
events, and a tracker checkpoint fired from inside the executor could race a
concurrent Claude-initiated checkpoint for `<instance_dir>/.lock`. Instead:
the executor records `touched_uids` per action (the tracker's delta is keyed
by uid, and a deleted element's id can never be resolved to one afterward);
Claude runs the checkpoint through the existing `execute_revit_code` +
`revit-session` path; `get_task_status(check_unexpected=True)` then reads
`~/.claude/revit-tracker/last_run.json` and its `delta_file` (never the
capped `last_run.json["items"]`) and classifies each changed item
`accounted` / `side_effect` (category in the plan's own
`expected_changes.tolerated_side_effects`) / `foreign` (attributed to
`human`/`sync_incoming`, never ours to begin with) / `unexpected`. Refuses
the comparison outright when `delta["run"] == baseline_run` — no checkpoint
has run since this task's baseline, so any comparison would silently include
older work.

**Known, disclosed scope reduction:** preflight (`plan_exec.py::preflight`)
checks that every target element still **exists**, not that it is still the
**same** element (a full `UniqueId`-drift check would need a Revit round-trip
at propose time, which `propose_plan` deliberately does not make). Each op's
own `capture()`/`verify()` cycle provides a partial, not complete, safety net
on top of this.

### Success-criteria evaluators (Phase B, slice 1 — `revit_mcp/evaluators.py`,
### `revit_mcp/eval_support.py`, data twin `tools/ops_schema.py::EVALUATOR_ARGS`)

Where the v1 ops answer "did this action do what it claimed," the 5
evaluators answer a different question: "does the plan's own stated design
goal hold against the model's final state." Evaluated exactly once per
task, read-only, right after the last slice reaches `outcome: "completed"`
and its `TransactionGroup` has already `Assimilate()`d.

| evaluator | args (beyond `target`, `op`) | reuses |
|---|---|---|
| `element_exists` | — | `utils.verify_elements_exist` |
| `element_absent` | — | `editing.verify_delete` (already `method: "element_absent"`) |
| `room_area_sqm` | `value` (number) | `utils.SQFT_TO_SQM` (exact: `0.3048**2`) |
| `distance_mm` | `value` (number) | `DB.LocationPoint` only — no bounding-box fallback |
| `param_value` | `parameter_name`, `value`, optional `allow_type_fallback` | `utils.resolve_param`/`read_param_native` — **no unit conversion** |

**Every result is one of 8 named statuses**, never a bare tri-state:
`passed | failed | invalid_reference | wrong_category | missing_parameter |
unsupported_type | unsupported_geometry | evaluator_error`. `ok` is
*derived* from `status` (`{"passed": True, "failed": False}`, else `None`),
never set independently. Full result shape: `{k, evaluator, op, target,
expected, actual, status, ok, reason, used_type_fallback}` —
`used_type_fallback` is always present (default `False`), not only for
`param_value`, so a caller never needs a key-missing-vs-`False` distinction.

**`param_value` performs NO unit conversion**, by design, not omission:
grepped for `UnitUtils`/`ForgeTypeId`/`ConvertToInternalUnits` across the
whole tree — zero matches. `set_parameters` (the op that would have written
the value a criterion later checks) already has the shipped contract "a
Double's JSON value is Revit's own raw internal unit" — `param_value` makes
the identical contract so a value written and read back agree by
construction, and a mismatched operator/value-type (`>=` against a String)
returns `status: "unsupported_type"` (`eval_support.compare`) rather than
raising or silently passing. `room_area_sqm` (sqm) and `distance_mm` (mm)
convert on purpose — their unit is part of the evaluator's own name.

**`distance_mm` requires a real `DB.LocationPoint` on both targets** — a
bounding-box-center fallback was deliberately rejected (an axis-aligned bbox
has no consistent physical meaning across categories); a curve-based
element (a wall) returns `"unsupported_geometry"`, never an approximated
number.

**`design_ok`** (`eval_support.aggregate_design_ok`, called identically from
`fold_progress`, unit-tested directly so the ledger fold cannot drift from
the tested rule): no criteria → `None`. Any `"failed"` → `False` (dominates
an unrelated unknown). Else any non-`"passed"` status present (including
`"evaluator_error"`) → `None`. Only "every criterion passed" → `True`. A
`"completed"` task with `design_ok: False` is a legitimate, disclosed
terminal state — this never escalates the task's own outcome to `"failed"`.

No new ref kinds — a Room/Level is already addressable through the existing
`"element"` kind; `unit`/`zone` have no existing concept anywhere in this
codebase to build one from.

### Referring to a plan's own output (Phase B, slice 2)

`success_criteria`'s `target` gains a second kind, `{"kind": "created_by",
"action_i": N, "index": 0}` — the element(s) action `N`'s `run()` created,
by index into its `action_end.created_ids` (default `0`). Schema-validated
at propose time: `action_i` must name a known action whose op is
`idempotency: "creating"` (referencing e.g. `move_elements` is rejected
before execution, not left to fail silently as `invalid_reference`).
Deliberately **not** available to an action's own `targets` — only
`success_criteria` (evaluated after every action has already committed)
can meaningfully reference an id that didn't exist at propose time.

**Composed references — a new, additive `targets` (plural) field**, for
when one `target` dict can't express two independently-sourced ids (an
existing element paired with one a plan creates, or two different
actions' outputs): a list of single-source refs, each `"element"`
(exactly one id) or `"created_by"`, each resolving to exactly one id. A
criterion has exactly one of `target` / `targets` — the existing,
shipped `target: {"kind": "element", "ids": [id1, id2]}` shape
(including `distance_mm`'s 2-id case) is **unchanged**. One function,
`evaluators.py::_resolve_criterion_targets`, resolves both shapes for
every evaluator.

**Measured, not assumed: `create_room_separation` never produces more
than one output through this wiring.** `rooms.apply_create_room_separation`
(the shared, general helper) accepts a multi-curve array, which is why
`created_by`'s `index` field exists at all — but `ops.py`'s own
`_room_sep_run` (the specific call the plan/task system makes) only ever
builds a **one**-curve array from its single `curve_mm` arg, so
`created_ids` is always exactly one id through this path today, same as
the other three creating ops. `index` stays in the schema (harmless,
forward-compatible) but is unexercised beyond `0` by any op that exists
today.

**A `created_by` reference to a probe-skipped action** (see the probe
contract change above) resolves for all four creating ops as of Phase B
slice 3 — never a guess either way; an action whose commit could not be
identified with sufficient evidence still resolves to `invalid_reference`,
honestly. The result-status vocabulary did **not** grow for this:
`design_ok`'s aggregation already treats every non-`"passed"`/non-`"failed"`
status identically, so a finer-grained status would change no aggregation
behaviour, only prose — `invalid_reference`'s `reason` string carries the
specific detail instead.

**Live-verified**: a plan created 4 temporary walls then a room inside
them (`needs: [0,1,2,3]`) — `room_area_sqm` on `created_by(action_i=4)`
read the room's real, positive area (13.33 sqm); `element_exists` on
`created_by(action_i=0)` (a wall) passed; a composed `distance_mm`
(existing family instance + `created_by(action_i=4)`) measured a real
428 m. `design_ok` correctly dominated by one deliberately-failing
threshold. `_room_probe`'s recovered id was confirmed to match the real
created room directly. All 5 elements deleted afterward, tracker
checkpoint diff net-zero.

Still out of scope, disclosed not solved: an action's own `targets`
referencing a prior action's output (needs mid-slice resolution, a
separate, larger feature).

### Crash-resume identity for all four creating ops (Phase B, slice 3)

Closes slice 2's own disclosed gap through the evidence hierarchy described
above. Two full adversarial-review cycles preceded implementation; the
shipped design is the third revision, after the first (type+geometry
candidate matching) was rejected for inferring causal identity from a
resemblance test, and the second (universe-diff without ledger fail-closing)
was rejected for treating `not_applied` as a default and "last one wins" on
disagreeing identity evidence.

**`commit_result` — Level 1, authoritative when present.** Written by
`plan_exec.py`'s slice loop right after `commit_verified()` reports a
creating op's transaction `Committed`: `{"kind": "commit_result", "i", "op",
"created_ids", "touched_ids"}`. Not atomically coupled to Revit's own
commit — a crash between `commit_verified()` returning and this ledger
append is the residual window the probe (Level 3) still covers — but it
collapses that window from "anywhere between `run()` and the final
`action_end`" down to a few Python bytecodes, since real Revit crashes
overwhelmingly happen *during* heavy operations, not in a two-line gap.
`task_store.commit_result_evidence(records, i)` never resolves a
disagreement by recency: one record (or several identical ones) is
authoritative; several disagreeing records — different `op`/`created_ids`/
`touched_ids` — is `("conflict", None, "commit_result_conflict")`, and
`_classify_open_action` blocks rather than guessing which is right.
`task_store.ledger_conflicts(records)` separately cross-checks every
`commit_result` against that same action's eventual `action_end` and
surfaces disagreement through a new `ledger_conflicts` field on
`fold_progress`'s output, next to `unknown_ledger_kinds`. Live-confirmed:
a real `_run_slice` call produced the ledger sequence `action_begin →
stage(committing) → commit_result → stage(verifying) → action_end`
with `commit_result.created_ids` and the eventual `action_end.created_ids`
agreeing exactly; a synthetic crash-window ledger (commit_result present,
no action_end yet) resolved via Level 1 with the op's own probe
monkey-patched to raise and confirmed never called; two disagreeing
`commit_result` records for the same action correctly blocked.

**On the authoritative path, the skip also runs the op's real `verify()`**
against a synthetic `{"created_ids", "touched_ids"}` result, instead of the
permanent `not_checked` a heuristic recovery still gets — live-confirmed
returning a genuine `room_area` verification (`{"ok": true, "actual":
{"area_sqm": 3.42, ...}}`), not a placeholder.

**Level 3 — universe-diff, not candidate-diff, for all four ops.**
`eval_support.probe_outcome_from_universe(before_map, current_map,
matching_ids)` requires the *entire* captured universe's survivors to be
byte-identical before "no new ids" is even considered — an id present at
capture time that vanishes, or whose fingerprint changes, forces `unknown`
regardless of what else appears. `"applied"` is returned only for exactly
one new id that is also in `matching_ids`. Per-op universe/identity:

| Op | Universe | Identity checked | Excluded (why) |
|---|---|---|---|
| `create_line_based_element` | `OST_Walls`, `GetTypeId() == type_id` | curve endpoints (bidirectional), base level (via `WALL_BASE_CONSTRAINT`, resolved by the same helper `_line_elem_run` itself uses), height (`WALL_USER_HEIGHT_PARAM`) | offset/`is_structural` — hardcoded constants in `_line_elem_run` |
| `create_room_separation` | `OST_RoomSeparationLines` | curve endpoints only | level/view — the line's own z already carries this information |
| `place_family` | `FamilyInstance`, `Symbol.Id == symbol_id` | point (host-aware tolerance, reusing `placement.PLACEMENT_TOLERANCE_FT` and `verify_place_family`'s own z-only-when-hosted rule), host id and level id when supplied and readable | rotation/facing/placement type — not set by `_place_run` |
| `create_room` | `OST_Rooms` | `GetRoomAtPoint` intersected with newly-appeared ids | — |

**The `_room_probe` fix.** The original compared only `GetRoomAtPoint`
before vs. after, which cannot distinguish a newly created room from a
**pre-existing** room whose boundary grew to newly cover the test point —
live-reproduced by building a small room, capturing a point outside it,
then extending the enclosing walls (keeping the loop closed) until
`GetRoomAtPoint` for that point started returning the same pre-existing
room: the fixed probe answered `unknown` / `relevant_model_change_ambiguous`
while the old comparison would have answered `applied` with a wrong id.

**Room-separation curve access, measured**: `.Location.Curve` on a real
`NewRoomBoundaryLines`-created `ModelLine` returns a usable `DB.Line`
directly; `.GeometryCurve` ships as a defensive fallback but is unexercised
in this Revit version.

**Live-verified for all four ops**: `not_applied`, `applied` with the exact
real id, ambiguous (two new matching → `unknown`, no id), a pre-existing
duplicate correctly excluded when a genuinely new match appears alongside
it, and probe determinism. Collector cost measured against the real model
(61 walls, 327 family instances, ~700 elements total): single-digit-to-low-
teens milliseconds per universe scan. Every element created during
verification deleted afterward; tracker checkpoint diff net-zero.

**Residual risk, documented not designed away**: `resume=true` bypasses the
`revit_restarted` block specifically so recovery can run after a real Revit
restart — exactly the window in which a human might have edited the model.
An indistinguishable element created by someone else, with no other change
in that same universe, would satisfy the heuristic; this is a bounded
heuristic, never described as proof of causation. Document locking stays
out of scope.

---

## Read-only and dry-run operations — a third contract, not level 1/2

Two operations added after the Milestone 0–3 gate (`revit_mcp/impact.py`, first
deliverable of the deferred relationship/impact-analysis phase) answer "what is
this connected to" and "what would deleting it actually do." Neither fits the
level-1/level-2 vocabulary above, because neither is a mutation:

### `analyze_relationships` (`impact.py`)

```
tx     : none - opens no Transaction at all
post   : not_checked, structurally - there is no committed state to verify
method : GetDependentElements(None), JoinGeometryUtils.GetJoinedElements,
         Host/hosted-by (one FilteredElementCollector pass over
         FamilyInstances, grouped by host id), room-boundary membership
         (one pass over Room.GetBoundarySegments), Document.GetRoomAtPoint
tol    : n/a
limits : this is Revit's own *informational* dependency graph, not a
         prediction of what an actual delete would remove. Measured live
         2026-08-31: every relationship call is cheap at this project's
         model scale (GetDependentElements ~0.4ms/element, GetJoinedElements
         ~0.4ms/element, GetBoundarySegments ~0.3ms/room over 44 walls / 16
         rooms) but cost was not measured on a large model. Also measured:
         this project's model has zero wall-to-wall geometry joins at all
         (confirmed independently via AreElementsJoined on every
         geometrically-adjacent wall pair) - a real, if unflattering,
         property of how this course model was built, not a bug in the
         relationship code. Treat this tool's output as a map, not a
         guarantee - see preview_delete_impact for the authoritative
         version of "what would actually be removed."
```

### `preview_delete_impact` (`impact.py`)

```
tx     : DB.Transaction, but never committed - always t.RollBack(),
         unconditionally, in a finally block. This is the one handler in
         the project whose entire contract is "ends in RolledBack", the
         opposite of what commit_verified checks for - so it deliberately
         does not call commit_verified at all.
post   : the real doc.Delete() cascade is captured (would_delete_ids,
         cascaded_ids), then rolled back
method : live-verified 2026-08-31 through the actual wired handler (fresh
         re-import + fake-API harness, same technique Milestone 1 used):
         deleting wall 1658707 (which hosts door 1660218) correctly
         reported would_delete_count=2, cascaded_ids=[1660218], tx_status
         "RolledBack" - and both elements were confirmed via a direct
         doc.GetElement() check to still exist immediately afterward
tol    : n/a
limits : element descriptions for cascaded ids are captured from a
         pre-delete GetDependentElements(None) probe taken BEFORE anything
         is actually deleted - once doc.Delete() runs mid-transaction,
         cascaded ids stop resolving via doc.GetElement() immediately,
         well before RollBack() is ever called. A cascade id the probe
         didn't surface falls back to "Unknown (already removed)" rather
         than a fabricated description. GetDependentElements is explicitly
         one level and informational; it is used here only to pre-label
         ids, never trusted as the answer to "what would be deleted" -
         that answer is doc.Delete()'s own return value.
```

Both are intentionally excluded from the `verified.{ok,method,expected,actual}`
schema above: that schema exists to answer "did this mutation's own contract
hold," and neither of these two operations mutates anything that persists.

### `validate_design` (`validation.py`)

**This engine has no jurisdiction of its own.** Every regulatory concept — which
room types exist, their minimum area, and any room type needing checks beyond
area — is data, supplied at runtime from a rules file this repo does not ship.
**A project should point `rules_path` at whichever building code actually
applies to it** — a room-size regulation is a property of where the building
is, not of this tool, and nothing here assumes one country's numbers apply
everywhere. Two projects in two different countries use the same engine with
two completely different rules files; neither is the "real" one.

```
tx     : none - opens no Transaction at all
post   : not_checked, structurally - there is no committed state to verify
method : per-room checks against an external, private, locale-supplied rules
         file (never committed to this repo - see module docstring). Each
         finding is tagged fact / assumption / pass / violation / warning /
         not_checked, never collapsed into a single pass/fail:
           - room-type minimum area: room-type is INFERRED from the room's
             free-text name (an assumption, named explicitly in the output),
             then its area is checked against that type's minimum
           - extended checks: any room type whose rule opts in via an
             `extended_checks` block (a building code's own special-purpose
             room - a protected space, a wet room, whatever that code calls
             out by name) also gets net area, bounding-wall thickness (via
             the same GetBoundarySegments walk analyze_relationships uses,
             scoped to one room), ceiling height (Room's "Unbounded Height"
             parameter), and volume (BuiltInParameter.ROOM_VOLUME, if volume
             computation is enabled for the model) checked - nothing in the
             code names or assumes which room type this applies to
tol    : n/a - each rule's own threshold, read from the rules file, not
         hardcoded in this engine
extra  : optional `extra_rules` body field (Milestone 5 of the M1-M5
         architecture upgrade) - {"room_types": [...]}, same shape as the
         rules file, merged over it by "id" (an id in both: extra_rules
         wins; an id only in extra_rules: added; see
         revit_mcp/validation.py::_merge_extra_rules). Request-scoped only
         - never written to disk, never mutates the loaded rules object,
         does not leak between calls. Lets a project-specific constraint
         (e.g. one recorded in that project's design_state.json) be
         checked per call with no file-path plumbing, still with NO
         jurisdiction data of any kind in this repo.
limits : ROOM WIDTH, window-area-vs-floor-area rules, and the kitchen
         work-triangle are NOT checked - explicit not_checked findings, not
         silent gaps. "Unbounded Height" approximates but is not
         independently confirmed against a modeled Ceiling element.
         Room.Area's relationship to "net area" depends on the project's
         Area Boundary Location setting, which this engine does not assert
         generally - see docs/architecture.md's Measured traps for what one
         project measured. Live-verified 2026-08-31 through the actual wired
         handler against a real model, including both a room type using
         extended_checks and one using only the generic area minimum - both
         correctly reported real, previously-unknown violations, not
         synthetic test cases (see the project's private notes for figures;
         none are reproduced here, consistent with this section's own point).
         Two real bugs were found and fixed by this same live verification,
         not by the offline test suite - see docs/architecture.md's
         "Measured traps": IronPython's json.loads not UTF-8-auto-detecting
         raw bytes the way CPython's does, and an int loaded from JSON
         crashing a `{:.0f}` format spec that a float value would not have.
```

**Rules file schema** (no real numbers below — this is the shape, not a standard):

```jsonc
{
  "source": "<name/citation of the building code this file encodes>",
  "room_types": [
    {"id": "kitchen", "match_keywords": ["<locale's word(s) for kitchen>"],
     "min_area_sqm": 0.0, "min_width_m": 0.0},
    {"id": "<a locale's special-purpose room, e.g. a protected space>",
     "match_keywords": ["<its local name>"],
     "extended_checks": {
       "net_area_sqm": 0.0, "relief_net_area_sqm": 0.0,
       "wall_thickness_mm": 0, "ceiling_height_m": [0.0, 0.0],
       "volume_cum": 0.0, "width_m": 0.0
     }}
  ]
}
```

`match_keywords` order matters: list more specific room types before generic
catch-alls, since the first rule whose keyword appears in the room's name wins
(see `_match_room_type` and its tests). Default path:
`~/.claude/revit-design-rules/room_standards.json`; override with `rules_path`
per call for a multi-jurisdiction portfolio.

---

## What is deliberately out of scope for this matrix

- **Rotation/mirror geometric verification**, **schedule field verification**,
  **dimension value verification**, **tag leader position verification**,
  **view-override read-back** — all honestly `not_checked` above, not silently
  assumed. Adding any of these is new level-2 work, not a fix to this milestone.
- **Elevation verification for `create_level`** — deferred; category+existence only.
- **`load_family`** — manages its own transaction internally (`doc.LoadFamily`);
  no `Transaction` object is ever constructed in this handler, so `commit_verified`
  does not apply. Its own return value (`ok`/bool) is Revit's own success signal.
- **Move/rotate impact preview** — `preview_delete_impact` covers deletion, where
  `doc.Delete()`'s own return value gives an authoritative affected-id list for
  free. No equivalent single authoritative call exists for "what would moving
  this wall disconnect" — `analyze_relationships`'s static join/host/dependent
  data is the closest available signal today, with the limits documented above.
- **Relationship history** (did this element's joins/host change since last
  session) — would need the relationship data captured into the snapshot itself,
  following the `pv`-blob precedent (`tracker.py`'s `diff_snapshots` already
  excludes the blob field from comparison). Not built this session: it answers a
  different question (drift over time) than the live queries above (current
  state), and the live queries were the higher-value, lower-risk first step —
  zero snapshot-format risk, since they touch no persisted format at all.

## Updating this matrix

Per the project's scope rule: **this matrix is authoritative for level-2 scope.**
Code must not silently add a post-condition this file does not describe. If
implementation reveals a contract here is incomplete or wrong, update this file
first, then the code — not the other way around.
