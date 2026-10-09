$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $projectRoot 'runtime/service.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded Fenjue process.'; exit }
$servicePid = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
if ($process -and $process.CommandLine -match '-m fenjue.service' -and $process.ExecutablePath -eq (Join-Path $projectRoot '.venv/Scripts/python.exe').Replace('/','\')) {
    # Windows venv python.exe launches a child interpreter which owns the socket.
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $servicePid")
    foreach ($child in $children) {
        if ($child.CommandLine -match '-m fenjue.service' -and $child.ExecutablePath -match '\\python.exe$') {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $servicePid -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile
    Write-Output 'Fenjue stopped. Sub2API remains running.'
} elseif ($process) { throw 'Recorded PID belongs to another process; no process stopped.' }
else { Remove-Item -LiteralPath $pidFile; Write-Output 'Fenjue was already stopped.' }
