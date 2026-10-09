$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
Push-Location $projectRoot
try {
    & $pythonExe -X utf8 -m fenjue.outbound_control status
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit takeover status unavailable; do not assume protection is active.' }
    try {
        $keyPath = Join-Path $projectRoot 'runtime/guard/gateway-key.local.json'
        $guardKey = (Get-Content -LiteralPath $keyPath -Raw | ConvertFrom-Json).api_key
        Invoke-RestMethod 'http://127.0.0.1:17878/api/status' -Headers @{Authorization="Bearer $guardKey"} -TimeoutSec 5 |
            Select-Object service,account_ref,last_call,outbound_acceptance,quality_gain_verified | Format-List
    } catch { Write-Output 'Guard service is not reachable. If takeover is ON, requests will fail closed.' }
} finally { Pop-Location }
