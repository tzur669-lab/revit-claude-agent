# Architecture

How Claude talks to Revit (the **connection**), and what sits on top so the work is
verified and remembered (the **memory**). Current-state only — the *why* behind each
Phase A/B mechanism is in [history/design-log.md](history/design-log.md); silent
failures found live are in [measured-traps.md](measured-traps.md).

> `tests/test_tool_registry.py` parses the tool table below (its rows, and the
> `### <N> tools, <word> categories` heading). Don't add other table rows that start
> with a bare number cell to this file, and update the table if a tool is added.

---

## System diagram

```
            ┌──────────────────────── Claude Code (main thread) ────────────────────────┐
            │  revit-session skill (protocol)   PreToolUse hook (reminder + RULES.md)   │
            │  bg agents: revit-historian (read disk)   revit-scribe (write journal)    │
            └───────────────┬───────────────────────────────────────────────────────────┘
                            │ MCP (stdio)
                            ▼
  mcp-server/  main.py + tools/          CPython 3.11+, FastMCP   — on the machine
     │   tools/plan_tools.py: propose/get_task_status/rollback  ──► ~/.claude/revit-tracker/tasks/<id>/
     │   (no Revit call; read/write task files directly)             ledger.ndjson (truth) + progress.json
     │ HTTP :48884 (auto-discovered, main.py::_discover_revit_port)
     ▼
  pyRevit Routes  startup.py + revit_mcp/*.py   IronPython 2.7 — INSIDE Revit.exe
     │   one route module per domain; every POST handler: repair_hebrew_in → work → commit_verified → verified{}
     ▼
  Revit API  ◄── tracker.py (run via execute_revit_code / execfile) — snapshot · diff · attribution
                     └─► <TRACKING_DATA_DIR>/<project>/  snapshot.tsv, deltas/, events.ndjson …
                     └─► ~/.claude/revit-tracker/last_run.json
```

Side channel, not in the agent loop: `pyrevit-extension/BIMAgents.extension` —
one ribbon button (*Extract QTO Data*) that copies a human's beam/slab selection as
JSON to the clipboard for pasting into Claude.

---

## Part 1 — the connection

| Component | Runtime | Location | Role |
|---|---|---|---|
| `main.py` + `tools/` | CPython 3.11+ | the machine | MCP protocol, tool definitions (`tools/registry.py` = metadata source of truth) |
| `startup.py` + `revit_mcp/` | IronPython 2.7 | inside Revit | route handlers, Revit API calls |

Units: every tool takes **mm**; conversion to feet is inside the server. Revit
2024–2027 `ElementId` differences are absorbed by central helpers, auto-detected.

### 57 tools, seven categories

| Count | Category | Examples |
|---|---|---|
| 15 | Creation | `create_level` · `place_family` · `create_room` · `create_duct` |
| 12 | Query | `list_levels` · `get_element_properties` · `get_revit_view` |
| 9 | Editing | `modify_element` · `transform_elements` · `tag_elements` |
| 8 | Analysis | `check_clashes` · `analyze_model_statistics` · `analyze_relationships` · `preview_delete_impact` · `validate_design` |
| 2 | Documentation | `create_dimensions` · `export_document` |
| 4 | Interop and save | `export_ifc` · `link_file` · `save_document` |
| 7 | Advanced | `execute_revit_code` (arbitrary IronPython) · `propose_plan` / `approve_plan` / `execute_plan` / `get_task_status` / `cancel_task` / `rollback_task` (execution spine) |

Full per-tool list (generated): [TOOLS.md](TOOLS.md).

### Route-layer conventions (every mutating handler)

1. `data = repair_hebrew_in(data)` first — pyRevit core decodes request bodies as
   Latin-1; this reverses it. Per-handler, not one shared patch, because pyRevit runs
   handlers in a different engine scope than `startup.py` (see its header comment).
2. `commit_verified(t)` instead of bare `t.Commit()` → tri-state `tx_ok`.
3. Return a `verified: {ok, method, expected, actual}` (or `not_checked` + reason).
4. `#!notx` as the first line of `execute_revit_code` = no wrapping transaction
   (needed for `StairsEditScope` / `SketchEditScope`).

Upstream diff and reasons: [../mcp-server/PATCH-NOTES.md](../mcp-server/PATCH-NOTES.md).

---

## Part 2 — the memory and verification layer

### Three verification levels (none subsumes another)

| Level | Question | Where | Catches |
|---|---|---|---|
| **Transaction** (L1) | Did Revit commit? | `commit_verified()` in every handler | `Commit()` ≠ `Committed` without raising |
| **Post-condition** (L2) | Did this op meet its own contract? | per handler — [operation-contracts.md](operation-contracts.md) | Revit committing while silently declining/clamping |
| **Intent** (L3) | Did the *batch* do what the user asked? | tracker checkpoint diff + queue item `user_intent` | every call passes L1/L2 but the aggregate is wrong |

L3 is the authority for "did the work succeed". **Impact analysis** is a separate,
before-the-fact question: `analyze_relationships` (static dependency read) and
`preview_delete_impact` (real `doc.Delete()` in an always-rolled-back transaction).

### Tracker (`memory-system/tracker/tracker.py`, runs inside Revit)

- Ops: `smoke` · `session_start` · `checkpoint`. Identity = **canonical file path**
  (template-derived files share `ProjectInformation.UniqueId`).
- Read-only passes end by raising → HTTP 500 with `TRACKER_OK|<run_id>` = success,
  keeps snapshots off Undo. Confirm via `last_run.json.run`.
