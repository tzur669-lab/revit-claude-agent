# Connection and memory

**How Claude talks to Revit, and what was built on top of that connection so it remembers**

Revit remembers nothing beyond the open session, and the MCP tools themselves
keep no history. This document describes two layers: the raw connection to Revit,
and the system built on top of it to give it memory.

Based on a reading of the actual code and documentation on 2026-08-29, and on
memory records from 2026-08-16 to 2026-08-23. Some details in the memory records
are point-in-time observations from when they were written — before relying on a
specific filename or line, verify against the live code.

Contents: [Part 1 — the connection](#part-1--the-connection-how-claude-touches-revit-at-all) ·
[Part 2 — the agent](#part-2--the-agent-what-was-built-on-top) ·
[Measured traps](#measured-traps)

---

## Part 1 — the connection: how Claude touches Revit at all

The raw layer: an external MCP server that talks to an add-in inside Revit
itself. Without this layer there is no layer 2.

Two separate runtimes communicate over local HTTP. `Claude Code` calls a Python
server running on the machine; the server sends a request to `pyRevit Routes`,
which runs **inside the Revit process itself** in `IronPython 2.7` and talks
directly to the Revit API. That is why Revit must be open with a project loaded —
no Revit process, no one to answer the request.

```
AI Client (Claude Code)  →  MCP Server (Python 3.11+, FastMCP)  →  pyRevit Routes (HTTP :48884)  →  Revit API (IronPython 2.7, inside Revit)
```

| Component | Runtime | Location | Role |
|---|---|---|---|
| `main.py` + `tools/` | CPython 3.11+ | The machine | MCP protocol, tool definitions |
| `startup.py` + `revit_mcp/` | IronPython 2.7 | Inside the Revit process | route handlers, API calls |

### 48 tools, six categories

All tools take dimensions in millimeters — the conversion to feet, Revit's
internal unit, happens inside the server. Revit versions 2024–2027 are supported
through central helper functions that absorb the `ElementId` API differences
between versions, auto-detected at runtime — no manual configuration.

| Count | Category | Examples |
|---|---|---|
| 15 | Creation | `create_level` · `place_family` · `create_room` · `create_duct` |
| 12 | Query | `list_levels` · `get_element_properties` · `get_revit_view` |
| 8 | Editing | `modify_element` · `transform_elements` · `tag_elements` |
| 5 | Analysis | `check_clashes` · `analyze_model_statistics` |
| 3 | Documentation | `create_dimensions` · `export_document` |
| 4 | Interop and save | `export_ifc` · `link_file` · `save_document` |
| 1 | Advanced | `execute_revit_code` — run arbitrary IronPython inside Revit |

### Companion tool: a manual pipe into Revit

Alongside the MCP channel sits a separate pyRevit extension called
`BIMAgents.extension`, with a `ClaudeIntegration` tab. Its one button so far,
`Extract QTO Data`, is the reverse direction: a human selects beams and slabs in
Revit, clicks the button, and the script packages the quantities (meters, m², m³)
as JSON on the clipboard, ready to paste into a conversation with Claude. Not
part of the agent loop — a standalone helper for the reverse direction: a human
prepares data for Claude, not Claude acting in Revit.

> **gotcha — hebrew encoding**
> Non-ASCII text typed directly as a parameter to any `mcp__revit__` tool is
> corrupted before it reaches IronPython: classic double-encoding, every
> character becomes two wrong bytes, and the comparison fails silently — 0
> results, not an error. Inside `execute_revit_code` the fix is fully reversible:
> `s.encode("latin-1").decode("utf-8")`. In the text parameters of the friendly
> tools (`type_name`, `level_name`) there is no such fix — there you must
> identify by numeric `ElementId`, not by name.
>
> This repo's fork applies a related fix on the Revit side: `_safe_str` /
> `normalize_string` / `sanitize_string` no longer strip non-ASCII to `?`, so
> Hebrew read **out** of the model survives. See `mcp-server/PATCH-NOTES.md`.

---

## Part 2 — the agent: what was built on top

The MCP tools are stateless: every call forgets the one before it. This layer was
built to answer three questions the raw connection cannot — what happened here
since last time, who did it, and how do you know an operation that reported
"success" actually happened.

### Three working assumptions the system is built around

- **Absence of an error is not proof the operation happened.** Measured:
  `MoveElement` returned success without moving anything. Every mutation is
  verified against the diff of the next checkpoint — not against the tool's
  return code.
- **A 500 response carrying `TRACKER_OK` is planned success, not failure.**
  Read-only passes deliberately raise at the end to force Revit to roll back the
  host transaction — so a state capture never pollutes the user's Undo. A read
  counts as successful only when both conditions hold: the error message contains
  `TRACKER_OK|run_id`, and `last_run.json` carries that exact `run_id`.
- **Project identity is by canonical path only, not by an internal id.** The
  user's three course-exercise files share the exact same
  `ProjectInformation.UniqueId`, because they were all created from the same
  template — the id identifies the template, not the project.

### Four attribution states

A `DocumentChanged` listener catches every change in the model and maps it to one
of four states, by the transaction name Revit itself recorded:

`claude` · `human` · `sync_incoming` · `unknown`

`unknown` is not equivalent to `human` — that is an explicit principle in the
system. It only indicates that the listener was not armed when the change
happened (it fires on commit, after the code has already finished running, so an
event is never seen in the same call that created it).

> **gotcha — attribution**
> The original detection looked for the string `MCP` in the transaction name. Of
> the extension's 28 transactions, 23 carry `MCP` and 5 do not — so real Claude
> work through the friendly tools was wrongly reported as `human`. The fix: a
> closed list of five exact names, derived from the extension source and not
> guessed, compared by exact match — because a wrong value here attributes human
> work to us, the one mistake the system must not make.

### The tracker's build timeline

1. **Phase 1** — document identification by canonical path, a full snapshot with
   geometry and parameter hashes, a diff with lazy parsing, atomic writes.
2. **Phase 2** — a `DocumentChanged` listener, transaction records, and the
   attribution engine for the four states.
3. **Phase 3** — automatic activation through a global `CLAUDE.md` and a
   `PreToolUse` hook — no need to remember to run it.
4. **Phase 4** — chunks with resume, a `.lock` file with a numeric epoch (not a
   time string — `DateTime.Parse` reads a `Z` suffix as local time), a sync
   window with expiry.
5. **Later** — a split into two background agents, a journal format migration
   from Hebrew prose to English `ndjson`, and a global cross-project lessons board.

### Two background agents, and why not three

`mcp__revit__` is a single channel to a single Revit process, and the tracker
locks the instance directory. Any additional agent that tried to touch Revit
would collide on the lock and slow the work down. The existing split is by
**resource** — disk and prose leave the critical path — not by task.

| Agent | Model | Tools | Role |
|---|---|---|---|
| `revit-historian` | sonnet | `Read`/`Glob`/`Grep` | Reconstructs a history briefing from disk, in parallel with `session_start`. Up to 15 lines, read-only, proposes no next steps. |
| `revit-scribe` | opus | `Read`/`Write`/`Edit`/`Bash` | The sole writer of the journal. Drains the `writeups/pending/` queue, writes `journal.ndjson`/`state.json` in the background, holds a `.scribe.lock` separate from the tracker's `.lock`. |

A key rule for the scribe: **the delta file is the source of truth** for every
fact about the model — the queue item contributes only the intent. A case was
measured where a queue item claimed `claude:2, unknown:0` while the delta and
`last_run.json` both said `claude:1, unknown:1` — a manual-copy error, caught
only because cross-checking against the delta is mandatory, not a sanity check.

### File ownership — one writer per file

| Owner | Files |
|---|---|
| `tracker.py` (Revit side) | `snapshot.tsv` · `events.ndjson` · `deltas/` · `_index.json` · `.lock` |
| `revit-scribe` only | `journal.ndjson` · `state.json` · `log/` · `.scribe.lock` |
| The main thread | `writeups/pending/` (creation) · `README.md` · `RULES.md` (only after user approval) |

### Global cross-project lessons board

`revit-lessons/RULES.md` is written **only** after an explicit user correction or
an approval by the user — not after a bug caught alone, and not after a technical
tool trap. Every line is written in the format
`id :: trigger :: rule :: check :: src :: ev`, with `trigger` from a closed
vocabulary of 17 domains (level creation, wall layout, family placement, MEP
routing, etc.), phrased as an activity that is starting — not as a symptom that
was discovered — so it is pulled **before** the work, not only after. The file is
injected into context automatically at the start of every session. As of today it
holds **one** rule.

<details>
<summary>Why the journal moved from Hebrew prose to English JSON</summary>

Until 2026-08-23 the journal was `PROJECT.md`/`STATE.md` in Hebrew prose. It was
replaced with `journal.ndjson`/`state.json`, one record per line, in English. The
reason: the only consumer of the files is Claude, not a human — Hebrew costs 2–3×
the tokens for the same content, with no reading benefit. The two existing
instances were frozen to `legacy/`; no information was deleted, and the
conversion did not touch `tracker.py` at all — the old and new formats sit on top
of the same `snapshot.tsv`.

</details>

---

## Measured traps

All silent — no error message. That is exactly why the system is built on
verification against a diff, not on a return code.

<details>
<summary>Family placement — furniture ends up inside a wall, at double height, or stuck at z=0</summary>

Measured on 268 furniture items across four floors, three different causes, all
with no API error:

1. The family's insertion point sits at the back of the item, not its center —
   104 items ended up inside walls. The fix: clamp computationally by
   `GetRoomAtPoint` and the bounding box, do not guess.
2. `elevation-from-level` receives the level elevation, so some families add it a
   second time — 116 items landed at double height. The fix: zero the
   `INSTANCE_ELEVATION_PARAM` after placement, because a Z move alone does not
   survive regeneration.
3. `WorkPlaneBased` families get stuck at z=0 with a read-only parameter — the
   only thing that worked was swapping the family to a `OneLevelBased` type.

</details>

<details>
<summary>A volatile parameter reported "changed" on every session open for no reason</summary>

A `3D View`/`Cameras` pair came back `modified` on every open. The finding:
`VIEWER_TARGET_ELEVATION` and `VIEWER_EYE_ELEVATION` — the camera position, which
Revit recomputes itself on every document open. They were added to a list of
volatile parameters neutralized from the diff; a deliberate camera move is still
visible, because it touches geometry, not just a parameter.

</details>

<details>
<summary>A lock that looks fresh forever</summary>

`System.DateTime.Parse` on a string ending in `Z` returns **local** time, not
UTC. Comparing against `UtcNow` in a UTC+3 time zone always comes out negative,
and a lock file looks valid forever. The fix: numeric epoch timestamps in every
locking mechanism in the system, not formatted time strings.

</details>

<details>
<summary>A temporary change to a view needs a full restore, not just the name</summary>

To read a view with a Hebrew name through `get_revit_view` (which suffers the same
encoding bug), the fix was to temporarily change the name to ASCII, read, and
restore. In one case the name was restored but `VIEW_DETAIL_LEVEL` stayed `Fine`
instead of `Medium` — caught only in the diff of the next checkpoint. Save every
field you touch, not just what you deliberately changed.

</details>

---

**Sources:** `mcp-server/README.md` · `memory-system/tracker/tracker.py` ·
`memory-system/claude/skills/revit-session/SKILL.md` ·
`memory-system/claude/agents/revit-historian.md` ·
`memory-system/claude/agents/revit-scribe.md` ·
`memory-system/claude/revit-lessons/RULES.md` · memory records from 2026-08-16–23.
