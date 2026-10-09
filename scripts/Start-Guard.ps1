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
        return ($result.service -eq 'fenjue-cockpit-guard' -and $result.version -eq '2.0')
    } catch { return $false }
}
Push-Location $projectRoot
try {
    if (-not (Test-FenjueGuard)) {
        if (Get-NetTCPConnection -LocalPort 17878 -State Listen -ErrorAction SilentlyContinue) {
            throw 'Port 17878 is occupied by another service or an older guard. Run Stop-Guard.cmd before starting the updated guard.'
        }
        & $pythonExe -X utf8 -m fenjue.cockpit_guard --initialize
        if ($LASTEXITCODE -ne 0) { throw 'Pinned Cockpit account could not be initialized.' }
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
    if ($EnableCodex) {
        & $pythonExe -X utf8 -m fenjue.guard_config enable
        if ($LASTEXITCODE -ne 0) { throw 'Codex configuration was not changed.' }
    }
    Write-Output 'Fenjue Codex guard: http://127.0.0.1:17878/v1'
    Write-Output 'Keep Cockpit running for login/refresh. Restart Codex after enabling.'
} finally { Pop-Location }
