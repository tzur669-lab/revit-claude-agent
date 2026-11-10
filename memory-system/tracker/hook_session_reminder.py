"""PreToolUse hook: remind Claude to run the revit-session protocol.

Fires once per Claude Code session, before the first mcp__revit__ tool call.
Silent on every later call in the same session.

Lives in a file rather than inline in settings.json on purpose. Inlining it
meant a shell string inside a JSON string inside a sed expression, and the
`\\1` backreference silently degraded into the control character 0x01 -- the
hook then extracted an empty session id and never fired. A file has no
escaping layers to get wrong.
"""
import sys
import os
import json
import time

REMINDER = (
    "REVIT TRACKING: invoke the revit-session skill and run its session_start "
    "protocol BEFORE this Revit call. It identifies the open document, restores "
    "prior work history, and detects changes made outside Claude. "
    "Tracker calls end in a 500 containing TRACKER_OK - that is SUCCESS, not "
    "failure: verify the run id in last_run.json and do not retry. "
    "Fire the session_start call and the revit-historian agent (background) in "
    "the SAME message - the historian restores history from disk in parallel, "
    "and running it afterwards wastes a serial turn for nothing."
)


MARK_TTL_SEC = 7 * 24 * 3600

# revit-lessons: user-corrected/approved rules, injected once per session so
# they don't have to be recalled from memory. See
# skills/revit-session/references/lessons.md for the write side (the gate,
# the trigger vocabulary, the lock). This hook only reads.
RULES_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "revit-lessons", "RULES.md"
)
RULES_MAX_BYTES = 6000


def _prune(marks):
    """Drop marks older than the TTL.

    One empty file is written per Claude session and nothing ever removed them,
    so the directory grew without bound. A mark only has to outlive the session
    that created it; a week is far past that. Failure here is never allowed to
    matter -- the caller is a hook in front of a Revit call.
    """
    try:
        cutoff = time.time() - MARK_TTL_SEC
        for nm in os.listdir(marks):
            p = os.path.join(marks, nm)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                continue
    except Exception:
        pass


def _load_rules():
    """Read RULES.md and return injectable text, or "" if there's nothing to add.

    Never allowed to raise or block the Revit call over this -- a missing
    file (nothing approved yet) is silent, and an oversized file is
    truncated with a pointer rather than skipped outright, so a rule near
    the top is still seen even on a day the file grew past the cap.
    """
    try:
        if not os.path.isfile(RULES_PATH):
            return ""
        with open(RULES_PATH, "rb") as f:
            raw = f.read(RULES_MAX_BYTES + 1)
        truncated = len(raw) > RULES_MAX_BYTES
        if truncated:
            raw = raw[:RULES_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        if truncated:
            text += (
                "\n[TRUNCATED - read the rest with Read on " + RULES_PATH + "]"
            )
        return (
            "\n\nREVIT LESSONS (user-corrected/approved rules - apply the ones "
            "matching the domain of the batch you're about to run):\n" + text
        )
    except Exception:
        return ""


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    sid = str(data.get("session_id") or "nosession")
    sid = "".join(c for c in sid if c.isalnum() or c in "-_")[:80] or "nosession"

    marks = os.path.join(os.path.expanduser("~"), ".claude", "revit-tracker", ".marks")
    try:
        if not os.path.isdir(marks):
            os.makedirs(marks)
        mark = os.path.join(marks, sid)
        if os.path.exists(mark):
            return                      # already reminded this session
        open(mark, "w").close()
        _prune(marks)
    except Exception:
        return                          # never block a Revit call over bookkeeping

    context = REMINDER + _load_rules()

    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    main()
