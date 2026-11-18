#Requires -Version 5.1
<#
.SYNOPSIS
    Install vpcd_bridge.py as an elevated, auto-starting scheduled task.

.DESCRIPTION
    Interactive installer. It finds the Python interpreters on this machine,
    asks which one to use, copies the bridge to an data directory
    (%XDG_DATA_HOME%/vpcd-bridge, default %USERPROFILE%\.local\share\vpcd-bridge)
    and registers a scheduled task that runs it with administrator rights at
    logon.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup_vpcd_bridge.ps1

.EXAMPLE
    .\setup_vpcd_bridge.ps1 -Remove

.NOTES
    Any value that is passed as a parameter is not asked for. -Yes only skips
    the final confirmation, -PauseAtEnd is used internally after elevation.
#>
[CmdletBinding()]
param(
    [string]$Bridge,                            # source vpcd_bridge.py (default: next to this script)
    [string]$Python,                            # interpreter; asked for when omitted
    [string]$DataDir,                           # install dir; asked for when omitted
    [string]$Reader,                            # --reader value for the bridge ("" = any reader)
    [string]$HostAddress = '127.0.0.1',         # --host
    [int]$Port = 35963,                         # --port
    [string]$TaskName = 'vpcd-bridge',
    [switch]$Remove,                            # uninstall: stop, unregister, optionally delete files
    [switch]$Yes,                               # skip the final "proceed?" question
    [switch]$NoStart,                           # register but do not start now
    [switch]$PauseAtEnd                         # keep the elevated window open at the end
)

$ErrorActionPreference = 'Stop'

