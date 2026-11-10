# Journal format — `journal.ndjson` and `state.json`

Loaded mostly by `revit-scribe`. The main thread loads it only when creating a
new journal for `NEW_INSTANCE`.

Both files are written **in English** and are for machine reading only. Non-ASCII
names that come from the model (levels, rooms, types) are kept as data values
verbatim — they are data, not prose.

The previous format was prose in `PROJECT.md`/`STATE.md`. Those files were frozen
under `<instance_dir>\legacy\`; they are readable and not edited. Do not write to
them and do not rely on them as current state.

---

## `journal.ndjson`

Append-only. One JSON line per event, UTF-8, no blank lines.

```json
{"n":10,"ts":"2026-08-23T14:02","by":"claude","run":"r-20260823-140201-4c1a","intent":"user asked for 4 furnished residential floors","did":"created levels 19800/23100/26400 + roof 29700; 33 walls & 24 rooms per floor","why":"identical mirrored-apartment plan around central core","ids":{"levels":[1385553,1385564],"floors":[1385599]},"counts":{"el":867,"saves":7},"diff":{"a":725,"m":1,"d":0},"attr":{"claude":726},"tx":["Create 4 levels, plan views"],"delta":"deltas/d-20260818104502.json","lesson":"family insertion point sits at the back, not center","notes":[],"expected":"4 levels + roof, furnished per floor","verified":[{"tool":"create_level","tx_status":"Committed","ok":true,"method":"element_category"}],"outcome":"match"}
```

| Field | Required | Source | Note |
|---|---|---|---|
| `n` | yes | see calc below | running, no reset and no skip |
| `ts` | yes | the queue item's `at` | `YYYY-MM-DDTHH:MM` |
| `by` | yes | the delta's `attribution` | `claude` \| `human` \| `sync` \| `unknown` |
| `run` | yes | the queue item's `run` | |
| `intent` | **yes** | `user_intent` from the queue item | missing ⇒ the item is rejected and not written |
| `did` | yes | the scribe, from the delta | what actually changed |
| `why` | yes | the scribe | the reason. This is what separates a journal from a catalog |
| `ids` | no | the delta | representative ids only. Large delta ⇒ rely on `delta` |
| `counts` | yes | the queue item | `el`, `saves` |
| `diff` | yes | the delta | `a`/`m`/`d` — added/modified/deleted |
| `attr` | yes | the delta **only** | count by `by`. Do not copy from the queue item |
| `tx` | yes | the delta | transaction names. Empty ⇒ `[]` |
| `delta` | yes | `result.delta_file` | path **relative** to `instance_dir` |
| `lesson` | no | the scribe | an insight we caught ourselves |
| `notes` | yes | the queue item | empty ⇒ `[]` |
| `expected` | no | the queue item's `expected`, verbatim | a **claim** by the main thread, not a fact — see below |
| `verified` | no | the queue item's `verified`, verbatim | level-1/level-2 evidence from tool responses — a **claim**, not a fact |
| `outcome` | no | computed by the scribe | `match` \| `mismatch` \| `unverified` — see below |

### `expected` / `verified` / `outcome` — claims, not facts

These three fields are **new**, added alongside the three-level verification
layer (see `docs/architecture.md`). Before this, all three levels computed
evidence on every call and none of it was ever kept — the journal knew *what*
changed and never knew whether that was what was actually intended.

**The distinction is critical and must not be blurred:**

- `expected`/`verified` are **claims made by the main thread** — what it
  predicted before executing, and what the tools themselves reported while
  executing. The scribe **copies them verbatim**, **never invents or
  corrects them**, exactly like `user_intent`.
- `did`/`diff`/`attr` remain **observed fact**, derived only from the delta —
  the existing rule ("the delta file is the source of truth for every fact")
  is unchanged.
- `outcome` is the one field of the three the scribe actually **computes**
  rather than copies: it compares `expected` (the claim) against the delta
  (the fact) and records `match` when they agree, `mismatch` when they don't,
  or `unverified` when no `expected` was supplied. **Never reconcile a
  contradiction** — a mismatch is recorded as exactly that, not silently
  smoothed over.

A queue item with no `expected` field ⇒ both `expected` and `verified` are
absent from the record, and `outcome` is `unverified`. That is normal, not an
error — not every checkpoint carries a prediction.

### `by` and `attr` — the rule that does not change

Attribution is decided from the evidence in the delta only
(`references/attribution.md`). **`unknown` is not "human"** and must not be
upgraded. Event-level `by` is the clear majority in `attr`; when there is no
clear majority — `unknown`.

### `lesson` — does not reach `revit-lessons`

This field carries an insight we caught ourselves in verification. It stays in
the journal. Only what passed the user's gate goes to `RULES.md` — see
`references/lessons.md`.

### Computing `n` — from two sources, not from an in-memory count

```
n = max(state.last_n, n of the last valid line in journal) + 1
```

Not an internal count in the scribe: a rejected queue item, or a run that died
mid-way, creates a gap that an in-memory count does not see. The two sources are
taken **together** because they fail in opposite directions — `state.json` lags
when the scribe dies after appending to the journal and before writing state, and
the journal lags when the last line was truncated.

### A line that does not parse

| Where | Meaning | What to do |
|---|---|---|
| **at the end of the file** | a truncated write | skip it, compute `n` from the last valid line, note it in the summary |
| **in the middle of the file** | corruption | **stop.** do not write, do not fix, report |

### Appending — heredoc, then verify

```bash
cat >> "$IDIR/journal.ndjson" <<'JEOF'
{"n":10,"ts":"...","by":"claude",...}
JEOF
python -c "import json,io,sys;ls=[l for l in io.open(sys.argv[1],encoding='utf-8').read().splitlines() if l.strip()];json.loads(ls[-1]);print('LINE_OK')" "$IDIR/journal.ndjson"
```

A quoted-delimiter heredoc (`<<'JEOF'`) expands nothing — that is what keeps a
`$` or a backslash inside a non-ASCII name from breaking the line. **`LINE_OK`
did not return ⇒ the line is damaged at the end of the file: remove it and
rewrite**, before moving to the next item.

---

## `state.json`

Current-state snapshot. Rewritten on every queue drain, **once at the end** and
not after every item.

```json
{"v":1,"updated":"2026-08-23T14:02","snap":"r-20260823-140201-4c1a","last_n":10,
 "model":{"el":867,"saves":7,
          "levels":[{"name":"קומה ה","mm":16500,"furnished":true}],
          "views":{"plans":10,"elevations":4,"3d":1,"sections":0},
          "per_floor":{"walls":33,"rooms":24,"doors":27,"windows":20,"furniture":69},
          "footprint_mm":[14204,27300],
          "last_geom_verify":{"at":"2026-08-18","wall_overlaps":0,"rooms_closed":96}},
 "open_task":null,
 "threads":[{"id":"hebrew-io","status":"partial","op":"fix() inside execute_revit_code; friendly-tool text params still need ElementId","ev":"skills/revit-session/references/hebrew-io.md"}],
 "read_warnings":[{"applies_to":"journal n<=4","text":"human attributions before 2026-08-16 21:16 are wrong; that was claude"}],
 "cfg":{"snapshot_format":3}}
