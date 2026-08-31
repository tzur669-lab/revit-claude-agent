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
