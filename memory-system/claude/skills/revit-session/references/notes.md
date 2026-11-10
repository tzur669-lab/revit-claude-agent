# notes — what each note from `session_start` means

Loaded when `notes` in `last_run.json` comes back non-empty. The value is an
array, and several notes can return together.

| Note | Meaning | What to do |
|---|---|---|
| `NEW_INSTANCE` | First time this file has been seen | Create `journal.ndjson`/`state.json` per the template in `project-log-format.md` |
| `BASELINE_CREATED` | No prior state to compare against | Say so explicitly; do **not** claim "no changes" |
| `PATH_CHANGED` | The file was renamed or moved | Continue as normal, note it in the journal |
| `REBASELINE_*` | The old baseline is not comparable (format or scope changed) | **Never** report the diff as user changes |
| `SYNC_WINDOW_EXPIRED` | A sync started and never reported completion | `sync_incoming` labels in that window were downgraded to `unknown`; say attribution for that window is unreliable |
| `CHUNK_n_OF_MANY` | Budget ran out, the snapshot is partial | Call the same op again — see `troubleshooting.md` |
| `BASELINE_LOST` | The snapshot is unreadable, and so is the `.bak` | Rebuild the baseline, document it, and never report phantom changes |
| `ROLLBACK_SUSPECTED_*` | A backup file, or the save count dropped | **Stop and ask the user** before writing anything |
| `SHARES_LINEAGE_WITH_n` | Same Revit template as other projects | Informational only — they are **not** the same project |
| `UNSAVED_DOCUMENT` | A document that has never been saved | Cannot be tracked; explain to the user why |
| `FAMILY_DOCUMENT_NOT_TRACKED` | A Family document, not a project | Cannot be tracked; explain to the user why |

## `SHARES_LINEAGE_WITH_n` — the note easiest to get wrong

`lineage` is `ProjectInformation.UniqueId`, and it comes from the template the
file was created from — not from the project. Three files created from the same
course template share the exact same lineage.

That is, `lineage` is **not a project identifier**. Identification in the system
is by canonical path (`ident.canon`) only. Do not merge records of projects that
share a lineage, and do not pull the history of one while working on another.
