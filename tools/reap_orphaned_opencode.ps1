<#
.SYNOPSIS
    Reap orphaned opencode CLI servers, which releases their MCP child processes.

.DESCRIPTION
    opencode's desktop app spawns `opencode-cli.exe serve --service` (a Bun
    single-file binary) as its server, and that server spawns one set of local
    MCP servers per booted location. Closing the app window does NOT stop the
    server process, so neither the server nor any MCP process is released. They
    accumulate for as long as the app stays open.

    The leak is upstream (opencode never disposes a location's connections), so
    this is a workaround, not a fix. It removes the residue of a run that has
    already ended:

        app dies -> server process is orphaned -> reaper kills the server
                 -> every MCP child sees stdin EOF -> each exits cleanly

    Only the orphaned server process is killed. Its MCP children are never
    signalled directly: they exit by themselves on EOF, and killing a launcher
    directly would orphan its interpreter child instead.

    Safety rules:
      * only processes whose command line matches `-CommandLineMatch`
        (default "serve --service") -- a hand-started `opencode serve` is left alone
      * only when the parent PID is no longer a live process
      * only after `-GraceSeconds` of continuous orphanhood
      * never this process

    Orphan detection on Windows: a child keeps ParentProcessId pointing at the
    now-dead PID rather than being reparented, so "parent PID is not live" is a
    reliable test.

.PARAMETER PollSeconds
    Seconds between passes.

.PARAMETER GraceSeconds
    How long a server must stay orphaned before it is reaped. Guards against a
    fast app restart and against parent-PID reuse.

.PARAMETER ProcessName
    Process image to inspect. Overridable so the logic can be tested against a
    harmless stand-in.

.PARAMETER CommandLineMatch
    Substring that must appear in the command line. Default targets the
    desktop-managed background server only.

.PARAMETER ChildNamePattern
    Used only to count MCP processes before and after a reap, so the log shows
    whether the EOF cascade actually released them.

.PARAMETER Once
    Run a single pass and exit. For testing.

.PARAMETER DryRun
    Log every decision but kill nothing.

.PARAMETER LogPath
    Append-only log file.

.EXAMPLE
    .\reap_orphaned_opencode.ps1 -Once -DryRun

.EXAMPLE
    pwsh -NoProfile -WindowStyle Hidden -File .\reap_orphaned_opencode.ps1

.NOTES
    Register at logon (run once, elevated not required). The task needs an
    absolute path, so substitute this repo's checkout path for <repo>:

        $a = New-ScheduledTaskAction -Execute pwsh -Argument `
            "-NoProfile -WindowStyle Hidden -File `"<repo>\tools\reap_orphaned_opencode.ps1`""
        $t = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName 'OpenCode-MCP-Reaper' -Action $a -Trigger $t `
            -Description 'Reap orphaned opencode servers so MCP processes are released'

    Remove it again:

        Unregister-ScheduledTask -TaskName 'OpenCode-MCP-Reaper' -Confirm:$false
#>
[CmdletBinding()]
param(
    [int]    $PollSeconds      = 5,
    [int]    $GraceSeconds     = 30,
    [string] $ProcessName      = 'opencode-cli.exe',
    [string] $CommandLineMatch = 'serve --service',
    [string] $ChildNamePattern = 'mcp-agent-*.exe',
    [switch] $Once,
    [switch] $DryRun,
    [string] $LogPath          = "$env:LOCALAPPDATA\opencode-mcp-reaper\reaper.log"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding utf8
    } catch {
        # logging must never take the watcher down
    }
    Write-Host $line
}

function Get-MatchingServers {
    param([string]$Name, [string]$Match)
    $pattern = "*$Match*"
    Get-CimInstance Win32_Process -Filter "Name='$Name'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like $pattern }
}

function Test-PidAlive {
    param([int]$Id)
    if ($Id -le 0) { return $false }
    [bool](Get-Process -Id $Id -ErrorAction SilentlyContinue)
}

function Get-ChildCount {
    param([string]$Pattern)
    # Get-Process -Name takes no wildcards, so filter on Name ourselves.
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like $Pattern }).Count
}

# serverPid -> record of when it was first seen orphaned
$state = @{}

function Invoke-Pass {
    $now = Get-Date
    $servers = @(Get-MatchingServers -Name $ProcessName -Match $CommandLineMatch)

    # forget servers that are gone
    # Project .ProcessId off the elements, never off the array itself: under
    # Set-StrictMode -Version Latest, member enumeration on an EMPTY array
    # throws "The property 'ProcessId' cannot be found on this object"
    # instead of yielding $null. That aborted every pass while no server was
    # running and a stale $state entry existed.
    $livePids = @($servers | ForEach-Object { $_.ProcessId })
    foreach ($pidKey in @($state.Keys)) {
        if ($livePids -notcontains $pidKey) { $state.Remove($pidKey) }
    }

    foreach ($s in $servers) {
        $pidKey = [int]$s.ProcessId
        $appPid = [int]$s.ParentProcessId

        if ($pidKey -eq $PID) { continue }

        if (Test-PidAlive -Id $appPid) {
            # healthy: app present
            if ($state.ContainsKey($pidKey)) { $state.Remove($pidKey) }
            continue
        }

        if (-not $state.ContainsKey($pidKey)) {
            $state[$pidKey] = [pscustomobject]@{ FirstSeen = $now; AppPid = $appPid }
            Write-Log 'ORPHAN' "server pid=$pidKey app pid=$appPid is gone, starting $GraceSeconds s grace"
            continue
        }

        $age = ($now - $state[$pidKey].FirstSeen).TotalSeconds
        if ($age -lt $GraceSeconds) { continue }

        $before = Get-ChildCount -Pattern $ChildNamePattern
        if ($DryRun) {
            Write-Log 'DRYRUN' "would kill server pid=$pidKey (orphaned $([int]$age) s, app pid=$($state[$pidKey].AppPid)), $before child processes alive"
            $state.Remove($pidKey)
            continue
        }

        Write-Log 'REAP' "killing orphaned server pid=$pidKey (orphaned $([int]$age) s, app pid=$($state[$pidKey].AppPid)), $before child processes alive"
        try {
            Stop-Process -Id $pidKey -Force -ErrorAction Stop
            Start-Sleep -Seconds 2
            $after = Get-ChildCount -Pattern $ChildNamePattern
            Write-Log 'REAPED' "server pid=$pidKey gone; child processes $before -> $after"
        } catch {
            Write-Log 'ERROR' "failed to kill server pid=$pidKey : $($_.Exception.Message)"
        }
        $state.Remove($pidKey)
    }
}

Write-Log 'START' ("watching {0} for '{1}'; poll {2}s grace {3}s dryrun={4}" -f $ProcessName, $CommandLineMatch, $PollSeconds, $GraceSeconds, [bool]$DryRun)

if ($Once) {
    Invoke-Pass
    Write-Log 'STOP' 'single pass done'
    return
}

while ($true) {
    try {
        Invoke-Pass
    } catch {
        Write-Log 'ERROR' "pass failed, continuing: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $PollSeconds
}