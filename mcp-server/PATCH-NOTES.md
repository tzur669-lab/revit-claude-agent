# Local modifications to the bundled MCP server

`mcp-server/` is a fork of
[Demolinator/revit-mcp-server](https://github.com/Demolinator/revit-mcp-server)
(MIT — see `mcp-server/LICENSE`), pinned at upstream commit `40af5a7`
("Merge PR #2: fix get_revit_model_info formatting + stdio cold-start test").

Three source files carry local changes on top of that commit. `uv.lock` is
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

---

### Note on the encoding bug's other half

These fixes cover text coming **out** of Revit. Text going **in** as a friendly-tool
parameter (`level_name`, `type_name`, ...) is still corrupted upstream of any code
in this repo, inside pyRevit's own request handler. That is handled at the agent
layer — see
`../memory-system/claude/skills/revit-session/references/hebrew-io.md`.