```

### The four parts — state, not history

| Field | Why it is state | What bounds it |
|---|---|---|
| `model` | what exists right now, without opening the journal | the size of the model itself |
| `open_task` | the open task | one at any moment, or `null` |
| `threads` | what still needs action | a thread that closes drops off here |
| `read_warnings` | warnings that apply to reading the old journal | few by nature |

`model` is **not a compression target.** It is the one part that is state by
definition, and everything else exists to serve it — `revit-historian` relies on
it to answer "what is in this project" without opening the journal. The build
detail also appears in journal records, but there it is an **event** and here it
is **state**.

A thread whose evidence has all been moved to `references/` — keep the operative
part in `op` and point `ev` at the rest. The trigger is an event (evidence
moved), not size.

### Two things that do not belong here

**No "closed" list.** A thread that closed is documented in its own journal
record, which is append-only. A list of pointers to it is a duplicate table of
contents, and it grows by a line on every close, forever.

**Single exception:** something that changes how the **old** journal is read —
e.g. attributions that are wrong retroactively — is not a closed item but a
`read_warnings` entry, and it stays there as long as the old journal exists.

**No "learned working rules".** A rule learned about Revit or about the tracker
applies to **every** project. Its place is `~/.claude/revit-lessons/RULES.md` (if
it passed the user's gate) or the skill's `references/` (if not). A rule sitting
in one project's `state.json` both duplicates and fails to propagate — measured:
of seven rules in one project, four were entirely absent from the other, and two
sat in two places at once.

| The rule concerns… | Its place |
|---|---|
| a correction the user made or approved | `revit-lessons/RULES.md` |
| attribution, reading a delta | `references/attribution.md` |
| non-ASCII text in and back out | `references/hebrew-io.md` |
| `partial`, locks, `tracker.py` development | `references/troubleshooting.md` |
| the writeups queue | `references/checkpoint-queue.md` |

### Atomic write — required, not an improvement

A truncated markdown file stays readable. A truncated `state.json` is an
unclosed brace that breaks the historian's read at the next session open — a
silent failure that shows up only when the file is already needed.

```bash
python -c "import json,io,sys;json.load(io.open(sys.argv[1],encoding='utf-8'));print('STATE_OK')" "$IDIR/state.json.tmp" \
  && { [ -f "$IDIR/state.json" ] && cp -f "$IDIR/state.json" "$IDIR/state.json.bak"; mv -f "$IDIR/state.json.tmp" "$IDIR/state.json"; echo SWAPPED; }
