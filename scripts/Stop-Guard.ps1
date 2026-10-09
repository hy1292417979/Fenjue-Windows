$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
Push-Location $projectRoot
$guardMutex = New-Object System.Threading.Mutex($false, 'Local\FenjueGuardSwitch')
$guardLock = $false
try {
    try { $guardLock = $guardMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $guardLock = $true }
    if (-not $guardLock) { throw 'Another Fenjue start/stop operation is running.' }
    & $pythonExe -X utf8 -m fenjue.outbound_control disable
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit release/drain not confirmed; guard left running.' }
    $pidFile = Join-Path $projectRoot 'runtime/guard/service.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded guard process.'; return }
    $servicePid = [int](Get-Content -LiteralPath $pidFile)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $servicePid"
    $expected = $pythonExe.Replace('/','\')
    if ($process -and $process.CommandLine -match '-m\s+fenjue\.cockpit_guard(?:\s|$)' -and $process.ExecutablePath -eq $expected) {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $servicePid")
        foreach ($child in $children) {
            if ($child.CommandLine -match '-m\s+fenjue\.cockpit_guard(?:\s|$)' -and $child.ExecutablePath -match '\\python.exe$') {
                Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
            }
        }
        Stop-Process -Id $servicePid -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $pidFile
    } elseif ($process) { throw 'PID belongs to another process; no process stopped.' }
    else { Remove-Item -LiteralPath $pidFile }
    Write-Output 'Fenjue OFF: Cockpit normal outbound restored. Codex address unchanged.'
} finally {
    if ($guardLock) { $guardMutex.ReleaseMutex() }
    $guardMutex.Dispose()
    Pop-Location
}
