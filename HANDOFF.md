# HANDOFF — revit-claude-agent

**Read this first, every session.** Everything else is linked; open only what the task needs ([docs/README.md](docs/README.md) = index).

## Purpose
Let Claude Code build/edit/query a **live Revit model** (57 MCP tools) and keep a **verified, attributed memory** of every change — including humans' manual edits — across sessions. Windows, Revit 2024–2027. Local git repo since 2026-11-10 (no remote yet).

**Active plan:** [docs/plans/design-intelligence.md](docs/plans/design-intelligence.md) — 10 sessions, read only its status table + the current session. Session 1 is blocked on this machine (see below).

## Machine readiness
Run `scripts/check-readiness.ps1` (read-only) before `install.ps1` or any live check. Last run 2026-11-10: **NOT READY** — no Revit, no pyRevit, extension not registered, no `*revit*` MCP server in `~/.claude.json`, Python `mcp` package missing. Python 3.12.10 present. Nothing installed under `~/.claude` yet. Paths to record once installed: Revit version, pyRevit path, Routes port, `TrackingDataDir` (work) and `REVIT_TRACKER_DIR` (tests, separate).

## Layers
| Layer | Path | Runtime | Responsibility |
|---|---|---|---|
| MCP server | `mcp-server/main.py`, `tools/` | CPython 3.11, FastMCP | tool defs (`tools/registry.py` = metadata truth), port discovery, plan/task tools |
| Route handlers | `mcp-server/startup.py`, `revit_mcp/` | IronPython 2.7 **inside Revit** via pyRevit Routes :48884 | Revit API work, L1/L2 verification |
| Tracker | `memory-system/tracker/tracker.py` | IronPython, run through `execute_revit_code` | snapshot · diff · attribution |
| Protocol | `memory-system/claude/skills/revit-session/` | Claude Code skill + `PreToolUse` hook | when to snapshot/checkpoint/log |
| Agents | `memory-system/claude/agents/` | background, **no Revit access** | historian (read history), scribe (sole journal writer) |
| QTO button | `pyrevit-extension/` | pyRevit ribbon | manual clipboard export; not in the loop |

Flow: user ask → `session_start` snapshot (+ historian in parallel) → tools → `checkpoint` diff = verification → queue item (intent) → scribe merges delta (facts) into `journal.ndjson`/`state.json`. Plans: `propose_plan → approve_plan → execute_plan → get_task_status`, state in `~/.claude/revit-tracker/tasks/<id>/ledger.ndjson`. Details: [docs/architecture.md](docs/architecture.md).

## Invariants — must not break
1. **Success = the checkpoint diff**, never a tool's return code (`MoveElement` measured "succeeding" with no move).
2. **HTTP 500 containing `TRACKER_OK|<run_id>` is success**; confirm `last_run.json.run == run_id`. Never retry it.
3. **`unknown` is never upgraded to `human`** — in `tracker.attribute()` *and* `plan_tools._check_unexpected_changes`.
4. `tx_ok` is tri-state: test `tx_ok is False`, never `not tx_ok` (`None` = `#!notx`, self-managed).
5. **One writer per file** (tracker / scribe / main thread — table in architecture.md). Delta file = facts; queue item = intent only.
6. `RULES.md` written **only** on explicit user correction/approval, immediately (not via queue), with `src:` + date, under `.lessons.lock`.
7. Project identity = **canonical path**, not `UniqueId`.
8. Locks use **numeric epoch**, never ISO strings.
9. Task `ledger.ndjson` is truth and fail-stop; `progress.json` is a disposable fold. Conflicting `commit_result` records ⇒ block.
10. Creating ops are not idempotent: resume needs a probe; inconclusive ⇒ block, never guess an id.
11. Tracker path/source stays ASCII; non-ASCII paths go over the wire as escapes.

## If you touch X, be careful with Y
| Touch | Watch |
|---|---|
| add/remove/rename a tool | `tools/registry.py` → run `python scripts/gen_tool_docs.py`; update table in `docs/architecture.md` + counts in `mcp-server/README.md` (tests parse all three) |
| any POST handler | first line `data = repair_hebrew_in(data)`; use `commit_verified`; return `verified{}`; update `docs/operation-contracts.md` **first** |
| a transaction name | `tracker.py::MCP_TX_NAMES` + `tests/test_transaction_names.py` |
| snapshot record computation | bump `SNAPSHOT_FORMAT` (else phantom adds/deletes) |
| `ops.py` | its twin `tools/ops_schema.py`; no transactions inside ops (`test_ops_no_transactions.py`) |
| evaluators | read-only, no transactions (`test_evaluators_no_transactions.py`); `ok` derived from `status` |
| IronPython code | offline tests run CPython — int/float format specs and `json.loads(bytes)` behave differently ([docs/measured-traps.md](docs/measured-traps.md)) |
| crash-resume / probes / `created_by` | read [docs/history/design-log.md](docs/history/design-log.md) Phase B slice 3 first |
| skill/agent docs | they are runtime prompts — keep format/paths; `install.ps1` substitutes `TRACKING_DATA_DIR` |

## Testing
`cd mcp-server && python -m pytest -q --ignore=tests/test_unexpected_changes.py` → 443 pass, 1 skip (2026-11-10). That one file needs the `mcp` package; `uv sync` currently fails building `pydantic-core` on this machine. Offline tests never fake `FilteredElementCollector`; Revit-side behaviour needs a live session.

## Open doc gaps (known, unresolved)
- `references/hebrew-io.md` predates the inbound Hebrew fix — flagged in-file; needs one live check before rewrite.

## Session log
- **2026-11-10 — plan + session 1 (partial).** Wrote `docs/plans/design-intelligence.md`. `git init`; `.gitignore` += `.pytest_cache/`; new read-only `scripts/check-readiness.ps1`; `uninstall.ps1 -IncludeLessons` now also removes `PREFERENCES.md`, `revit-design-rules/`, `revit-backups/` (default leaves them). Tests 443 pass / 1 skip. Install + live steps blocked by readiness gate.
- **2026-11-10 — docs restructure (no code change).** Added this file + `docs/README.md`. Cut `docs/architecture.md` 760→~200 lines; moved Phase A/B rationale verbatim to `docs/history/design-log.md` and traps to `docs/measured-traps.md` (stub sections left so code-comment references still resolve). Fixed stale: `agent-workflow.md` Hebrew claim, `attribution.md` `claude` rule, `PATCH-NOTES.md` call-site counts (22 files / 41 sites); flagged `hebrew-io.md`.
