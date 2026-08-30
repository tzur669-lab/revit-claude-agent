# Non-ASCII text going into the model

Loaded when writing or comparing non-ASCII text (Hebrew, etc.) **into** Revit — a
room, level, type, or view name.

This is a separate topic from terminal display corruption. Here it is data
corruption, not rendering.

## What exactly happens — measured, not assumed

Non-ASCII text sent in any `mcp__revit__` call reaches IronPython with each
**byte** of the UTF-8 sitting in its own character. Measured 2026-08-16 sending
`u"קרקע"` ("ground"):

```
received : [215, 167, 215, 168, 215, 167, 215, 162]   length 8
expected : [1511, 1512, 1511, 1506]                    length 4
```

`215, 167` are exactly the UTF-8 bytes of `ק`. That is, the bytes were decoded as Latin-1.

**Source of the bug, located:** `pyrevit/routes/server/server.py` — the body is
read with `self.rfile.read(...)` and passed to `json.loads` with no explicit
`.decode("utf-8")`. That is pyRevit's own code (`C:\Program Files\pyRevit-Master\`),
not the extension and not this repo.

The failure is **silent**: a string comparison simply finds nothing, and a tool
like `create_line_based_element` returns `"Created 0 element(s)"` with no error.

## The fix: the corruption is reversible with no loss

Each character is one original byte, so the string can be reconstructed exactly.
**Inside `execute_revit_code`, just fix it on the way in:**

```python
def fix(s):
    return s.encode("latin-1").decode("utf-8")

want = fix(u"קרקע")          # -> [1511, 1512, 1511, 1506]
```

Verified end to end: `fix()` returns the same result as the `unichr()` method,
and locates the real Hebrew level in the model (`Level 311`).

**Meaning:** inside `execute_revit_code` you may write non-ASCII text naturally
and wrap it in `fix()`. No more building strings from codepoint lists by hand.

## What this does **not** solve

`fix()` only works where our code runs. It does **not** help the text parameters
of the friendly tools — `type_name`, `level_name`, `name`, `number`, `view_name`
— because the string is consumed by the extension before our code exists.

Measured: `get_revit_view(view_name="דרומית")` returned `404` even though the
name appears fine in `list_revit_views`. Reading **from** Revit is always fine;
writing **into** a text parameter is suspect by default.

So, when non-ASCII names are involved:

1. **Identify by numeric `ElementId`** — from `list_levels`, or `GetTypeId()` on
   an element already located. Never by a name string through a friendly tool.
2. **Prefer `execute_revit_code`** over the `create_*` tools. It already wraps a
   transaction — do not open another — and inside it `fix()` solves everything.
3. Clean up any temporary element created during diagnosis. A wall that landed on
   the default level because `level_name` failed silently will not report an
   error, it will just sit there.

## `unichr()` — still valid as a fallback

```python
def uh(codes): return u"".join([unichr(c) for c in codes])
want = uh([1511, 1512, 1511, 1506])   # קרקע
```

Equivalent to `fix()` in result. Useful when you need pure ASCII the whole way,
or when you are not sure the string went through the corruption path (e.g. text
already fixed once — `fix()` twice will break it).

**Rule of thumb:** a string that came from outside ⇒ `fix()`. A string built
from scratch in code ⇒ `uh()`.

## After writing non-ASCII text — read it back and compare

`fix()` solves the input, not the result. A corrupted name **is saved to the
file with no warning**, and from there it is already part of the model.

Every write of non-ASCII text into the model ends with a read-back of that same
field and a comparison to what was intended. That is the only moment it can be caught.

## Temporary change for a workaround — restore **everything** you touched

The workaround for a non-ASCII name is a temporary change to ASCII, do the
operation, restore.

Save and restore **every field the operation touched**, not just the name.
Measured: `VIEW_DETAIL_LEVEL` stayed altered after the name was restored, and a
separate round was needed to clean it.

Before such a change — confirm exactly which element it is by `Id`, do not assume
by the collector's return order. That actually caused the wrong view to be changed once.

## Terminal display corruption — a different topic

The Windows console turns non-ASCII text into `????`. That is rendering only. The
data on disk is fine — `snapshot.tsv` is pure ASCII with `\uXXXX` escapes.
Verify through `json.dumps`, not through printing to the console.
