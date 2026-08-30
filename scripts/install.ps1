<#
.SYNOPSIS
    Install the memory-system pieces into your Claude Code home (~/.claude).

.DESCRIPTION
    Copies (or symlinks, with -Link) the tracker, the revit-session skill, and
    the two agents into ~/.claude. Does NOT edit ~/.claude/settings.json - it
    prints the hook block for you to merge by hand. Does NOT touch mcp-server/
    (install that separately - see its README).

    Re-running is safe. Existing files are backed up to *.bak-<timestamp> before
    being replaced. RULES.md is never overwritten unless you pass -Force, because
    it accumulates your approved lessons.

.PARAMETER TrackingDataDir
    Absolute path to your tracking-data directory (browsable, kept OUTSIDE this
    repo). If given, "<TRACKING_DATA_DIR>" is substituted into the installed
    SKILL.md and revit-historian.md. If omitted, you edit them by hand later.

.PARAMETER ClaudeHome
    Target Claude home. Default: ~/.claude

.PARAMETER Link
    Create directory symlinks / file hardlinks instead of copying (for
    developing this repo in place). Needs Developer Mode or an elevated shell.

.PARAMETER Force
    Also overwrite an existing RULES.md.

.EXAMPLE
    ./scripts/install.ps1 -TrackingDataDir 'C:\Users\me\revit-tracking'
#>
[CmdletBinding()]
param(
    [string]$TrackingDataDir,
    [string]$ClaudeHome = (Join-Path $HOME '.claude'),
    [switch]$Link,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$src  = Join-Path $repo 'memory-system'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

function Backup-IfExists([string]$path) {
    if (Test-Path $path) {
        $bak = "$path.bak-$stamp"
        Write-Host "  backing up existing -> $bak" -ForegroundColor DarkYellow
        Move-Item -LiteralPath $path -Destination $bak
    }
}

function Install-Item([string]$from, [string]$to, [bool]$isDir) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
    Backup-IfExists $to
    if ($Link) {
        $kind = if ($isDir) { 'SymbolicLink' } else { 'HardLink' }
        New-Item -ItemType $kind -Path $to -Target $from | Out-Null
        Write-Host "  linked  $to" -ForegroundColor Green
    } else {
        if ($isDir) { Copy-Item -Recurse -LiteralPath $from -Destination $to }
        else        { Copy-Item -LiteralPath $from -Destination $to }
        Write-Host "  copied  $to" -ForegroundColor Green
    }
}

Write-Host "Installing into $ClaudeHome" -ForegroundColor Cyan

# --- tracker ---
Install-Item (Join-Path $src 'tracker\tracker.py')                (Join-Path $ClaudeHome 'revit-tracker\tracker.py')                $false
Install-Item (Join-Path $src 'tracker\hook_session_reminder.py')  (Join-Path $ClaudeHome 'revit-tracker\hook_session_reminder.py')  $false

# --- skill ---
Install-Item (Join-Path $src 'claude\skills\revit-session') (Join-Path $ClaudeHome 'skills\revit-session') $true

# --- agents ---
Install-Item (Join-Path $src 'claude\agents\revit-historian.md') (Join-Path $ClaudeHome 'agents\revit-historian.md') $false
Install-Item (Join-Path $src 'claude\agents\revit-scribe.md')    (Join-Path $ClaudeHome 'agents\revit-scribe.md')    $false

# --- lessons (do not clobber) ---
$rulesTo = Join-Path $ClaudeHome 'revit-lessons\RULES.md'
if ((Test-Path $rulesTo) -and -not $Force) {
    Write-Host "  keeping existing revit-lessons\RULES.md (pass -Force to replace)" -ForegroundColor DarkYellow
} else {
    Install-Item (Join-Path $src 'claude\revit-lessons\RULES.md') $rulesTo $false
}

# --- substitute the tracking-data path ---
if ($TrackingDataDir) {
    if (-not [System.IO.Path]::IsPathRooted($TrackingDataDir)) {
        throw "TrackingDataDir must be an absolute path: $TrackingDataDir"
    }
    if ($Link) {
        Write-Warning "-Link was used; skipping path substitution so the repo files are not edited. Set <TRACKING_DATA_DIR> manually or re-run without -Link."
    } else {
        foreach ($rel in @('skills\revit-session\SKILL.md', 'agents\revit-historian.md')) {
            $f = Join-Path $ClaudeHome $rel
            (Get-Content -LiteralPath $f -Raw).Replace('<TRACKING_DATA_DIR>', $TrackingDataDir) |
                Set-Content -LiteralPath $f -Encoding utf8
            Write-Host "  set tracking dir in $rel" -ForegroundColor Green
        }
    }
}

Write-Host ""
Write-Host "Done. One manual step left:" -ForegroundColor Cyan
Write-Host "  Merge the 'hooks' block from memory-system/claude/settings.example.json"
Write-Host "  into $ClaudeHome\settings.json (fix the path to hook_session_reminder.py)."
Write-Host ""
Write-Host "Then install the MCP server (mcp-server/README.md) and add it to your MCP client."
