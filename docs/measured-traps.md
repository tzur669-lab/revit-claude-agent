# Measured traps

Moved verbatim out of `docs/architecture.md` on 2026-11-10. Silent Revit/IronPython
failures found live. Read before: placing families, touching view names/params,
writing lock code, reading UTF-8 files inside IronPython, or formatting numbers in
route code.

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