- Attribution by transaction name: `claude` (name contains `MCP` **or** is in the
  closed list `MCP_TX_NAMES`, 5 names) · `human` · `sync_incoming` · `unknown`.
  `unknown` is never upgraded to `human`.
- `SNAPSHOT_FORMAT` must be bumped when record computation changes.

### Agents and file ownership (one writer per file)

| Player | Runs | Revit access | Writes |
|---|---|---|---|
| main thread | conversation | yes (only one) | `writeups/pending/` items, `README.md`, `RULES.md` (user-gated) |
| `tracker.py` | inside Revit | yes | `snapshot.tsv`, `snapshot.hdr.json`, `events.ndjson`, `deltas/`, `_index.json`, `.lock` |
| `revit-historian` (sonnet) | background, parallel to `session_start` | no | nothing — ≤15-line briefing |
| `revit-scribe` (opus) | background, after checkpoint | no | `journal.ndjson`, `state.json`, `design_state.json`, `log/`, `.scribe.lock` |

Why only two agents: `mcp__revit__` is one channel to one Revit process and the
tracker locks the instance dir; the split is by **resource** (disk/prose off the
critical path), not by task. Scribe rule: the **delta file** is the source of truth
for facts; the queue item contributes intent only.

### Five kinds of memory

| Memory | Home |
|---|---|
| Model (what exists now) | live via `mcp__revit__`, or reconstructed from `snapshot.tsv` |
| Project history | `journal.ndjson` (append-only) · `state.json` (rollup) |
| Intent (goals, preferences) | `design_state.json` `kind: goal/preference` |
| Constraint | project: `design_state.json` `kind: constraint` · global: `RULES.md` `check` |
| Learned design | `~/.claude/revit-lessons/RULES.md` — only after user correction/approval |

Schemas: [`project-log-format.md`](../memory-system/claude/skills/revit-session/references/project-log-format.md).
Lessons gate/format: [`lessons.md`](../memory-system/claude/skills/revit-session/references/lessons.md).

---

## The execution spine: plan/task lifecycle

A durable, file-backed **task** that survives client timeouts, paused slices, and
Revit restarts — without changing the 51 independent tools.

```
propose_plan ──► approve_plan ──► execute_plan (1 slice = 1 DB.TransactionGroup) ──► get_task_status
 validate+hash    preflight route     revit_mcp/plan_exec.py, budgeted                 reads disk only
 no Revit call    stamps doc identity                                                   rollback_task → builds a
                                                                                         compensation PLAN, runs nothing
```

- State: `~/.claude/revit-tracker/tasks/<task_id>/`. `ledger.ndjson` is the single
  source of truth (append-only, fail-stop); `progress.json` is a disposable fold
  (`task_store.fold_progress`).
- Ops (`revit_mcp/ops.py`, schema twin `tools/ops_schema.py`): `move_elements` is
  **absolute** (idempotent on resume); the 4 creating ops are `"creating"` and need a
  `probe()` before resume — inconclusive ⇒ block for a human.
- Crash-resume identity: `commit_result` ledger record (authoritative) → universe-diff
  heuristic (`eval_support.probe_outcome_from_universe`) → `unknown`. Conflicting
  records block, never "last one wins".
- `success_criteria`: 5 evaluators (`revit_mcp/evaluators.py`), 8 named statuses,
  `design_ok` = `eval_support.aggregate_design_ok`; targets may be `element` or
  `created_by` (a plan action's output).
- Unexpected-change classifier (`tools/plan_tools.py::_check_unexpected_changes`):
  only positive human/sync evidence counts as foreign; `unknown` → `"unexpected"`.

Contracts: [operation-contracts.md § execution spine](operation-contracts.md#the-phase-a-execution-spine--propose_plan--approve_plan--execute_plan--get_task_status).
Deliberately not built: semantic BIM graph, model health scoring, visual
verification, autonomous repair, action-to-action targeting.

### Phase B (slices 1–3)

Rationale and live-verification records for success criteria, `created_by`, and the
crash-resume evidence hierarchy (incl. the residual-risk note and the
`_room_probe` false-`applied` fix) moved to
[history/design-log.md](history/design-log.md#phase-b-slice-1-success-criteria-become-real).

---

## Coupled vs. intentionally isolated

| Coupled (change together) | Why |
|---|---|
| `tools/registry.py` ↔ `docs/TOOLS.md` ↔ this file's tool table ↔ `mcp-server/README.md` counts | enforced by `tests/test_tool_registry.py`; regenerate with `scripts/gen_tool_docs.py` |
| `revit_mcp/ops.py` ↔ `tools/ops_schema.py` | data twin of the op/evaluator arg schema |
| route transaction names ↔ `tracker.py::MCP_TX_NAMES` | attribution; re-derive, never guess (`tests/test_transaction_names.py`) |
| `revit_mcp/*` verification shape ↔ `operation-contracts.md` | contract doc is updated **first**, then code |
| `revit-session` skill paths ↔ `scripts/install.ps1` | installer substitutes `TRACKING_DATA_DIR` |

| Isolated on purpose | Why |
|---|---|
| historian/scribe have no Revit access | single Revit channel + tracker lock |
| tracker path pure ASCII (`# -*- coding: ascii -*-`) | non-ASCII on the wire fails silently |
| `propose_plan`/`get_task_status`/`rollback_task` never call Revit | status must work while Revit is busy/down |
| `RULES.md` vs `design_state.json` | global user-gated lessons vs project-only constraints |
| tracking data dir outside the repo | real project names and snapshots |

---

## Measured traps

Silent-failure catalogue (family placement, volatile camera params, `Z`-suffix lock
times, view-restore, IronPython UTF-8 `json.loads`, int-with-float-format):
[measured-traps.md](measured-traps.md).
