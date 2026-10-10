param([switch]$EnableCodex)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & (Join-Path $PSScriptRoot 'Install-Fenjue.ps1') -Backend Cockpit
}
$runtimePath = Join-Path $projectRoot 'runtime/guard'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $runtimePath /inheritance:r /grant:r "${identityName}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot secure guard runtime directory.' }
function Test-FenjueGuard {
    try {
        $result = Invoke-RestMethod 'http://127.0.0.1:17878/health' -TimeoutSec 2
        return ($result.service -eq 'fenjue-cockpit-guard' -and $result.version -eq '3.0')
    } catch { return $false }
}
Push-Location $projectRoot
$guardMutex = New-Object System.Threading.Mutex($false, 'Local\FenjueGuardSwitch')
$guardLock = $false
try {
    try { $guardLock = $guardMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $guardLock = $true }
    if (-not $guardLock) { throw 'Another Fenjue start/stop operation is running.' }
    if (-not (Test-FenjueGuard)) {
        if (Get-NetTCPConnection -LocalPort 17878 -State Listen -ErrorAction SilentlyContinue) {
            throw 'Port 17878 is occupied by another service or an older guard. Run Stop-Guard.cmd before starting the updated guard.'
        }
        & $pythonExe -X utf8 -m fenjue.cockpit_guard --initialize
        if ($LASTEXITCODE -ne 0) { throw 'Cockpit OAuth account routing could not be initialized.' }
        $process = Start-Process -FilePath $pythonExe -ArgumentList @('-X','utf8','-m','fenjue.cockpit_guard') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'stdout.log') -RedirectStandardError (Join-Path $runtimePath 'stderr.log')
        $process.Id | Set-Content -LiteralPath (Join-Path $runtimePath 'service.pid')
        $ready = $false
        for ($i = 0; $i -lt 20; $i++) {
            if (Test-FenjueGuard) { $ready = $true; break }
            $process.Refresh()
            if ($process.HasExited) { throw 'Guard exited; inspect runtime/guard/stderr.log.' }
            Start-Sleep -Milliseconds 500
        }
        if (-not $ready) { throw 'Guard startup timed out.' }
    }
    # EnableCodex is retained for old launchers; all starts now enable outbound.
    & $pythonExe -X utf8 -m fenjue.outbound_control enable
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit takeover was not acknowledged. Guard left running for inspection.' }
    # One-time migration from the old frontend gateway, not a toggle operation.
    & $pythonExe -X utf8 -m fenjue.guard_config restore
    if ($LASTEXITCODE -ne 0) { throw 'Takeover active but old Codex URL migration needs attention.' }
    $codexRoute = & $pythonExe -X utf8 -m fenjue.guard_config status | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Takeover active but Codex provider status is unavailable.' }
    Write-Output 'Fenjue ON: Cockpit selected OAuth account -> Fenjue -> official upstream.'
    if (-not $codexRoute.codex_provider_is_cockpit) {
        Write-Output 'Codex is currently using another provider; its requests will not enter Cockpit/Fenjue.'
    }
    Write-Output 'After first migration restart Codex. Future toggles do not change its address.'
} finally {
    if ($guardLock) { $guardMutex.ReleaseMutex() }
    $guardMutex.Dispose()
    Pop-Location
}
