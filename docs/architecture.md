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

### 51 tools, seven categories

All tools take dimensions in millimeters — the conversion to feet, Revit's
internal unit, happens inside the server. Revit versions 2024–2027 are supported
through central helper functions that absorb the `ElementId` API differences
between versions, auto-detected at runtime — no manual configuration.

| Count | Category | Examples |
|---|---|---|
| 15 | Creation | `create_level` · `place_family` · `create_room` · `create_duct` |
| 12 | Query | `list_levels` · `get_element_properties` · `get_revit_view` |
| 9 | Editing | `modify_element` · `transform_elements` · `tag_elements` |
| 8 | Analysis | `check_clashes` · `analyze_model_statistics` · `analyze_relationships` · `preview_delete_impact` · `validate_design` |
| 2 | Documentation | `create_dimensions` · `export_document` |
| 4 | Interop and save | `export_ifc` · `link_file` · `save_document` |
| 1 | Advanced | `execute_revit_code` — run arbitrary IronPython inside Revit |

`analyze_relationships` and `preview_delete_impact` (`revit_mcp/impact.py`) are the
first deliverable past the Milestone 0–3 hard gate; `validate_design`
(`revit_mcp/validation.py`) is the second — see
[operation-contracts.md](operation-contracts.md#read-only-and-dry-run-operations--a-third-contract-not-level-12)
for all three tools' contracts, and the "Impact analysis" section below for how
the first two relate to the three verification levels.

### Companion tool: a manual pipe into Revit

Alongside the MCP channel sits a separate pyRevit extension called
`BIMAgents.extension`, with a `ClaudeIntegration` tab. Its one button so far,
`Extract QTO Data`, is the reverse direction: a human selects beams and slabs in
Revit, clicks the button, and the script packages the quantities (meters, m², m³)
as JSON on the clipboard, ready to paste into a conversation with Claude. Not
part of the agent loop — a standalone helper for the reverse direction: a human
prepares data for Claude, not Claude acting in Revit.

> **gotcha — hebrew encoding (inbound half fixed at the route layer, 2026-08-24)**
> Non-ASCII text sent as a parameter to any `mcp__revit__` tool used to be
> corrupted before it reached IronPython: pyRevit's own request-body parsing
> decodes the UTF-8 bytes as Latin-1, one wrong "character" per original byte,
> and a comparison then failed silently — 0 results, no error.
>
> This repo's fork fixes it where every route handler reads its request body:
> `repair_hebrew_in()` (`revit_mcp/utils.py`) recursively repairs every string
> in the parsed JSON payload before any field is read, and is called first
> thing in all 20 handlers — the friendly tools (`create_room`, `place_family`,
> `type_name`, `level_name`, ...) exactly as much as `execute_revit_code`. An
> earlier attempt patched pyRevit core's own request handler instead; that does
> not work, because pyRevit runs a route handler in a different engine scope
> than the module that registers it (see the comment at the top of
> `mcp-server/startup.py`) - the fix has to sit inside each handler, which is
> why it is 20 near-identical call sites rather than one shared patch.
>
> Text read **out** of Revit has a companion, older fix: `_safe_str` /
> `normalize_string` / `sanitize_string` no longer strip non-ASCII to `?`. See
> `mcp-server/PATCH-NOTES.md` for both.

---

## Part 2 — the agent: what was built on top

The MCP tools are stateless: every call forgets the one before it. This layer was
built to answer three questions the raw connection cannot — what happened here
since last time, who did it, and how do you know an operation that reported
"success" actually happened.

### Three levels of verification, one designed system

Milestone 1 (2026-08-31) added the two levels this project's own memory had
been compensating for since the tracker existed, without ever fixing at the
source. All three now exist deliberately, and none subsumes another — see
`docs/operation-contracts.md` for the full per-operation matrix:

| Level | Question | Where | Catches |
|---|---|---|---|
| **1 · Transaction** | Did Revit commit? | `commit_verified()`, every `revit_mcp/*.py` handler | `Commit()` returning anything but `Committed` — the exact "`MoveElement` returned success and moved nothing" mechanism, now caught at the route layer instead of only inferred later |
| **2 · Post-condition** | Did *this operation* achieve its own contract? | per-handler, `docs/operation-contracts.md` | Revit committing while silently declining, clamping, or relocating — measured live: a hosted door's host-wall curve change that Committed with zero failure messages and no actual change |
| **3 · Intent** | Did the *batch* achieve what the user asked? | the tracker's checkpoint diff + the queue item's `user_intent` field | every call passing levels 1 and 2 while the aggregate is still wrong — the reference case is 268 furniture placements where every call succeeded and 104 landed inside walls |

Level 3 is what the rest of this document describes. It is not being replaced —
it is the one level that was already built correctly, and it stays the authority
for "did the actual work succeed," because a route handler can only ever certify
its own operation, never the user's full request.

### Impact analysis: a fourth, different kind of question

The three levels above all answer *"did the thing I just did work?"* — after the
fact, about a mutation that already happened. `revit_mcp/impact.py` (added after
the Milestone 0–3 hard gate) answers a question none of the three levels ask at
all: *"what would happen if I did this?"* — before anything is committed, often
before anything is even attempted.

Two tools, deliberately not one, because they trust different sources of truth:

- **`analyze_relationships`** — a static read: Revit's own dependency graph
  (`GetDependentElements`), geometry joins, host/hosted-by, room-boundary
  membership. Fast, but informational — it does not claim to predict a cascade.
- **`preview_delete_impact`** — the actual `doc.Delete()` runs for real inside a
  transaction that is *always* rolled back, never committed. This is Revit's own
  cascade logic producing the real answer, then discarded — the same
  guaranteed-rollback discipline the tracker's own snapshot ops use so a read
  never touches Undo, applied here to a mutation instead of a read.

Neither fits the `verified.{ok,method,expected,actual}` schema level 2 uses,
because neither commits anything for that schema to describe — see
`docs/operation-contracts.md` for their full contract and the live verification
each was proven against.

### Three working assumptions the system is built around

- **Absence of an error is not proof the operation happened.** Measured:
  `MoveElement` returned success without moving anything — see Level 1/2 above
  for where this is now caught at the source, and the checkpoint diff for the
  independent, batch-level confirmation that always applies regardless.
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
| `revit-scribe` only | `journal.ndjson` · `state.json` · `design_state.json` · `log/` · `.scribe.lock` |
| The main thread | `writeups/pending/` (creation) · `README.md` · `RULES.md` (only after user approval) |

### Five kinds of memory, five physical homes

Not all memory in this system is the same kind of thing, and conflating them
was never forced by the file layout — each already sits somewhere distinct:

| Memory | What it holds | Physical home |
|---|---|---|
| **Model** | elements, geometry, parameters, relationships — what exists *right now* | never persisted as its own store; read live via `mcp__revit__` (`get_element_properties`, `analyze_relationships`, ...) or reconstructed from `snapshot.tsv` |
| **Project** | history — previous changes, decisions, failures, alternatives | `journal.ndjson` (append-only events) · `state.json` (current-state rollup) |
| **Intent** | what the user is trying to accomplish — goals, priorities | `design_state.json`, `kind: "goal"`/`"preference"` records (Milestone 5) |
| **Constraint** | rules and limits — code, client requirements, site/structural/MEP limits | project-scoped: `design_state.json`, `kind: "constraint"` records. Cross-project/general: `revit-lessons/RULES.md`'s `check` field |
| **Learned design** | reusable knowledge from a user correction or approval | `revit-lessons/RULES.md` — see below |

`design_state.json` is new (Milestone 5 of the M1-M5 architecture upgrade,
`memory-system/claude/skills/revit-session/references/project-log-format.md`
has the full schema) — it exists specifically because Project memory
(`journal.ndjson`) cannot represent Intent or Constraint memory: a queue item
requires a non-empty diff (`checkpoint-queue.md`'s own rule), so a goal
stated or a constraint agreed with no model change was, until this
milestone, structurally unrecordable.

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

<details>
<summary>A private rules file's Hebrew came back as one Unicode codepoint per UTF-8 byte</summary>

Building `revit_mcp/validation.py`'s rules loader (2026-08-31): reading a UTF-8
JSON file as raw bytes and handing it to IronPython 2.7's own `json.loads`
does **not** UTF-8-auto-detect the way CPython's does — every Hebrew keyword came
back as `u"\xd7\x9e\xd7\x98..."` (each UTF-8 byte reinterpreted as its own
Latin-1 codepoint) instead of `u"מט..."`, and every keyword match
silently failed — 0 rooms matched any rule, with no error at all. This is the
exact mojibake shape `repair_hebrew_text()` already exists to reverse for
request bodies (see the hebrew-encoding gotcha above), just hit from a new
direction: a file read, not a request parse. The fix: decode the bytes as
UTF-8 explicitly (`raw_bytes.decode("utf-8")`) before calling `json.loads`,
so the mojibake never happens, rather than parsing corrupted text and
repairing it after. Caught only because the room-type checks were verified
live against real room names instead of trusted on the strength of "it
imported without error."

</details>

<details>
<summary>Formatting an int with a float precision spec crashes under IronPython, not CPython</summary>

`"{:.0f}mm".format(n)` raised `ValueError: Precision not allowed in integer
format specifier` under IronPython 2.7, where `n` came from a JSON rules-file
value written without a decimal point (`json.loads` returns `int` for a number
literal with no decimal point, `float` for one with a decimal point — a rules
file is free-form external data, so either can show up). The identical line
runs fine under CPython 3,
where this offline test suite runs — so this could not have been caught
offline, only live. The fix: don't apply a float format spec to a value that
might be a JSON int; plain `{}` formatting displays an already-whole number
just as well. A standing reminder that this project's offline tests prove
CPython-side logic, and the IronPython engine underneath can still disagree
in ways only a live call surfaces.

</details>

---

**Sources:** `mcp-server/README.md` · `memory-system/tracker/tracker.py` ·
`memory-system/claude/skills/revit-session/SKILL.md` ·
`memory-system/claude/agents/revit-historian.md` ·
`memory-system/claude/agents/revit-scribe.md` ·
`memory-system/claude/revit-lessons/RULES.md` · memory records from 2026-08-16–23.