function Fail([string]$message) {
    Write-Host "error: $message" -ForegroundColor Red
    if ($PauseAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

function Get-DataHome {
    if ($env:XDG_DATA_HOME) { return $env:XDG_DATA_HOME }
    return (Join-Path $env:USERPROFILE '.local\share')
}

# --- elevation ---------------------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    $forward = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-PauseAtEnd')
    foreach ($key in $PSBoundParameters.Keys) {
        if ($key -eq 'PauseAtEnd') { continue }
        $value = $PSBoundParameters[$key]
        if ($value -is [switch]) {
            if ($value.IsPresent) { $forward += "-$key" }
        } else {
            $forward += "-$key"
            $forward += "`"$value`""
        }
    }
    Write-Host 'Administrator rights are required; opening an elevated window...' -ForegroundColor Yellow
    Start-Process -FilePath (Get-Process -Id $PID).Path -Verb RunAs -ArgumentList $forward
    return
}

# --- uninstall ---------------------------------------------------------------
if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "task '$TaskName' removed" -ForegroundColor Green
    } else {
        Write-Host "task '$TaskName' does not exist" -ForegroundColor DarkGray
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*vpcd_bridge.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    $dir = if ($DataDir) { $DataDir } else { Join-Path (Get-DataHome) 'vpcd-bridge' }
    if (Test-Path -LiteralPath $dir) {
        $answer = Read-Host "Delete '$dir'? [y/N]"
        if ($answer -match '^[Yy]') { Remove-Item -LiteralPath $dir -Recurse -Force; Write-Host 'files deleted' }
        else { Write-Host "files kept in $dir" }
    }
    if ($PauseAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
    return
}

# --- pick a Python -----------------------------------------------------------
function Find-Pythons {
    $found = [System.Collections.Generic.List[string]]::new()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $path = & py -3 -c 'import sys;print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) { $found.Add($path.Trim()) }
    }
    foreach ($name in @('python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and $cmd.Source -notmatch '\\WindowsApps\\') { $found.Add($cmd.Source) }
    }
    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python3*\python.exe'),
        'C:\Python3*\python.exe'
    )
    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
            ForEach-Object { $found.Add($_.FullName) }
    }
    return @($found | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
}

function Test-Interpreter([string]$path) {
    $resolved = (Resolve-Path -LiteralPath $path -ErrorAction SilentlyContinue).Path
    if (-not $resolved -or -not (Test-Path -LiteralPath $resolved)) { return $null }
    $ver = & $resolved -c "import sys;print('.'.join(map(str,sys.version_info[:2])))" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $ver) { return $null }
    if ([version]$ver -lt [version]'3.10') { return $null }
    return $resolved
}

if ($Python) {
    $requested = $Python
    $Python = Test-Interpreter $requested
    if (-not $Python) { Fail "not a Python 3.10+ interpreter: $requested" }
} else {
    $candidates = @(Find-Pythons)
    if ($candidates.Count -gt 0) {
        Write-Host 'Python interpreters found:' -ForegroundColor Cyan
        for ($i = 0; $i -lt $candidates.Count; $i++) {
            $ver = & $candidates[$i] -c "import sys;print('.'.join(map(str,sys.version_info[:3])))" 2>$null
            Write-Host ('  [{0}] {1}  ({2})' -f ($i + 1), $candidates[$i], $ver)
        }
    } else {
        Write-Host 'No Python interpreter found automatically.' -ForegroundColor Yellow
    }

    while (-not $Python) {
        if ($candidates.Count -gt 0) {
            $answer = Read-Host "Use which one? [1-$($candidates.Count)], a full path, or blank to type a path"
        } else {
            $answer = Read-Host 'Full path to python.exe (blank = abort)'
        }
        if (-not $answer) {
            if ($candidates.Count -eq 0) { Fail 'aborted' }
            $answer = '1'
        }
        if ($candidates.Count -gt 0 -and $answer -match '^\d+$') {
            $index = [int]$answer - 1
            if ($index -lt 0 -or $index -ge $candidates.Count) {
                Write-Host "no such entry: $answer" -ForegroundColor Yellow
                continue
            }
            $answer = $candidates[$index]
        }
        $resolved = Test-Interpreter $answer
        if ($resolved) { $Python = $resolved }
        else { Write-Host "not a usable Python 3.10+: $answer" -ForegroundColor Yellow }
    }
}
$pyVersion = & $Python -c "import sys;print('.'.join(map(str,sys.version_info[:2])))"
Write-Host "Python: $Python ($pyVersion)" -ForegroundColor Green

# --- pyscard -----------------------------------------------------------------
& $Python -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'pip missing, running ensurepip' -ForegroundColor DarkGray
    & $Python -m ensurepip --upgrade | Out-Null
}
& $Python -m pip install --upgrade --user --quiet pyscard
if ($LASTEXITCODE -ne 0) { Fail 'pip install pyscard failed' }
& $Python -c 'import smartcard.System' 2>$null
if ($LASTEXITCODE -ne 0) { Fail "pyscard cannot be imported by $Python" }
Write-Host 'pyscard: ok' -ForegroundColor Green

# --- source file -------------------------------------------------------------
if (-not $Bridge) { $Bridge = Join-Path $PSScriptRoot 'vpcd_bridge.py' }
$Bridge = (Resolve-Path -LiteralPath $Bridge -ErrorAction SilentlyContinue).Path
if (-not $Bridge -or -not (Test-Path -LiteralPath $Bridge)) { Fail 'vpcd_bridge.py not found; pass -Bridge <path>' }

# --- install dir -------------------------------------------------------------
if (-not $DataDir) {
    $suggested = Join-Path (Get-DataHome) 'vpcd-bridge'
    $answer = Read-Host "Install directory (Enter = $suggested)"
    $DataDir = if ($answer) { $answer } else { $suggested }
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
$installed = Join-Path $DataDir 'vpcd_bridge.py'
$logFile = Join-Path $DataDir 'vpcd-bridge.log'

# --- reader ------------------------------------------------------------------
if (-not $PSBoundParameters.ContainsKey('Reader')) {
    $Reader = Read-Host 'Reader name substring (Enter = any physical reader)'
}

# --- summary -----------------------------------------------------------------
$bridgeArgs = @('-u', $installed, '--log-file', $logFile, '--host', $HostAddress, '--port', "$Port")
if ($Reader) { $bridgeArgs += @('--reader', $Reader) }

Write-Host ''
Write-Host 'Summary' -ForegroundColor Cyan
Write-Host "  Python   : $Python"
Write-Host "  Bridge   : $Bridge  ->  $installed"
Write-Host "  Data dir : $DataDir"
Write-Host "  Log      : $logFile"
Write-Host "  Task     : $TaskName (logon, highest privileges)"
if (-not $Yes) {
    $proceed = Read-Host 'Proceed? [Y/n]'
    if ($proceed -and $proceed -notmatch '^[Yy]') {
        Write-Host 'aborted'
        if ($PauseAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
        return
    }
}

# --- install -----------------------------------------------------------------
$null = New-Item -ItemType Directory -Force -Path $DataDir
if ($Bridge -ne $installed) { Copy-Item -LiteralPath $Bridge -Destination $installed -Force }
Write-Host "copied bridge to $installed" -ForegroundColor Green
# leftovers from an older install format
Remove-Item -LiteralPath (Join-Path $DataDir 'run_vpcd_bridge.cmd') -Force -ErrorAction SilentlyContinue

# --- replace the task --------------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}
# a stopped task can leave its python child behind; kill only bridges
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*vpcd_bridge.py*' } |
    ForEach-Object {
        Write-Host "  stopping leftover bridge $($_.ProcessId)" -ForegroundColor DarkGray
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Milliseconds 300

$quoted = ($bridgeArgs | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ' '
$inner = "& '{0}' {1}" -f ($Python -replace "'", "''"), $quoted
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$inner`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principalDef = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principalDef -Settings $settings -Force `
    -Description 'Relay a PC/SC smart card into vpcd (runs with administrator rights).' | Out-Null
Write-Host "task '$TaskName' registered" -ForegroundColor Green

# --- start and check ---------------------------------------------------------
if ($NoStart) {
    Write-Host 'task not started (-NoStart)' -ForegroundColor DarkGray
} else {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5
    $task = Get-ScheduledTask -TaskName $TaskName
    $info = $task | Get-ScheduledTaskInfo
    Write-Host ("state: {0}  last result: 0x{1:X8}" -f $task.State, $info.LastTaskResult)
    if (Test-Path -LiteralPath $logFile) {
        Write-Host 'log tail:' -ForegroundColor DarkGray
        Get-Content -LiteralPath $logFile -Tail 6 | ForEach-Object { Write-Host "  $_" }
    }
}

Write-Host ''
Write-Host 'done' -ForegroundColor Green
Write-Host "  restart : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  stop    : Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "  remove  : .\setup_vpcd_bridge.ps1 -Remove"
if ($PauseAtEnd) { Read-Host 'Press Enter to close' | Out-Null }