```

The order is fixed: write to `state.json.tmp` (the `Write` tool) ⇒ verify it
parses ⇒ roll the existing file to `state.json.bak` ⇒ `mv`. **`STATE_OK` did not
return ⇒ do not `mv`.** A valid old file beats a broken new one. This is the same
pattern `tracker.py` runs on `_index.json` and `snapshot.hdr.json`.

The reading side: `state.json` does not parse ⇒ fall back to `state.json.bak`
**and declare it**. That does not parse either ⇒ "no saved state". Do not guess state.

---

## `design_state.json`

Current design reasoning for this project — goals, constraints, assumptions,
decisions, open questions, preferences. **Not** a replacement for
`journal.ndjson`/`state.json`/deltas/snapshots — those stay the record of
what changed and what exists; this is the record of what the project is
*trying to achieve* and *why*, which can exist with no diff at all (rejecting
an option, agreeing a strategy) — exactly the case `checkpoint-queue.md`'s
own rule excludes from ever reaching the journal.

**Exactly one writer: the scribe.** Same rule, same reason, as
`journal.ndjson`/`state.json` — a single writer per file is what prevents
collisions between the tracker (Revit side) and the agents (disk side). No
Revit route, no MCP tool, no `revit-historian`, may write this file. The main
thread proposes a record (a claim); the scribe is the one that actually
writes it, under `.scribe.lock`, using the **same** atomic procedure as
`state.json` — write `.tmp`, verify it parses, roll the existing file to
`.bak`, `mv`. This is a direct reuse of that existing mechanism, not a new
one.

```json
{"v":1,"updated":"2026-08-31T18:00","records":[
  {"id":"D001","kind":"constraint","scope":"project",
   "statement":"preserve the central core; do not touch stair/elevator/shaft footprint",
   "source":"user","status":"active","check":null,"evidence":null,
   "tags":["core","circulation"],"created":"2026-08-31T18:00","supersedes":null}
]}
```

| Field | Values | Note |
|---|---|---|
| `id` | `D001`, `D002`, ... | monotonic, derived the same way the journal derives `n` — from the max id already present, never from a count. Never reused, even after a record is superseded or its status changes. |
| `kind` | `goal` \| `constraint` \| `assumption` \| `decision` \| `question` \| `preference` | what TYPE of thing this record is |
| `scope` | `project` (only value in use today) | reserved for a future non-project scope; do not invent one without a real need |
| `statement` | free text | the claim itself, one or a few sentences |
| `source` | `user` \| `measured` \| `inferred` | where this claim came from |
| `status` | `active` \| `resolved` \| `rejected` \| `superseded` | current standing |
| `check` | free text or `null` | an operational test, when one exists — same spirit as a `RULES.md` line's `check` field |
| `evidence` | free text/pointer or `null` | optional support for the claim |
| `tags` | list of free-text strings | for lookup; not a closed vocabulary like `RULES.md`'s trigger domains |
| `created` | ISO timestamp | when the record was written |
| `supersedes` | an earlier record's `id`, or `null` | see below |

### `kind` × `source` is the fact/rule/intent/judgement separation

No second field for this — the two already on the record say it:

| `source: measured` | `source: user` | `kind: goal`/`preference` | `source: inferred` |
|---|---|---|---|
| **fact** | **rule** (when `kind: constraint`) | **intent** | **judgement** |

A record the system itself derived from the model (`source: measured`) is a
fact. A constraint the user stated (`kind: constraint, source: user`) is a
rule. A goal or preference is intent, regardless of source. Anything the
system concluded on its own (`source: inferred`) is judgement, and must be
distinguishable from a fact a human can check directly. Do not invent a
`confidence` field or any other axis for this distinction — these two fields
already carry it.

### Superseding a record — never delete

A record that no longer holds is **not** deleted. Its own `status` becomes
`superseded`, it stays in `records` exactly as written, and the **new**
record points back at it via `supersedes`. Historical design reasoning stays
available, the same way the journal never deletes a past entry.

### Ownership, precisely

```
main thread proposes a design-state claim
      ↓  scribe receives it (queued, like a journal writeup, or as a
      ↓  direct message — see "Updating state.json without a delta"
      ↓  in agents/revit-scribe.md, the same mechanism applies here)
      ↓  scribe takes .scribe.lock
      ↓  scribe writes design_state.json.tmp → verifies it parses →
      ↓  rolls the existing file to .bak → mv
      ↓  scribe releases the lock
