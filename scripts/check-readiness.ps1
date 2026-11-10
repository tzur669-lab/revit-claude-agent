<#
.SYNOPSIS
    Read-only readiness check before install.ps1 or any live Revit test.

.DESCRIPTION
    Prints a PASS/FAIL/INFO table of what this machine has: Revit, pyRevit,
    the Routes port, the MCP server registration, Python + the `mcp` package,
    and the memory-system pieces under ~/.claude. Changes nothing.

    Exit code 0 = every REQUIRED row passed; 1 = at least one failed.
    Never prints environment values or MCP server env/args (they can hold
    secrets) - only paths and yes/no.

.PARAMETER ClaudeHome
    Default: ~/.claude

.PARAMETER Port
    pyRevit Routes port. Default: $env:REVIT_PORT, else 48884.
#>
[CmdletBinding()]
param(
    [string]$ClaudeHome = (Join-Path $HOME '.claude'),
    [int]$Port = $(if ($env:REVIT_PORT) { [int]$env:REVIT_PORT } else { 48884 })
)

$ErrorActionPreference = 'Continue'
$rows = New-Object System.Collections.Generic.List[object]

function Add-Row($name, $status, $detail, [bool]$required) {
    $rows.Add([pscustomobject]@{
        Check    = $name
        Status   = $status
        Required = $(if ($required) { 'yes' } else { 'no' })
        Detail   = $detail
    })
}

# --- Revit -----------------------------------------------------------------
$revitDirs = @(Get-ChildItem 'C:\Program Files\Autodesk' -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^Revit \d{4}$' } | ForEach-Object { $_.FullName })
if ($revitDirs.Count -gt 0) {
    Add-Row 'Revit installed' 'PASS' ($revitDirs -join '; ') $true
} else {
    Add-Row 'Revit installed' 'FAIL' 'no C:\Program Files\Autodesk\Revit <year>' $true
}

# --- pyRevit ---------------------------------------------------------------
$pyrevitHits = @()
foreach ($p in @(
        (Join-Path $env:APPDATA 'pyRevit-Master'),
        (Join-Path $env:APPDATA 'pyRevit'),
        (Join-Path $env:PROGRAMDATA 'pyRevit'),
        'C:\Program Files\pyRevit-Master',
        'C:\Program Files\pyRevit CLI')) {
    if ($p -and (Test-Path -LiteralPath $p)) { $pyrevitHits += $p }
}
$cli = Get-Command pyrevit -ErrorAction SilentlyContinue
if ($cli) { $pyrevitHits += "cli: $($cli.Source)" }
if ($pyrevitHits.Count -gt 0) {
    Add-Row 'pyRevit installed' 'PASS' ($pyrevitHits -join '; ') $true
} else {
    Add-Row 'pyRevit installed' 'FAIL' 'no pyRevit folder and no pyrevit CLI on PATH' $true
}

# --- Routes port (only answers while Revit is open with Routes enabled) -----
$open = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect('127.0.0.1', $Port, $null, $null)
    $open = $iar.AsyncWaitHandle.WaitOne(1500) -and $tcp.Connected
    $tcp.Close()
} catch { $open = $false }
if ($open) {
    Add-Row "Routes listening :$Port" 'PASS' 'Revit open with pyRevit Routes' $false
} else {
    Add-Row "Routes listening :$Port" 'INFO' 'nothing listening - open Revit and enable Routes before live checks' $false
}

# --- Extension registered with pyRevit --------------------------------------
$extDir = Join-Path $env:APPDATA 'pyRevit\Extensions\mcp-server-for-revit-python.extension'
$cfg    = Join-Path $env:APPDATA 'pyRevit\pyRevit_config.ini'
$extFound = (Test-Path -LiteralPath $extDir)
if (-not $extFound -and (Test-Path -LiteralPath $cfg)) {
    $extFound = [bool](Select-String -LiteralPath $cfg -Pattern 'revit-claude-agent|mcp-server' -Quiet)
}
if ($extFound) {
    Add-Row 'pyRevit extension registered' 'PASS' 'extension folder or config entry found' $true
} else {
    Add-Row 'pyRevit extension registered' 'FAIL' 'not in pyRevit\Extensions and not in pyRevit_config.ini' $true
}

