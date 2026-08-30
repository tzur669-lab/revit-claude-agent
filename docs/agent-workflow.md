# The agent: build commands, initial state, documentation and lessons

This document focuses on one part of the system: the agent that takes a command
from the user to build something in Revit (levels, walls, rooms, furniture,
views...) — and what happens **around** the command itself, before it and above
it. Three principles drive this whole layer:

1. **Before touching anything — capture the initial state.** Without it there is
   no way to know afterwards what changed, and who changed it.
2. **Every action is documented**, not just as free text but as a record with
   facts that come from the model itself, not from the conversation's memory.
3. **A mistake the user corrected is written once, and does not recur.** Not
   every mistake — only one that passed an explicit gate.

---

## Step 0: before every command — capturing the initial state

Before I touch the model, I run `session_start` — an operation that identifies
the open document by **canonical file path** (not by an internal id; three files
can share the same id if they were created from the same template), and takes a
full snapshot: a hash of geometry and parameters for every element in the model,
saved to `snapshot.tsv`.

That snapshot is the baseline. It is not just for "now" — it is what turns the
sentence "someone changed these three walls since yesterday" into something that
can be known for certain, not guessed.

**In parallel**, a separate background agent called `revit-historian`
(read-only, no Revit access at all) reads the project history from disk — what
was done last time, what is still open, which traps are already documented for
this project — and returns a short briefing (up to 15 lines) that runs
**concurrently** with the snapshot capture, so no time is wasted.

The result is cross-checked: if the tracker's identification (the only authority,
because it has direct Revit access) does not match what the historian guessed —
the briefing is discarded, and the correct state file is read directly.

**If the snapshot reveals the model changed since last time** — whether by me, or
by someone else who worked on the file outside Claude — this is not presented as
a fault. It is exactly the information the system exists for, and the change is
written to the journal like any other action (see the documentation step below).

---

## Executing the command itself

Once there is a baseline, execution can happen. A build command goes out through
one of two routes:

- **Friendly tools** (`create_level`, `place_family`, `create_room`, etc.) —
  convenient, but non-ASCII text in their parameters (a level name, a room name)
  is silently corrupted before it reaches Revit. So where a name exists, identify
  elements by numeric `ElementId`, not by a non-ASCII name.
- **`execute_revit_code`** — run arbitrary Python inside Revit. Here you can write
  non-ASCII text naturally and wrap it in an encoding-fix function
  (`s.encode("latin-1").decode("utf-8")`), and also create elements directly
  through the API when you need control the friendly tools do not give.

### The rule that runs through this whole layer: **absence of an error is not proof the operation happened**

This is not a theoretical guideline — it was measured in practice. `MoveElement`
returned "success" without moving anything. Placing 268 furniture items reported
"268 placed, 0 errors" when nearly half of them landed inside walls, at double
height, or stuck at z=0 — three different causes, all with no API error.

**The practical meaning: no build operation counts as "done" just because the
tool reported success.** The real verification happens in the next step.

---

## Documentation: every action is verified and written, not just reported

After every meaningful batch of changes, run `checkpoint`. This is the operation
that closes the loop in two ways at once:

### 1. checkpoint is the verification itself

The checkpoint takes a fresh snapshot and compares against the baseline. That
**diff** — not the tool's return code — is what decides whether the operation
actually happened. If the diff does not show what I expected, I do not tell the
user the operation succeeded, even if the tool itself threw no error.

### 2. every change is attributed to someone, by evidence only

A `DocumentChanged` listener inside Revit catches every change and assigns it to
one of four states, by the transaction name Revit itself recorded — not by a guess:

| Attribution | The evidence | What it means |
|---|---|---|
| `claude` | A transaction created through one of five exact MCP action names | I did this |
| `human` | A transaction with a name Revit gave it (e.g. `Wall - Line`) | Someone did this manually in the UI |
| `sync_incoming` | Arrived in a sync-with-central window | Someone else's work in a shared model |
| `unknown` | No covering event | Not known — **and not equal to "human"** |

`unknown` is an honest value, not a failure: it also covers Revit's internal
regeneration, and any change that happened while the listener was not armed (Revit
closed and reopened, for example). Upgrading it to "human" by guessing is exactly
the mistake the system must not make — because it attributes human work to me, or
the reverse.