```

`expected`/`verified`/`outcome` in the journal (see above) are a related but
separate idea: those are per-*change* claims about one batch of work.
`design_state.json` is the project's standing claims, independent of any
single change.

---

## Rotation

`journal.ndjson` past `~150KB` or `~200` records ⇒ move records older than `30`
days to `log/YYYY-MM.ndjson` (create, or append), and keep the records from the
last `30` days. Rotation preserves `n` continuity — the numbers do not restart,
and the old records stay whole in the monthly file.

`events.ndjson` is **never rotated.** It is the Revit side's machine record and
stays whole.

---

## New instance (`NEW_INSTANCE`)

The main thread creates two files. From that moment both belong to the scribe.

`journal.ndjson` — one record:

```json
{"n":1,"ts":"<at>","by":"system","run":"<run>","intent":"instance registered","did":"tracking started","why":"first snapshot of this document","ids":{},"counts":{"el":0,"saves":0},"diff":{"a":0,"m":0,"d":0},"attr":{},"tx":[],"delta":null,"notes":[]}
```

`state.json` — skeleton:

```json
{"v":1,"updated":"<at>","snap":"<run>","last_n":1,
 "meta":{"title":"<title>","path":"<path>","iid":"<first 8 chars of instance_id>","lineage":"<lineage>","since":"<YYYY-MM-DD>","user":"<username>"},
 "model":{},"open_task":null,"threads":[],"read_warnings":[],"cfg":{"snapshot_format":3}}
```

`notes` included `SHARES_LINEAGE_WITH_n` ⇒ add to `read_warnings`:

```json
{"applies_to":"identity","text":"lineage shared with N other files - same Revit template, NOT the same project. never merge."}
```
