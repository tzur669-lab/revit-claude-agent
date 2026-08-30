<#
.SYNOPSIS
    Install the memory-system pieces into your Claude Code home (~/.claude).

.DESCRIPTION
    Copies (or symlinks, with -Link) the tracker, the revit-session skill, and
    the two agents into ~/.claude. Does NOT edit ~/.claude/settings.json - it
    prints the hook block for you to merge by hand. Does NOT touch mcp-server/
    unless -LinkExtension is passed (see below) - install it separately by
    default, per its own README.

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

.PARAMETER LinkExtension
    Development convenience, NOT the documented install path: replace the
    pyRevit extension folder with a directory junction into this repo's
    mcp-server/, so an edit here is what Revit runs immediately - no copy
    step, ever. A junction (not a symlink) needs no elevation and no
    Developer Mode. Independent of -Link, which only covers the memory-system
    files under -ClaudeHome.

    Whatever branch is checked out becomes what Revit executes, including a
    dirty working tree - acceptable for one developer with Revit open beside
    them, not something to rely on for a real install. The ordinary copy
    install (this script's default, with -LinkExtension omitted) is what the
    README documents and what every change in this repo must keep working
    under. After switching branches, pyRevit's extension cache may need
    clearing (pyRevit tab > Settings > Clear Cache) before the change is
    picked up.

    Backs up any existing extension folder at *.bak-<timestamp> first, same
    as every other install step here - it is never deleted outright.

.PARAMETER PyRevitExtensionsDir
    Where pyRevit looks for extensions. Default: %APPDATA%\pyRevit\Extensions.
    Only used when -LinkExtension is passed.

.EXAMPLE
    ./scripts/install.ps1 -TrackingDataDir 'C:\Users\me\revit-tracking'

.EXAMPLE
    ./scripts/install.ps1 -Link -LinkExtension
    # Development setup: everything under ~/.claude AND the pyRevit
    # extension folder point straight into this checkout.
#>
[CmdletBinding()]
param(
    [string]$TrackingDataDir,
    [string]$ClaudeHome = (Join-Path $HOME '.claude'),
    [switch]$Link,
    [switch]$Force,
    [switch]$LinkExtension,
    [string]$PyRevitExtensionsDir = (Join-Path $env:APPDATA 'pyRevit\Extensions')
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

# --- pyRevit extension, dev-only opt-in ---
if ($LinkExtension) {
    $extPath = Join-Path $PyRevitExtensionsDir 'mcp-server-for-revit-python.extension'
    $repoMcp = Join-Path $repo 'mcp-server'
    Write-Host ""
    Write-Host "Linking pyRevit extension (dev mode, not the documented install path)" -ForegroundColor Cyan
    Backup-IfExists $extPath
    New-Item -ItemType Junction -Path $extPath -Target $repoMcp | Out-Null
    Write-Host "  junction  $extPath -> $repoMcp" -ForegroundColor Green
    Write-Host "  Restart Revit (or Clear Cache under pyRevit > Settings) to pick it up." -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "Done. One manual step left:" -ForegroundColor Cyan
Write-Host "  Merge the 'hooks' block from memory-system/claude/settings.example.json"
Write-Host "  into $ClaudeHome\settings.json (fix the path to hook_session_reminder.py)."
Write-Host ""
if ($LinkExtension) {
    Write-Host "The MCP server is linked - add mcp-server/ to your MCP client (see its README)."
} else {
    Write-Host "Then install the MCP server (mcp-server/README.md) and add it to your MCP client."
}