### 3. from snapshot to journal record — three players, each writes only what it knows

| Step | Who writes | What |
|---|---|---|
| checkpoint | the tracker (inside Revit) | a **delta** file — raw facts: which elements changed, attribution for each, transaction names |
| queue item | the main thread (me) | a small file with `user_intent` — the **intent** only I know, because it is written nowhere in the model |
| journal record | a separate background agent, `revit-scribe` | combines the two into one record in `journal.ndjson` |

The decisive rule here: **the delta file is the source of truth for every fact
about the change.** The queue item does not copy a diff or an attribution of its
own — it contributes only intent. This was tested in practice: once a queue item
was written with a wrong count (`claude: 2, unknown: 0`) while the real delta
said `claude: 1, unknown: 1` — a manual-copy error, caught **only** because the
scribe always cross-checks against the delta and does not trust the item itself.

A complete journal record looks like this (abbreviated):

```json
{"n":10,"ts":"2026-08-23T14:02","by":"claude",
 "intent":"user asked for 4 furnished residential floors",
 "did":"created levels 19800/23100/26400 + roof 29700; 33 walls & 24 rooms per floor",
 "why":"identical mirrored-apartment plan around central core",
 "diff":{"a":725,"m":1,"d":0},
 "attr":{"claude":726},
 "delta":"deltas/d-20260818104502.json"}
```

`intent`+`why` are the difference between a journal and a catalog — they are what
turns "725 elements added" into "why they were added". A required field: a record
without `intent` is rejected and not written at all.

**This documentation happens even when I did not create the change.** If
`session_start` reveals a change made outside Claude, it goes into the journal
exactly like a checkpoint — because that is precisely why the system exists.

### 4. state.json — not history, current state only

Alongside the journal (which is append-only and only grows) there is one state
file, rewritten every time: how many elements exist, what the current open task
is, which threads are not yet closed. The historian agent relies on it to answer
"what is in this project" without opening the whole journal.

---

## Lessons: a corrected mistake is written once, and not forgotten

This is the layer that answers "do not repeat mistakes" — and it is deliberately
separate from the ordinary journal.

### The gate — the one rule that must not break

A line is written to the global `RULES.md` (spanning **all** Revit projects, not
just the current one) **only** in one of two cases:

1. **Correction** — the user says something I did was wrong or not what they asked.
2. **Approval** — I propose a lesson, and the user approves it.

Nothing else passes the gate. A bug I caught myself in verification is written to
`journal.ndjson` (a `lesson` field), not to `RULES.md`. A technical tool trap
with no user involvement is written to the skill's internal documentation. That
separation is what keeps the global file from filling with every small insight —
it is loaded automatically **every session, every project**, so every line there
is a fixed reading cost.

Every line carries a mandatory source: `src: user-corrected <date>` or
`src: user-approved <date>`. A line without a valid source is deleted on sight,
because it is a sign it was written without authorization.

### The format — a rule you can act on, not advice

```
id :: trigger :: rule :: check :: src :: ev
```

```
L001 :: staircase-layout :: Israeli residential code caps an interior riser at 175mm and floors the going at 260mm; derive the shaft footprint from the resulting run length BEFORE laying out the rooms around it :: risers = ceil(floor_to_floor/175); pick going so 2R+G lands in 610-650; assert shaft_length >= longest_flight_run + landing_depth :: user-corrected 2026-08-30 :: -
```

`check` is an operational test, not general advice. A rule with no `check` is
usually a sign it is phrased too softly to act on.

### The trigger — the part that decides whether the lesson is even recalled in time

This is the most important part of the whole format. A rule pulled **after** I
have already built the geometry is documentation, not prevention.

**The rule: the trigger is phrased as an activity that is starting, not as a
symptom that was discovered.**

| Phrasing | What actually happens |
|---|---|
| `wrapped-stair-fix` | Pulled only after I already built wrapped stairs by mistake. Useless. |
| `staircase-layout` | Pulled when I **start** planning stairs, before geometry exists. Correct. |

Every `trigger` must come from a closed list of 17 domains written at the top of
the file (level creation, wall layout, family placement, MEP routing, and more) —
so that one-off phrasings that would never match any future task are not created.

