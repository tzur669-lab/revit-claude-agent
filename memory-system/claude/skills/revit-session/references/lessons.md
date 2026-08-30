# revit-lessons — user-corrected/approved rules

Global file: `~/.claude/revit-lessons/RULES.md`. Applies to
**every** Revit project, not just the current one. Read automatically by the
`PreToolUse` hook (`hook_session_reminder.py`) before the first
`mcp__revit__` call in a session — you do not need to load this reference to
receive its content, only to write to it.

Evidence backing a rule (long deltas, code snippets) lives in
`~/.claude/revit-lessons/evidence/<id>.md`, loaded only when that
specific rule is relevant.

## The gate — the one rule that must never break

A line is written to `RULES.md` **only** in one of two cases:

1. **Correction.** The user tells you something you did was wrong or not
   what they asked. Write immediately, no need to ask.
2. **Approval.** You propose a lesson; the user confirms it.

Nothing else qualifies, and that boundary is what keeps the file from
bloating into noise:

| Case | Correct destination |
|---|---|
| Bug you caught yourself during verification | `lesson` field in `journal.ndjson` |
| Tool/API trap with no user involvement | `references/` of this skill |
| A fact about this project specifically | `state.json` |

Every line carries `src: user-corrected <date>` or `src: user-approved
<date>`. A line without a valid `src` was written without authorization —
delete it on sight.

## Line format

One line per rule, `::`-delimited, English, imperative voice.

```
id :: trigger :: rule :: check :: src :: ev
```

```
L001 :: family-placement :: never trust insertion point = center; clamp bbox into room bbox after placing :: verify AABB vs wall rects @40mm before reporting done :: user-corrected 2026-08-18 :: ev:L001
```

`check` is an operational test, not advice. A line with no `check` is
usually a sign the rule is phrased too softly to act on.

## The trigger — what decides whether the rule fires in time

This is the part that decides whether the whole library is worth anything. A
rule that fires after the geometry is already built is documentation, not
prevention.

**Rule: the trigger names the activity about to start, not the symptom that
was discovered.**

Example — a correction says "stairs must be straight runs, not a wrapped
configuration":

| Phrasing | What happens |
|---|---|
| `wrapped-stair-fix` | Only fires after you already built it wrapped. Worthless. |
| `staircase-layout` | Fires when you start planning stairs, before geometry exists. **Correct.** |

### Closed vocabulary

`RULES.md` opens with the domain list. Every `trigger` must be one of these.
The list doubles as the index you scan, which is what prevents the likely
failure mode — one-off phrasings that never match any future task:

```
domains: level-creation · wall-layout · room-definition · floor-slab ·
  staircase-layout · family-placement · door-window-placement · mep-routing ·
  view-creation · view-export · schedule-sheet · annotation-dimension ·
  hebrew-text-io · attribution-reporting · geometry-verification ·
  user-reporting · tracker-dev
```

Adding a domain is a deliberate act — a line added to the header — not a
side effect of writing a rule. When a correction could be filed narrow or
broad, **file it broad**: a rule that fires once unnecessarily costs one
line of reading; a rule that never fires is dead weight.

Rules are grouped by domain and kept contiguous in the file, so one match
pulls in everything relevant to that activity.

### The firing point — without it, trigger is a label, not a mechanism

This is enforced as an explicit step in `SKILL.md`: **before starting any
batch of work on the model, name the domain of that batch and apply every
rule carrying it.** This costs one line of thought — the list is already in
context, injected by the hook.

## Writing

The main thread writes, at the moment the correction happens. Not through
the scribe's queue — a queued item vanishes if the session closes, and the
moment of correction is mid-conversation with the user.

### Locking — the file is global, existing locks don't cover it

`.lock` belongs to `tracker.py` and `.scribe.lock` sits inside an instance
directory — both are per-project. Two sessions working on **different**
projects that add a lesson at the same moment would collide on this same
`RULES.md` with no protection, and `>>` in Git Bash on Windows is not an
atomicity guarantee. Hence `~\.claude\revit-lessons\.lessons.lock`, same
pattern already proven in the scribe: `noclobber` for atomic creation,
numeric `hb_epoch`, stale lock taken over after 120s. Epoch, not ISO — a
timestamp ending in `Z` has already been misread here once as local time,
making a lock look forever-fresh in a positive UTC offset.

```bash
LOCK=~/.claude/revit-lessons/.lessons.lock
NOW=$(date +%s)
if [ -f "$LOCK" ]; then
  HB=$(grep -o '"hb_epoch":[0-9]*' "$LOCK" | cut -d: -f2)
  if [ -n "$HB" ] && [ $((NOW - HB)) -lt 120 ]; then echo "LESSONS_BUSY"; exit 0; fi
  rm -f "$LOCK"
fi
( set -o noclobber; echo "{\"hb_epoch\":$NOW}" > "$LOCK" ) 2>/dev/null || { echo "LESSONS_BUSY"; exit 0; }
```

Locking covers both write paths, not just appends:

- **New rule:** lock ⇒ append via heredoc ⇒ verify the line landed whole ⇒
  unlock.
- **Sharpening an existing rule:** lock ⇒ `Read` + `Edit` the line ⇒ unlock.
  The lock must wrap the pair — otherwise another session inserts a line
  between the read and the edit, and the `Edit` lands on stale content.
- `LESSONS_BUSY` ⇒ wait and retry once. Still locked ⇒ tell the user the
  lesson was not recorded. **Never fail silently** — that is exactly the
  failure this system exists to prevent.

Before any addition: scan for an identical `trigger`. One exists ⇒
**sharpen it in place, don't add a nearby line.** Two lines on the same
trigger are two sources for one fact, and a second source exists to
contradict.

## Cap and dedup

`30` rules or `~4KB`, whichever is lower. Above the cap — don't just delete:
a rule fully absorbed into `references/` drops from the list and leaves a
pointer, and two rules with overlapping triggers merge. This file is read
every session; its length is a fixed tax, so it must stay a living rule set,
not an archive.

## Seeding

`RULES.md` starts **empty, header only**. History contains clear candidates
(family insertion point, partial view restore after export) but none of
them passed the gate yet — present them as a list, and only what the user
approves gets written as `L001` onward.
