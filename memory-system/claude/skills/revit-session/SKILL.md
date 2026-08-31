---
name: revit-session
description: Track and document work on Revit files across sessions. Use BEFORE any mcp__revit__ tool call - it identifies the open document, restores its history, detects changes made since last time (including changes a human made outside Claude), and logs every action with a timestamp. Triggers whenever the user asks to work on a Revit model, open/edit/inspect a .rvt, or asks what was done to a project previously.
---

# Revit Session Protocol

This is what makes "let's pick up where we left off yesterday" possible, and what
lets you tell the user "someone changed these three walls since last time".

## Configuration

Two paths are installation-specific. Set them once, here, after installing the skill:

| What | Default | Notes |
|---|---|---|
| Tracker library (ASCII path — never edit from inside Revit) | `~/.claude/revit-tracker/tracker.py` | Override with the `REVIT_TRACKER_DIR` env var. |
| Tracking data (user-visible) | `<TRACKING_DATA_DIR>` | e.g. `C:\Users\<you>\revit-tracking`. Pick a path you can browse. **Must be an absolute path.** |

Everything below assumes `<TRACKING_DATA_DIR>` has been replaced with your real path.

## The call

One line through `mcp__revit__execute_revit_code`. The (possibly non-ASCII) data
path is passed as escapes so the wire stays pure ASCII:

```python
execfile(r"C:\Users\<you>\.claude\revit-tracker\tracker.py"); main("OP", {"base": u"<TRACKING_DATA_DIR, as an escaped literal>"})
```

Ops: `smoke` (environment check) · `session_start` (open a session) · `checkpoint` (after work).

Optional args: `budget_ms` (default `18000`) · `scope` (`model` by default, or `all`).

## Critical: a 500 carrying `TRACKER_OK` is success

Read-only passes raise on purpose at the end. That forces Revit to roll back the
host transaction, so snapshots never pollute the user's Undo list. **The tool
will report an error. That is the planned, healthy path — do not retry.**

The call counts as successful only when both conditions hold:

1. The error message contains `TRACKER_OK|<run_id>`.
2. `last_run.json` exists and its `run` field exactly equals that `run_id`.

Always `Read` `last_run.json` afterwards. Never parse the payload out of the 500 body.

## Critical: absence of an error is not proof the operation happened

Measured: `MoveElement` returned successfully and moved nothing.

**Every mutation is verified against the diff of the next `checkpoint`** — it runs
regardless, so the verification is free. If the diff does not show what you
expected, do not tell the user the operation succeeded. The mechanism is
explained in `references/troubleshooting.md`.

## Before every batch: name the domain and apply RULES.md

`hook_session_reminder.py` injects the contents of
`~/.claude/revit-lessons/RULES.md` into context once at the start of the session
— no need to read it again. But the injection alone does nothing if nobody
checks it.

**Before starting a batch of work on the model** (placing families, creating
levels, building stairs, exporting a view, etc.): name the domain of that batch
(from the domain list at the top of `RULES.md`) and apply every rule carrying it,
**before** touching geometry. See `references/lessons.md` for the full detail —
especially how the trigger is phrased so it fires ahead of the work, not only in
hindsight.

This is not another file read — the list is already in context. It is a
one-line check.

## Opening a session — the two actions go out together

**In a single message:**
1. Call `session_start` through `mcp__revit__execute_revit_code`.
2. Launch the `revit-historian` agent **in the background** — it reconstructs
   history from disk in parallel. Hand it the expected canonical path if known.

Then:
3. `Read` `last_run.json`.
4. **Cross-check:** the tracker is the authority. If its `instance_id` does not
   match the one the historian returned — discard the briefing and read the
   correct `state.json` directly from `instance_dir`.
5. `notes` non-empty ⇒ `references/notes.md`. `diff` non-empty ⇒ `references/attribution.md`.
6. Report to the user. Never guess beyond the evidence.
7. **diff non-empty ⇒ a queue item is written here too.** A change detected at
   session open is exactly what the system exists for — work done outside
   Claude. Do not omit it from the journal just because we did not make it.

## During work — checkpoint

After every meaningful batch of changes: `checkpoint`. Its diff is also the
verification of the mutations.

The journal write does not happen here but into a queue on disk, which the
`revit-scribe` agent drains in the background.

**The rule is the same for both authors:** `status` is `complete` **and** the
diff is not all zeros ⇒ a queue item. This applies to `session_start` exactly as
to `checkpoint`. **Load `references/checkpoint-queue.md`** before the first item
of the session.

**State `expected` before executing a batch, not after.** Before running the
tool calls for a batch worth predicting (skip this for pure investigation/
read-only work), form one short sentence of what the batch should produce.
While executing, note each mutating tool's own `tx_status`/`verified.ok`/
`verified.method` from its response — this is what becomes `verified` in the
queue item. Writing `expected` retroactively from what actually happened
defeats its purpose: it exists to catch the gap between intent and outcome,
not to restate the outcome. See `references/checkpoint-queue.md` for the
exact fields and why they are optional.

## Correction from the user — write to RULES.md immediately

The user says something you did was wrong, or approves a lesson you proposed —
do not wait for the scribe's queue. Load `references/lessons.md` and write
directly, at that moment in the conversation. The gate, the line format, and the
lock are all there.

## File ownership — never write past the line

| Owner | Files |
|---|---|
| Revit side (`tracker.py`) | `snapshot.tsv`, `snapshot.hdr.json`, `events.ndjson`, `deltas/`, `_index.json`, `.lock` |
| `revit-scribe` only | `journal.ndjson`, `state.json`, `log/`, `.scribe.lock` |
| The main thread | `writeups/pending/` (creation), `README.md`, `RULES.md` (only after a user correction/approval) |

A single writer per file is what prevents collisions between the processes.

**Exception:** when `NEW_INSTANCE` arrives, the main thread creates the initial
`journal.ndjson`/`state.json` per the template in
`references/project-log-format.md`. From that moment both files belong to the scribe.

## Routing to references

| File | Load when |
|---|---|
| `references/notes.md` | `notes` came back non-empty |
| `references/attribution.md` | the diff is non-empty — before reporting who did what |
| `references/hebrew-io.md` | writing non-ASCII text **into** the model (room, level, type name) |
| `references/checkpoint-queue.md` | before the first `checkpoint` of the session — the queue-item schema and launching the scribe |
| `references/lessons.md` | a user correction/approval to be written to `RULES.md`, or any doubt about domain/trigger |
| `references/troubleshooting.md` | `partial`, `LOCKED_BY`, a 500 without `TRACKER_OK`, or a mutation that never showed in the diff |
| `references/project-log-format.md` | creating a new `journal.ndjson`/`state.json` (otherwise — only the scribe loads it) |
