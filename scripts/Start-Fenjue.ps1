param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
$runtimePath = Join-Path $projectRoot 'runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $runtimePath /inheritance:r /grant:r "${currentIdentity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
function Test-Fenjue {
    try {
        $result = Invoke-RestMethod 'http://127.0.0.1:17877/health' -TimeoutSec 2
        return ($result.service -eq 'fenjue' -and $result.version -eq '0.2.0')
    } catch { return $false }
}
if (-not (Test-Fenjue)) {
    if (Get-NetTCPConnection -LocalPort 17877 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 17877 is occupied by another service.'
    }
    $process = Start-Process -FilePath $pythonExe -ArgumentList @('-X','utf8','-m','fenjue.service') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath 'server.stdout.log') -RedirectStandardError (Join-Path $runtimePath 'server.stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $runtimePath 'service.pid')
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-Fenjue) { $ready = $true; break }
        if ($process.HasExited) { throw 'Fenjue exited; inspect runtime/server.stderr.log.' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Fenjue readiness timed out; inspect runtime/server.stderr.log.' }
}
Write-Output 'Fenjue is running: http://127.0.0.1:17877'
Write-Output 'Select runtime/gateway-key.local.json in the workbench to connect.'
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:17877' }
