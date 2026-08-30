# revit-claude-agent

Let [Claude Code](https://claude.com/claude-code) drive Autodesk Revit — and
remember what it did.

Two subsystems:

1. **The connection** (`mcp-server/`) — an MCP server that gives Claude 48 tools
   for building, editing, querying, and analyzing a live Revit model.
2. **The memory** (`memory-system/`) — a tracking layer that snapshots the model
   *before* any work, detects every change since last time (including edits a
   human made outside Claude), attributes each one by evidence, and writes a
   durable per-project journal. This is the part that "first maps what exists".

The MCP tools are stateless — every call forgets the last. The memory system is
what makes "pick up where we left off yesterday" and "someone changed these three
walls since Tuesday" possible.

> Personal tooling, shared as-is. Windows-only (Revit is), Revit 2024–2027,
> built around Claude Code specifically. Read `docs/` before relying on it.

---

## How it works

```
Claude Code
   │  MCP (stdio)
   ▼
mcp-server/  (CPython 3.11+, FastMCP)          ← this repo, bundled fork
   │  HTTP :48884
   ▼
pyRevit Routes  (IronPython 2.7, inside Revit) ← pyRevit, installed separately
   │
   ▼
Revit API
```

The memory system rides the same channel: the `revit-session` skill calls
`tracker.py` **inside Revit** through the server's `execute_revit_code` tool, so
a snapshot has direct API access and never has to trust a tool's return value.

| Player | Runs where | Role |
|---|---|---|
| `mcp-server/` | on your machine | MCP protocol, the 48 tool definitions |
| `memory-system/tracker/tracker.py` | inside the Revit process | snapshot, diff, attribution |
| `memory-system/claude/skills/revit-session` | Claude Code | the protocol: when to snapshot, checkpoint, and log |
| `revit-historian` agent | background | reconstructs a history briefing from disk, in parallel |
| `revit-scribe` agent | background | the sole writer of the journal / state files |
| `pyrevit-extension/` | inside Revit (ribbon button) | a manual helper: push a QTO selection to the clipboard for Claude |

Full write-ups: **[docs/architecture.md](docs/architecture.md)** (the connection
and the memory model) and **[docs/agent-workflow.md](docs/agent-workflow.md)**
(what happens around a single build command, end to end).

---

## Repo layout

```
mcp-server/            Bundled fork of Demolinator/revit-mcp-server (MIT).
                       Local changes: mcp-server/PATCH-NOTES.md
memory-system/
  tracker/             tracker.py (runs in Revit) + the PreToolUse hook
  claude/
    skills/revit-session/   the session protocol + reference docs
    agents/                  revit-historian, revit-scribe
    revit-lessons/RULES.md   global, cross-project, user-approved rules only
    settings.example.json    the hook wiring to merge into ~/.claude/settings.json
pyrevit-extension/     BIMAgents.extension — the "Extract QTO Data" ribbon button
docs/                  architecture + workflow write-ups
scripts/               install.ps1 / uninstall.ps1
config.example.jsonc   the two installation-specific paths
```

---

## Setup

### Prerequisites

| | |
|---|---|
| Windows 10/11 | Revit is Windows-only |
| Autodesk Revit | 2024, 2025, 2026, or 2027 |
| pyRevit | installed, with the **Routes server enabled** (pyRevit → Settings → Routes) |
| uv | Python package manager — <https://docs.astral.sh/uv/> |
| Claude Code | the memory system's skill/agent/hook model is Claude-Code-specific |
| A project open in Revit | the tools need an active document |

### 1. The MCP server

```bash
cd mcp-server
uv sync
```

Then register it with your MCP client. For Claude Code:

```bash
claude mcp add revit -- uv --directory /abs/path/to/mcp-server run main.py
```

Also install the `revit_mcp/` route handlers into Revit via pyRevit — see
[mcp-server/README.md](mcp-server/README.md) for both options. Verify pyRevit
Routes is up: open `http://localhost:48884/` in a browser.

### 2. The memory system

```powershell
./scripts/install.ps1 -TrackingDataDir 'C:\Users\YOU\revit-tracking'
```

This copies `tracker.py` + the hook to `~/.claude/revit-tracker/`, the
`revit-session` skill to `~/.claude/skills/`, and the two agents to
`~/.claude/agents/`. It backs up anything it replaces and never clobbers an
existing `RULES.md`. Pass `-Link` instead to symlink for in-place development.

Pick a `TrackingDataDir` **outside this repo** — real project names and snapshots
land there.

### 3. The hook

Merge the `hooks` block from
[memory-system/claude/settings.example.json](memory-system/claude/settings.example.json)
into `~/.claude/settings.json`, fixing the path to `hook_session_reminder.py`. It
fires once per session and reminds Claude to run the session protocol before the
first Revit call.

### 4. (optional) The pyRevit extension

Point pyRevit at `pyrevit-extension/` (pyRevit → Settings → Custom Extension
Directories), or copy `BIMAgents.extension/` into your pyRevit extensions folder.
Adds a **ClaudeIntegration** tab with one button, *Extract QTO Data*.

---

## Using it

Just ask Claude Code to work on the model ("add a floor with 6 rooms", "what
changed since last time?"). The hook reminds it to open a session; the skill
snapshots first, does the work, then `checkpoint`s and verifies against the diff.
The background agents restore history and write the journal without blocking.

Three things that trip people up (all detailed in the skill's references):

- A tracker call ends in an **HTTP 500 containing `TRACKER_OK` — that is planned
  success**. Verify the run id in `last_run.json`; do not retry.
- **A tool returning without an error does not mean it did anything.**
  `MoveElement` was measured succeeding while moving nothing. Verification is
  against the next checkpoint's diff.
- The Windows terminal renders Hebrew (and other non-ASCII) as `????`. That is
  display only — data on disk is fine.

---

## Licensing

This repo is MIT (`LICENSE`). `mcp-server/` is a fork of the MIT-licensed
[Demolinator/revit-mcp-server](https://github.com/Demolinator/revit-mcp-server);
its license is kept at `mcp-server/LICENSE` and the changes are listed in
`mcp-server/PATCH-NOTES.md`. See `NOTICE` for full attribution. pyRevit (GPLv3)
is a runtime dependency, not bundled.
