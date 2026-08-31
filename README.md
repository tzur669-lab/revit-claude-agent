# revit-claude-agent

**Give an AI assistant real hands inside Autodesk Revit — and a memory of every change it makes.**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Revit 2024–2027](https://img.shields.io/badge/Revit-2024--2027-blue.svg)
![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Built for Claude Code](https://img.shields.io/badge/built%20for-Claude%20Code-8A2BE2.svg)

BIM models change constantly, and usually by more than one hand. When an AI
assistant joins that loop it can move fast — but it has amnesia: every request
starts from zero, and nothing records what it touched or why. `revit-claude-agent`
addresses both halves. It connects [Claude Code](https://claude.com/claude-code)
to a live Revit session through 48 modelling tools, and wraps that connection in
a tracking layer that photographs the model *before* any work, detects every
change since last time — including edits a colleague made by hand in Revit —
attributes each one to a person or to the AI **by evidence**, and writes a
durable, per-project journal that captures the *intent* behind a change, not just
the geometry that moved.

The practical upshot for a design team: you can ask for "a mirrored apartment on
level 4", come back the next day, and ask *"what changed since Tuesday, and who
did it?"* — and get a real answer, backed by a snapshot diff rather than someone's
memory.

> Personal tooling, shared as-is. Windows only (Revit is), Revit 2024–2027, built
> specifically around Claude Code. Read [`docs/`](docs/) before relying on it in
> production.

---

## What can it do?

- **Automate quantity take-off (QTO).** Select beams and slabs in Revit; get back
  grouped totals — length, concrete/steel volume, slab area, counts per type —
  with any missing or suspicious geometry flagged for review.
- **Build repetitive floor plates.** Levels, walls, rooms, doors, windows and
  furniture laid out across identical floors from a single instruction.
- **Follow a moving element.** Relocate a straight-run stair core and see every
  wall that stretched with it — reported as one deliberate move plus its side
  effects, not a dozen mystery edits.
- **Audit against code.** Interior clear widths, minimum room areas and widths,
  exterior-vs-interior dimension mismatches — checked against a ruleset you supply
  *(the original setup uses Israeli residential code; any standard works)*.
- **Catch drift between sessions.** *"These three walls moved since Tuesday"* —
  each change attributed to a human (a manual Revit edit), the AI, or a
  workshared sync, from transaction evidence rather than a guess.
- **Keep work done outside the AI in the record.** A colleague's manual edits are
  captured and journaled on the next session, so the project history stays whole.
- **Reach the rest of the model.** Clash detection, MEP routing, view / sheet /
  schedule creation, IFC export, and arbitrary Revit API scripting through the
  48 MCP tools.
- **Record a "why", not just a "what".** Every journal entry keeps the request and
  the reasoning ("identical mirrored-apartment plan around a central core"), so
  *"725 elements added"* is still legible a month later.

---

## How it works

Two subsystems, one channel:

```
Claude Code
   │  MCP  (a standard way to give an AI assistant real tools)
   ▼
mcp-server/          CPython 3.11+, FastMCP           ← this repo (bundled fork)
   │  HTTP :48884
   ▼
pyRevit Routes       IronPython 2.7, inside Revit     ← pyRevit (installed separately)
   │
   ▼
Revit API
```

**The connection** (`mcp-server/`) exposes 51 tools for building, editing,
querying and analysing a live model — including a relationship inspector, an
always-rolled-back delete-impact dry run, and a design-standards validator. It
is stateless — every call forgets the last.

**The memory** (`memory-system/`) rides the same channel: the `revit-session`
skill runs `tracker.py` *inside Revit* through the server's code-execution tool,
so a snapshot has direct API access and never has to trust a tool's return value.
It is the part that "first maps what already exists" before anything is touched.

| Player | Runs where | Role |
|---|---|---|
| `mcp-server/` | your machine | the MCP protocol and the 48 tool definitions |
| `memory-system/tracker/tracker.py` | inside the Revit process | snapshot, diff, attribution |
| `revit-session` skill | Claude Code | the protocol — when to snapshot, checkpoint, and log |
| `revit-historian` agent | background | rebuilds a history briefing from disk, in parallel |
| `revit-scribe` agent | background | the sole writer of the journal / state files |
| `pyrevit-extension/` | Revit ribbon button | a manual helper — push a QTO selection to the clipboard for Claude |

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
| Claude Code | the memory system's skill / agent / hook model is Claude-Code-specific |
| A project open in Revit | the tools need an active document |

### 1 · The MCP server

```bash
cd mcp-server
uv sync
```

Register it with your MCP client. For Claude Code:

```bash
claude mcp add revit -- uv --directory /abs/path/to/mcp-server run main.py
```

Also install the `revit_mcp/` route handlers into Revit via pyRevit — see
[mcp-server/README.md](mcp-server/README.md) for both options. Verify pyRevit
Routes is up: open `http://localhost:48884/` in a browser.

### 2 · The memory system

```powershell
./scripts/install.ps1 -TrackingDataDir 'C:\Users\YOU\revit-tracking'
```

Copies `tracker.py` + the hook to `~/.claude/revit-tracker/`, the `revit-session`
skill to `~/.claude/skills/`, and the two agents to `~/.claude/agents/`. It backs
up anything it replaces and never clobbers an existing `RULES.md`. Pass `-Link`
instead to symlink for in-place development.

Pick a `TrackingDataDir` **outside this repo** — real project names and snapshots
land there.

### 3 · The hook

Merge the `hooks` block from
[memory-system/claude/settings.example.json](memory-system/claude/settings.example.json)
into `~/.claude/settings.json`, fixing the path to `hook_session_reminder.py`. It
fires once per session and reminds Claude to run the tracking protocol before the
first Revit call.

### 4 · The pyRevit extension *(optional)*

Point pyRevit at `pyrevit-extension/` (pyRevit → Settings → Custom Extension
Directories), or copy `BIMAgents.extension/` into your pyRevit extensions folder.
Adds a **ClaudeIntegration** tab with one button, *Extract QTO Data*.

---

## Using it

Describe what you want in plain language — *"add a floor with six rooms"*,
*"what changed since last time?"*, *"check every room on level 3 against the
minimum areas"*. The hook reminds Claude to open a tracking session; the skill
photographs the model first, does the work, then checkpoints and verifies the
result **against the snapshot diff** — not against whether a tool call returned
without an error. The two background agents rebuild history and write the journal
without slowing the work down.

> _[Insert GIF/screenshot demonstrating the CLI and Revit side-by-side here]_

### Three things that trip people up

> [!IMPORTANT]
> A tracker call ends in an **HTTP 500 whose message contains `TRACKER_OK`. That
> is planned success, not a failure.** The read-only pass raises on purpose to
> keep snapshots off Revit's Undo stack; the real result is already on disk.
> Verify the run id in `last_run.json` and move on — do not retry.

> [!WARNING]
> **A tool call returning without an error is not proof it did anything.**
> `MoveElement` has been measured returning success while moving nothing. Every
> change is confirmed against the next checkpoint's diff before it is reported as
> done.

> [!NOTE]
> The Windows terminal renders Hebrew and other non-ASCII text as `????`. That is
> a display artifact only — the data written to disk is correct UTF-8.

---

## Engineering notes

Three decisions that shaped the design, expanded in [`docs/`](docs/):

- **Verify against the model, never the return code.** A build step counts as
  done only when the next snapshot diff shows it — measured after tools reported
  "success" on moves and placements that silently did nothing.
- **Attribution is by evidence.** Each change is matched to the exact transaction
  name Revit recorded. `unknown` is a first-class answer and is never upgraded to
  "human" by guessing — mislabelling a person's work as the AI's is the one
  mistake the system must not make.
- **The wire stays ASCII.** Non-ASCII text (Hebrew level and room names) is
  double-encoded in transit and fails *silently* — zero matches, no error. The
  tracker keeps its own path pure ASCII and the fork preserves Unicode on the way
  back out; the skill documents the reversible fix.

---

## Licensing & credits

This repository is MIT (see [`LICENSE`](LICENSE)). `mcp-server/` is a fork of the
MIT-licensed
[Demolinator/revit-mcp-server](https://github.com/Demolinator/revit-mcp-server)
(itself derived from
[mcp-servers-for-revit](https://github.com/mcp-servers-for-revit/mcp-server-for-revit-python));
its licence is kept at `mcp-server/LICENSE` and the local changes are listed in
[`mcp-server/PATCH-NOTES.md`](mcp-server/PATCH-NOTES.md). See [`NOTICE`](NOTICE)
for full attribution. [pyRevit](https://github.com/pyrevitlabs/pyRevit) (GPLv3)
is a runtime dependency, not bundled.
