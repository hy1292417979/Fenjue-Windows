param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & (Join-Path $PSScriptRoot 'Install-Fenjue.ps1') -Backend Cockpit
}
$runtimePath = Join-Path $projectRoot 'runtime/cockpit'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $runtimePath /inheritance:r /grant:r "${identityName}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot secure local runtime directory.' }
function Test-CockpitWorkbench {
    try {
        $result = Invoke-RestMethod 'http://127.0.0.1:17877/health' -TimeoutSec 2
        return ($result.service -eq 'fenjue-cockpit')
    } catch { return $false }
}
if (-not (Test-CockpitWorkbench)) {
    if (Get-NetTCPConnection -LocalPort 17877 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 17877 is occupied. Stop the old workbench before starting Cockpit mode.'
    }
    $process = Start-Process -FilePath $pythonExe -ArgumentList @('-X','utf8','-m','fenjue.cockpit') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'stdout.log') -RedirectStandardError (Join-Path $runtimePath 'stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $runtimePath 'service.pid')
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-CockpitWorkbench) { $ready = $true; break }
        $process.Refresh()
        if ($process.HasExited) { throw 'Cockpit workbench exited. Check that Cockpit local access is enabled; see runtime/cockpit/stderr.log.' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Startup timed out. See runtime/cockpit/stderr.log.' }
}
Write-Output 'Fenjue Cockpit: http://127.0.0.1:17877'
Write-Output 'Connect by selecting runtime/cockpit/gateway-key.local.json in the page.'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:17877' }
