# Docs index

**Start with [../HANDOFF.md](../HANDOFF.md).** Open anything below only when the task needs it.

## Core (repo-level)

| Doc | What it is | Read when |
|---|---|---|
| [../HANDOFF.md](../HANDOFF.md) | Purpose, layers, invariants, "touch X → watch Y" | **always, first** |
| [architecture.md](architecture.md) | System diagram, modules, coupling, execution spine summary | changing structure, or a module you haven't seen |
| [operation-contracts.md](operation-contracts.md) | Per-tool L1/L2 verification contract (large, ~880 lines) | editing a route handler, op, evaluator — read **only that tool's section** (`grep -n "^###"`) |
| [TOOLS.md](TOOLS.md) | Generated 57-tool table (route, risk, contract) | looking up a tool; never hand-edit |
| [agent-workflow.md](agent-workflow.md) | Narrative of one build command end to end (snapshot → work → checkpoint → journal → lesson) | onboarding to the memory layer |
| [measured-traps.md](measured-traps.md) | Silent Revit/IronPython failures found live | family placement, view edits, locks, UTF-8 file reads, number formatting |
| [history/design-log.md](history/design-log.md) | Phase A/B design rationale + live-verification records | before changing crash-resume, `created_by`, evaluators, probes |

## Component docs

| Doc | What it is | Read when |
|---|---|---|
| [../README.md](../README.md) | Public overview + setup | installing |
| [../mcp-server/PATCH-NOTES.md](../mcp-server/PATCH-NOTES.md) | Every local change vs. upstream fork, with reasons | touching `main.py`, `utils.py`, encoding, `#!notx`, verification layer |
| [../mcp-server/README.md](../mcp-server/README.md) | Upstream README (install, transports, tool list) — counts are test-checked | server setup / MCP client config |
| [../mcp-server/LLM.txt](../mcp-server/LLM.txt) | Upstream guide to writing a new tool + route | adding a tool (also update registry + regen TOOLS.md) |
| [../mcp-server/README_UV.md](../mcp-server/README_UV.md) | Upstream uv notes (legacy; clone step is already done) | rarely |
| [../memory-system/tracker/README.md](../memory-system/tracker/README.md) | tracker.py + hook: invocation, paths, edit-before notes | editing `tracker.py` or the hook |
| [../config.example.jsonc](../config.example.jsonc) | The 3 installation-specific values | install / port problems |

## Runtime docs (consumed by Claude in a Revit session — keep their format)

| Doc | Loaded when |
|---|---|
| [revit-session/SKILL.md](../memory-system/claude/skills/revit-session/SKILL.md) | before any `mcp__revit__` call; has its own reference-routing table |
| `references/notes.md` · `attribution.md` · `checkpoint-queue.md` · `lessons.md` · `troubleshooting.md` · `hebrew-io.md` · `project-log-format.md` | per the SKILL.md routing table (`hebrew-io.md` is flagged possibly stale) |
| [agents/revit-historian.md](../memory-system/claude/agents/revit-historian.md) · [agents/revit-scribe.md](../memory-system/claude/agents/revit-scribe.md) | agent definitions (installed to `~/.claude/agents/`) |
| [revit-lessons/RULES.md](../memory-system/claude/revit-lessons/RULES.md) | injected by the hook every session; user-gated writes only |