# --- MCP server registration in Claude Code --------------------------------
$mcpDetail = $null
$claudeJson = Join-Path $HOME '.claude.json'
if (Test-Path -LiteralPath $claudeJson) {
    try {
        $j = Get-Content -LiteralPath $claudeJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $found = @()
        if ($j.mcpServers) {
            foreach ($n in $j.mcpServers.PSObject.Properties.Name) { if ($n -match 'revit') { $found += "user:$n" } }
        }
        if ($j.projects) {
            foreach ($proj in $j.projects.PSObject.Properties) {
                $ms = $proj.Value.mcpServers
                if ($ms) {
                    foreach ($n in $ms.PSObject.Properties.Name) { if ($n -match 'revit') { $found += "project:$n" } }
                }
            }
        }
        if ($found.Count -gt 0) { $mcpDetail = ($found | Select-Object -Unique) -join '; ' }
    } catch {
        $mcpDetail = $null
    }
}
foreach ($mcpFile in @((Join-Path (Split-Path $PSScriptRoot -Parent) '.mcp.json'))) {
    if (-not $mcpDetail -and (Test-Path -LiteralPath $mcpFile)) {
        if (Select-String -LiteralPath $mcpFile -Pattern 'revit' -Quiet) { $mcpDetail = ".mcp.json in repo" }
    }
}
if ($mcpDetail) {
    Add-Row 'MCP server registered' 'PASS' $mcpDetail $true
} else {
    Add-Row 'MCP server registered' 'FAIL' 'no server named *revit* in ~/.claude.json or repo .mcp.json' $true
}

# --- Python + mcp package ---------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $ver = (& python --version 2>&1) -join ' '
    Add-Row 'Python' 'PASS' "$ver ($($py.Source))" $true
    & python -c "import mcp" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Add-Row 'Python package mcp' 'PASS' 'importable' $true
    } else {
        Add-Row 'Python package mcp' 'FAIL' 'not importable (needed by the MCP server)' $true
    }
} else {
    Add-Row 'Python' 'FAIL' 'python not on PATH' $true
}

# --- Memory system under ~/.claude (informational: install.ps1 creates it) ---
$items = [ordered]@{
    'skill revit-session'     = 'skills\revit-session\SKILL.md'
    'agent revit-historian'   = 'agents\revit-historian.md'
    'agent revit-scribe'      = 'agents\revit-scribe.md'
    'tracker.py'              = 'revit-tracker\tracker.py'
    'hook script'             = 'revit-tracker\hook_session_reminder.py'
    'RULES.md'                = 'revit-lessons\RULES.md'
    'PREFERENCES.md'          = 'revit-lessons\PREFERENCES.md'
    'design rules'            = 'revit-design-rules'
}
foreach ($k in $items.Keys) {
    $p = Join-Path $ClaudeHome $items[$k]
    if (Test-Path -LiteralPath $p) { Add-Row "~/.claude $k" 'PASS' $p $false }
    else                           { Add-Row "~/.claude $k" 'INFO' 'not installed' $false }
}

$dupes = @(Get-ChildItem (Join-Path $ClaudeHome 'skills') -Directory -Filter 'revit-session.bak-*' -ErrorAction SilentlyContinue)
if ($dupes.Count -gt 0) {
    Add-Row 'no duplicate skill backups' 'FAIL' "move to ~/.claude/revit-backups/: $($dupes.Name -join ', ')" $false
}

$settings = Join-Path $ClaudeHome 'settings.json'
if ((Test-Path -LiteralPath $settings) -and (Select-String -LiteralPath $settings -Pattern 'hook_session_reminder' -Quiet)) {
    Add-Row 'hook in settings.json' 'PASS' 'hook_session_reminder referenced' $false
} else {
    Add-Row 'hook in settings.json' 'INFO' 'not merged yet (install.ps1 prints the block)' $false
}

$skillMd = Join-Path $ClaudeHome 'skills\revit-session\SKILL.md'
if ((Test-Path -LiteralPath $skillMd) -and (Select-String -LiteralPath $skillMd -Pattern '<TRACKING_DATA_DIR>' -SimpleMatch -Quiet)) {
    Add-Row 'TrackingDataDir substituted' 'FAIL' 'placeholder still in installed SKILL.md - rerun install.ps1 -TrackingDataDir' $false
}

# --- Report -----------------------------------------------------------------
$rows | Format-Table -AutoSize -Wrap | Out-String -Width 200 | Write-Host
$failedRequired = @($rows | Where-Object { $_.Required -eq 'yes' -and $_.Status -eq 'FAIL' })
if ($failedRequired.Count -gt 0) {
    Write-Host "NOT READY: $($failedRequired.Count) required check(s) failed. Do not run install.ps1 or live tests yet." -ForegroundColor Red
    exit 1
}
Write-Host 'READY: all required checks passed.' -ForegroundColor Green
exit 0
