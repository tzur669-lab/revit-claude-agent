# Troubleshooting

## Two results that are not failures

### `"status": "partial"`

The time budget ran out mid-snapshot. `progress` shows `done` / `total`.

**Call the same op again, exactly** to continue from the cursor, and repeat until
`"status": "complete"`. A partial pass produces no diff and writes nothing to
disk, because a partial snapshot must never be mistaken for a baseline. A chunk
always processes at least `64` elements, so the loop always terminates.

If the model changed between chunks, the partial is abandoned and rebuilt
(`progress.resumed` comes back `false`). That is the correct behavior —
continuing would stitch two model states into one snapshot.

**Do not create a writeups queue item for `partial`** — there is no diff at all.

### `"error": "LOCKED_BY:<run>"`

Another Claude session is mid-run on this project. Wait and retry. A lock whose
heartbeat is older than `120` seconds is taken over automatically.

## A real failure

If the message does **not** contain `TRACKER_OK|<run_id>`, or `last_run.json`
does not exist, or its `run` field is not identical to that `run_id` — that is a
real failure. Read the traceback. **Do not retry blindly.**

## A mutation that succeeded and did nothing — the mechanism

The rule itself is in the skill core. This is the explanation for why it exists.

Measured: `ElementTransformUtils.MoveElement` returned successfully, with no
error, and moved nothing. The reason: the wall was parallel to the move axis, so
the move was along its own length, and the wall joins at both ends stretched it
back to the exact same endpoints.

The move was recorded as success at every layer of the API. Only the
`checkpoint` diff showed there was no change.

Moving that same wall by `1.00 m` on the perpendicular axis was detected
correctly (`moved_ft = 3.2808`). So the problem is not in detection but in the
assumption that absence of an error equals execution.

## `params_note: "other"` — a parameter change with no name

The item says the element's parameters changed, but none of the listed
parameters moved, so there is nothing to report beyond the fact.

For view-family elements — `Views`, `Sheets`, `Cameras`, `Viewers` — this should
no longer happen. They hold a full parameter map in a blob, and the diff returns
`params_changed` with the BuiltInParameter name and its before/after values:

```json
"params_changed": [["VIEW_DETAIL_LEVEL", 2, 3]]
```

`other` will return for them only once, right after this mechanism was added,
when the prior side of the comparison was still written without the map. From the
next change on — name it.

For the other categories `other` is still valid and expected: they rely on
`KEY_PARAM_NAMES`, and a parameter not on the list changes the hash without a
name to give.

## A fix in `tracker.py` that looks like it did not work

`ours` (the flag that decides whether a transaction is ours) is computed **at
event time**, inside the listener's closure. `execfile` builds a fresh namespace
on every run, so a listener armed before the edit keeps running the old code —
until the next snapshot, which re-arms it.

Measured: after a fix to `_tx_is_ours`, the first operation was still reported
with the old attribution. Only after a `checkpoint` re-armed the listener was the
next operation reported correctly.

**After every change to `tracker.py`: run one snapshot before concluding the fix
failed.** The first event after the edit still belongs to the previous rollout.

## A `human` attribution that looks suspicious

If an operation we performed comes back `human`, check the `tx` name.
`MCP_TX_NAMES` in `tracker.py` holds the five extension transaction names that do
not carry `MCP`. If the extension was updated and added a new such name,
**re-derive the list** from the extension source (`revit_mcp/*.py`, search
`Transaction(doc,`) — do not add a name by guessing. A wrong value in the list
tags human work as ours.

## `scope`

Tracking records model content and annotation only. Settings and document
resources are excluded.

Measured on a standard course project: `1044` elements were actually `422`
Materials, `125` Space Type Settings, `117` Legend Components — and only `13` walls.

If the unfiltered view is needed, pass `{"scope": "all"}` — but that invalidates
the baseline, and the format keeper will rebuild it properly instead of reporting
the change.

## Measured rate

About `800` elements per second. An `18`-second budget is enough for about
`14,000` elements before chunked work is needed. A typical course model (`76`–`142`
elements) finishes in `0.9`–`2.3` seconds in a single pass.