**The mechanism that turns this from a label into an actual mechanism:** before
starting a batch of work on the model, I name the domain of the batch and apply
every rule carrying it — **before** touching geometry. This is not an extra file
read (the content is already injected into context at the start of the session) —
it is a one-line check before starting.

### When this is written

Not through the scribe's background queue — **immediately, at that moment in the
conversation** where the user corrects or approves. A queued item vanishes if the
session closes; the moment of correction is mid-conversation with the user and
cannot be deferred.

The global file is exposed to a collision between two Claude conversations on two
different projects writing at the same moment, so it has a dedicated lock of its
own (`~/.claude/revit-lessons/.lessons.lock`) — separate from both the tracker's
lock and the scribe's lock, with the same convention: numeric epoch, not a time
string (a string ending in `Z` was measured being read once as local time, which
made a lock look valid forever).

Before any addition — scan for an identical `trigger` that already exists. If one
exists — **sharpen it in place, do not add a nearby line.** Two lines on the same
trigger are two sources for one fact, and a second source exists only to contradict.

### Ceiling — this file stays a living rule list, not an archive

At most 30 rules or about 4KB, whichever is lower. Past the ceiling you do not
just delete — a rule fully absorbed into internal documentation drops from the
list and leaves a pointer, and two rules with overlapping triggers merge.

---

## Who does what — all the players together

| Player | Where it runs | Revit access | Role |
|---|---|---|---|
| The main thread (me) | in the conversation | yes | executes commands, runs session_start/checkpoint, writes lessons in real time |
| `tracker.py` | inside Revit, IronPython | yes | captures the snapshot, computes the diff, attributes changes |
| `revit-historian` | background, parallel to session_start | no | reconstructs a history briefing from disk only |
| `revit-scribe` | background, after checkpoint | no | the sole writer of `journal.ndjson`/`state.json`, drains the queue |
| `RULES.md` (lessons) | a global file, not an agent | — | written directly by the main thread, at the moment of correction |

**Why not more background agents:** `mcp__revit__` is a single channel to a
single Revit process, and the tracker locks the instance directory during work.
Any additional agent that tried to touch Revit would collide on the lock instead
of saving time. The split is by **resource** (disk and prose leave the critical
path), not by task.

---

## Full life journey: from one command to a saved lesson

To see all the layers together, this is what a typical build command looks like end to end:

1. **The user asks** — e.g. "add another floor with 6 rooms".
2. **session_start** captures a snapshot, identifies the file by canonical path.
   `revit-historian` runs in the background in parallel and returns a briefing:
   what has been done in this project so far, what is still open.
3. **Execution** — the floor, walls, and rooms are built through
   `execute_revit_code` or the friendly tools, as needed.
4. **checkpoint** — a fresh snapshot, a diff against the baseline, and attribution
   for every changed element. The report to the user relies on the diff, not on a
   "success" the tool returned.
5. **A queue item is written** with the user's intent ("asked for another floor
   with 6 rooms"), and `revit-scribe` is launched in the background (or keeps
   running if already active).
6. **The scribe drains the queue**, reads the delta as the source of truth, and
   writes one journal record combining facts + intent. Updates `state.json` at the end.
7. If the user replies **"wait, those rooms should be at a different ceiling
   height"** — that is a correction. I write immediately, not through the queue, a
   line to `RULES.md` with the appropriate domain (e.g. `room-definition`), phrased
   to be pulled **before** starting next time, with a `user-corrected` source + date.
8. **Next time** I start a batch in the `room-definition` domain, that rule is
   already in context — and I apply it before touching geometry, not after.

That is the full loop: a known initial state → a verified action → documentation
with real facts → a corrected mistake that stays corrected in every future project.

---

**Sources:** `memory-system/claude/skills/revit-session/SKILL.md`,
`memory-system/claude/skills/revit-session/references/{lessons,project-log-format,checkpoint-queue,attribution}.md`,
`memory-system/tracker/tracker.py`,
`memory-system/claude/agents/{revit-historian,revit-scribe}.md`,
`memory-system/claude/revit-lessons/RULES.md`. Some details were last verified
2026-08-16–23 — before relying on a specific line or filename, verify against the
live code.
