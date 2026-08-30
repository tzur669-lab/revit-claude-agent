# The journal write queue

Loaded before the first queue item of the session. A read-only session that
produced no diff does not need it.

## When an item is created

`status` is `complete` **and** the diff is not all zeros ⇒ write a queue item,
and keep working without waiting.

**The rule applies to both authors.** `session_start` exactly like `checkpoint`:
a change detected at session open is work done outside Claude, and that is
precisely what the system exists to catch. Do not omit it from the journal just
because we did not make it.

**Do not create an item** when `status` is `partial` (no diff at all — see
`troubleshooting.md`), or when the diff is all zeros.

## The item

`<instance_dir>\writeups\pending\w-<YYYYMMDDHHMMSS>.json`

Write to `.tmp` then rename, so a partial item is never read as valid.

```json
{
  "at": "2026-08-16T13:18:28",
  "run": "r-20260816-131826-698d",
  "delta_file": "<instance_dir>\\deltas\\d-20260816131828.json",
  "counts": {"elements": 142},
  "notes": [],
  "saves": 5,
  "user_intent": "user asked for plan views on the three new levels; created via execute_revit_code following the existing level's template."
}
```

`delta_file` is `result.delta_file` verbatim. `counts`, `notes` and `saves` are
copied from `last_run.json` — they do **not** exist in the delta file, so they
must pass through here.

**Do not copy `diff` or `attribution` here.** Both are already inside the delta,
at the individual-element level and not just as a summary. Copying them by hand
adds a second source for one fact — and a second source exists to contradict.
Measured 2026-08-16: a queue item was written with `"claude": 2, "unknown": 0`
while the delta and `last_run.json` said `claude: 1, unknown: 1`. The error was
in the copy, and the scribe caught it only because it cross-checks against the delta.

**The delta file is the source of truth for every fact about the change.** The
item adds only what the delta does not know: the intent, and the three scalars above.

**No need to copy `last_run.json` itself** — that way there is no risk it gets
overwritten before the scribe reads it.

## Several deltas in one stretch

When several checkpoints are one logical unit of work, you may add
`extra_delta_files` — an array of additional paths — and ask for a single
`user_intent` section. The scribe reads them all. Better than three sections
describing one check.

## `user_intent` — a required field

The field carries what the main thread knows and `delta_file` does not. An item
without it is rejected by the scribe and not written, rightly so — without it the
journal drops to catalog level.

- **After a `checkpoint` on our work:** what the user asked for and how it was
  done. The delta knows "3 Views added"; only the main thread knows **why**.
- **After a `session_start` with a diff:** the context surrounding the detected
  change — was Revit restarted (`listener.gen` low, `events_seen: 0`), have the
  same elements changed in prior sessions, and what that says about the
  reliability of attribution. There is no "user intent" here, and none should be
  invented — the intent is to document why the evidence looks the way it does.

In both cases: **do not upgrade `unknown` to "human"** through this field.

## Launching the scribe

- No `revit-scribe` in flight ⇒ launch one **in the background**.
- One in flight ⇒ **do nothing**. The item is already in the queue, and the
  current run checks the queue again before it exits.

The main thread is the only launcher, so within one session serialization is
guaranteed. `.scribe.lock` protects the case of two Claude conversations working
the same project in parallel — the same scenario the tracker already guards for snapshots.

If the scribe crashed or the session closed mid-way, the items stay in
`pending\` and the next scribe drains them.
