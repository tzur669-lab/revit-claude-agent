# tracker

The snapshot / diff / attribution engine. This is the "maps what already exists"
half of the system.

## Files

| File | Runs where | Role |
|---|---|---|
| `tracker.py` | **Inside Revit**, IronPython 2.7, via pyRevit Routes | Identifies the document by canonical path, hashes every element's geometry + parameters, diffs against the stored baseline, and attributes each change to `claude` / `human` / `sync_incoming` / `unknown`. Writes `snapshot.tsv`, `deltas/`, `events.ndjson`, `_index.json`, `last_run.json`. |
| `hook_session_reminder.py` | **On the machine**, CPython, as a Claude Code `PreToolUse` hook | Fires once per session before the first `mcp__revit__` call. Injects a reminder to run the `revit-session` protocol, plus the current contents of `~/.claude/revit-lessons/RULES.md`. |

## How `tracker.py` is invoked

Not imported. The `revit-session` skill runs it in one line through the MCP
server's `execute_revit_code` tool:

```python
execfile(r"C:\path\to\tracker.py"); main("session_start", {"base": u"C:\\path\\to\\revit-tracking"})
```

Ops: `smoke`, `session_start`, `checkpoint`.

## Paths

- `tracker.py` and `last_run.json` live in `LIB_DIR`, which defaults to
  `~/.claude/revit-tracker` and can be overridden with the `REVIT_TRACKER_DIR`
  environment variable. This path **must stay ASCII** — the module header
  explains why, and the source is `# -*- coding: ascii -*-` enforced.
- The tracking data directory (snapshots, deltas, journals — one subfolder per
  project) is passed in at call time as `args["base"]`. It may contain non-ASCII
  characters; it is passed as an escaped literal so the wire stays ASCII.

## Design notes worth knowing before you edit

- **A read-only pass ends by raising `TrackerOK`.** That is deliberate — it
  forces Revit to roll back the host transaction so a snapshot never lands on the
  user's Undo stack. The real payload is always written to disk *before* the
  raise. The caller sees an HTTP 500 whose message is `TRACKER_OK|<run_id>`; that
  is success. `TrackerAbort` is a real failure (also rolls back).
- **`SNAPSHOT_FORMAT`** must be bumped whenever anything changes *how* a record
  is computed (the element filter, a signature function, the field layout). A
  baseline built by a different format version is not comparable and produces
  phantom additions/deletions.
- The attribution allow-list (`MCP_TX_NAMES`) is a closed set of exact
  transaction names derived from the MCP server source, not a substring match on
  `"MCP"`. If the server adds a transaction, re-derive the list — do not guess.
- All lock files use a **numeric epoch** heartbeat, never an ISO timestamp: a
  string ending in `Z` was measured being parsed as local time, making a lock
  look valid forever in a positive UTC offset.

See `../../docs/architecture.md` for the full picture.
