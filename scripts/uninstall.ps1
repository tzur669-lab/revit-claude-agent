<#
.SYNOPSIS
    Remove the memory-system pieces installed by install.ps1 from ~/.claude.

.DESCRIPTION
    Deletes the tracker files, the revit-session skill, and the two agents.
    Leaves RULES.md and any *.bak-* backups alone by default. Does not touch
    settings.json - remove the PreToolUse hook block by hand. Does not touch
    your tracking-data directory (your project history lives there).

.PARAMETER ClaudeHome
    Default: ~/.claude

.PARAMETER IncludeLessons
    Also delete user data and backups: ~/.claude/revit-lessons/RULES.md,
    ~/.claude/revit-lessons/PREFERENCES.md, ~/.claude/revit-design-rules/
    (may hold rules you edited) and ~/.claude/revit-backups/.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ClaudeHome = (Join-Path $HOME '.claude'),
    [switch]$IncludeLessons
)

$ErrorActionPreference = 'Stop'

$targets = @(
    'revit-tracker\tracker.py'
    'revit-tracker\hook_session_reminder.py'
    'skills\revit-session'
    'agents\revit-historian.md'
    'agents\revit-scribe.md'
)
if ($IncludeLessons) {
    $targets += 'revit-lessons\RULES.md'
    $targets += 'revit-lessons\PREFERENCES.md'
    $targets += 'revit-design-rules'
    $targets += 'revit-backups'
}

foreach ($rel in $targets) {
    $p = Join-Path $ClaudeHome $rel
    if (Test-Path $p) {
        if ($PSCmdlet.ShouldProcess($p, 'Remove')) {
            Remove-Item -Recurse -Force -LiteralPath $p
            Write-Host "removed $p" -ForegroundColor Green
        }
    } else {
        Write-Host "not present: $p" -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($IncludeLessons) {
    Write-Host "Left in place: settings.json hook block (remove by hand) and your tracking-data directory." -ForegroundColor Cyan
} else {
    Write-Host "Left in place: settings.json hook block (remove by hand), RULES.md, PREFERENCES.md, revit-design-rules/, revit-backups/, *.bak-* backups, and your tracking-data directory. Pass -IncludeLessons to remove the user-data items." -ForegroundColor Cyan
}
