# Attribution — report exactly what the evidence supports

Loaded when the diff is non-empty. Every item in `items` carries `by`, plus `tx`
when a transaction name was captured. `result.attribution` summarizes the counts.

| `by` | Evidence | How to phrase it |
|---|---|---|
| `claude` | Changed under a transaction whose name contains `MCP` | "Claude · \<action\>" |
| `human` | Changed under a transaction Revit itself named | "Human · action name: `Move`" — quote the `tx` name, it is the strongest evidence there is |
| `sync_incoming` | Arrived during a sync-with-central / reload-latest window | "Pulled from the central model" — someone else's work, not the current user's |
| `unknown` | No covering event | "A change not made by Claude" |

## `unknown` is not "human"

**Never upgrade `unknown` to "human".** That value also covers Revit's own
regeneration, and any edit made before the listener was armed — a restart of
Revit, or the first run on a project.

The listener exists only inside a live Revit session. Edits made while Revit was
closed will always come back `unknown`, and that is honest, not a defect.

## `join_collateral` — do not double-count

Moving one wall changes the geometry of every wall joined to it, and they all
carry the same `tx`. Report the direct action, then the side effect separately —
do not present them as N independent edits.

Measured: moving one wall by `1.00 m` produced `4` changed walls. One moved, three stretched.

## `worksharing`

In a workshared model, `result` gains a `worksharing` field with the owners of
the changed elements. That is additional evidence, not a replacement for `by`.

## Derived copy — `unknown` is the right answer

An element that holds a copy of another element's data changes along with it, but
Revit only reports the one that was touched in `DocumentChanged`.

Measured: a change to a 3D view produced `m` = `1` in the event while the
snapshot saw `2` — the `Camera` element changed **with no `tx` field at all**.

So a derived copy necessarily falls to `unknown`. **That is correct behavior,
not a bug, and must not be upgraded to "human".**

## An internal fix-up round is not visible in `added`/`deleted`

An element created and deleted between two snapshots will not appear in the diff
at all — both sides are identical. Only `transactions` in the delta file shows
there was a round.

When reporting "N elements created", the number is the net. If there were
mid-stream fix-ups, read `transactions` to describe what actually happened.
