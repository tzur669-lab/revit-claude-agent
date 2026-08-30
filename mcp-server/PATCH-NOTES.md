# Local modifications to the bundled MCP server

`mcp-server/` is a fork of
[Demolinator/revit-mcp-server](https://github.com/Demolinator/revit-mcp-server)
(MIT — see `mcp-server/LICENSE`), pinned at upstream commit `40af5a7`
("Merge PR #2: fix get_revit_model_info formatting + stdio cold-start test").

Five source files carry local changes on top of that commit. `uv.lock` is
regenerated (lockfile format `revision 1 -> 3`, adds `upload-time` metadata; no
dependency versions changed).

To see the exact diff against upstream:

```bash
git clone https://github.com/Demolinator/revit-mcp-server /tmp/upstream
diff -ru /tmp/upstream mcp-server --exclude .git --exclude .venv --exclude __pycache__
```

---

## 1. `main.py` — pyRevit Routes port auto-discovery

**Upstream:** `REVIT_PORT = 48884` hardcoded.

**Change:** a `_discover_revit_port()` function. pyRevit Routes defaults to
48884, but if that port is still held when Revit starts (usually a previous Revit
process that has not released it) Routes silently increments. Each live Routes
server drops a `*_serverinfo.pickle` in `%APPDATA%\pyRevit` recording its port.
The function reads those, tries the newest first, then falls back to the
documented 48884, and picks the first port that actually accepts a TCP
connection. `REVIT_PORT` / `REVIT_HOST` environment variables still override.

**Why:** without this, every session after a Revit crash-restart connects to a
dead port and every tool call fails until the server config is edited by hand.

## 2. `revit_mcp/parameters.py` — preserve Unicode in `_safe_str`

**Upstream:** `_safe_str` iterated every character and replaced any codepoint
`>= 128` with `'?'`.

**Change:** return `unicode(value)` (IronPython 2.7) / `str(value)` directly. No
character stripping.

**Why:** JSON natively supports Unicode. Stripping non-ASCII turned every Hebrew
level / room / type name read out of the model into a run of `?`, making the
query tools useless on any localized model.

## 3. `revit_mcp/utils.py` — same fix for `normalize_string` / `sanitize_string`

**Upstream:** both did `str(text).encode('ascii', 'replace').decode('ascii')`.

**Change:** a `_to_text()` helper (tries `unicode` first, falls back to `str`);
`normalize_string` and `sanitize_string` use it and no longer force ASCII.

**Why:** identical to #2 — these helpers are on the path for element names in
several tool responses.

## 4. `revit_mcp/utils.py`, and all 20 route handlers — repair Hebrew coming **in**

**Upstream:** no equivalent. Text sent as a parameter to any route (friendly
tool or `execute_revit_code`) arrives already corrupted: pyRevit's own
request-body parsing (`pyrevit/routes/server/server.py`, not this repo) decodes
the UTF-8 bytes as Latin-1, one wrong "character" per original byte, and any
comparison against that text then fails silently — 0 results, no error.

**Change:** `repair_hebrew_text()` reverses the corruption
(`encode("latin-1").decode("utf-8")`, a safe no-op on text that was never
corrupted) and `repair_hebrew_in()` applies it recursively to every string in a
parsed JSON value. Every route handler calls `data = repair_hebrew_in(data)` as
the first thing it does with `request.data` — 20 call sites, one per handler,
not one shared patch.

**Why not one shared patch:** an earlier attempt monkeypatched pyRevit core's
own `HttpRequestHandler` from `startup.py`. It does not work — pyRevit runs a
registered route handler function in a *different engine scope* than the module
that registered it (see `handler.py`'s own comment on `base.Response`), so a
patch installed from the registration module never reaches the code that
actually parses a request. Verified live, 2026-08-24: the patch showed as
installed when checked immediately after being applied, yet `request.data` as
received by a handler stayed uncorrected. The fix has to run inside each
handler, at the point it actually reads its parameters.

**Why:** this is the inbound half of the encoding bug — the friendly-tool text
parameters (`type_name`, `level_name`, `room_name`, ...), not just
`execute_revit_code`. Without it, a Hebrew name passed to a friendly tool fails
silently and the only workaround is identifying elements by numeric `ElementId`.

## 5. `revit_mcp/code_execution.py` — `#!notx` opts out of the wrapping transaction

**Upstream:** every `execute_code` call is wrapped in one
`DB.Transaction(doc, ...)`, always started and always committed.

**Change:** code whose first line is `#!notx` runs with no wrapping transaction
and is expected to manage its own. The commit/rollback logic checks `t is not
None` throughout instead of assuming a transaction always exists.

**Why:** several of Revit's own edit scopes (`StairsEditScope`,
`SketchEditScope`, `TopographyEditScope`) refuse to start while the document is
already inside another modifiable transaction — the pre-existing wrapper made
those APIs entirely unreachable from `execute_revit_code`.

---

### Note on the encoding bug's other half

Both halves are now fixed in this fork. Text coming **out** of Revit is covered
by changes #2 and #3 above; text going **in** — including friendly-tool
parameters, not just `execute_revit_code` — is covered by #4. The reference at
`../memory-system/claude/skills/revit-session/references/hebrew-io.md`
predates #4 and still describes the inbound half as an agent-layer-only
workaround; it has not yet been re-verified against the friendly-tool case and
updated.
