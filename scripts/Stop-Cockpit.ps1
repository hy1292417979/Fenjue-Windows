$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'runtime/cockpit/service.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded Cockpit workbench process.'; exit }
$servicePid = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
$expected = (Join-Path $projectRoot '.venv/Scripts/python.exe').Replace('/','\')
if ($process -and $process.CommandLine -match '-m\s+fenjue\.cockpit(?:\s|$)' -and $process.ExecutablePath -eq $expected) {
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $servicePid")
    foreach ($child in $children) {
        if ($child.CommandLine -match '-m\s+fenjue\.cockpit(?:\s|$)' -and $child.ExecutablePath -match '\\python.exe$') {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $servicePid -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile
    Write-Output 'Workbench stopped. Cockpit remains running.'
} elseif ($process) { throw 'PID belongs to another process; no process stopped.' }
else { Remove-Item -LiteralPath $pidFile; Write-Output 'Workbench was already stopped.' }
