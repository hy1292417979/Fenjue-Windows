param([switch]$Restore)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $projectRoot 'runtime/guard'
$installPath = Join-Path $env:LOCALAPPDATA 'Cockpit Tools'
$targetExe = Join-Path $installPath 'cockpit-cliproxy.exe'
$cockpitExe = Join-Path $installPath 'cockpit-tools.exe'
$builtExe = Join-Path $runtimePath 'cockpit-cliproxy-fenjue.exe'
$backupExe = Join-Path $runtimePath 'original-cockpit-cliproxy.exe'
$recordPath = Join-Path $runtimePath 'deployment.local.json'
$controlDir = Join-Path $installPath 'fenjue-guard'
$controlPath = Join-Path $controlDir 'control.local.json'
$switchMutex = New-Object System.Threading.Mutex($false, 'Local\FenjueGuardSwitch')
$switchLock = $false
Push-Location $projectRoot
function File-Hash($path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Stop-CockpitExact {
    # Only these two verified paths, never Codex or unrelated Python processes.
    $apps = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $cockpitExe })
    foreach ($app in $apps) { Stop-Process -Id $app.ProcessId -ErrorAction Stop }
    $relays = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $targetExe })
    foreach ($relay in $relays) { Stop-Process -Id $relay.ProcessId -ErrorAction SilentlyContinue }
    for ($n=0; $n -lt 20; $n++) {
        $remaining = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $targetExe -or $_.ExecutablePath -eq $cockpitExe })
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 250
    }
    throw 'Cockpit processes did not stop; installed executable left intact.'
}
try {
    try { $switchLock = $switchMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $switchLock = $true }
    if (-not $switchLock) { throw 'Another Fenjue switch/deployment operation is running.' }
    if (-not (Test-Path -LiteralPath $cockpitExe)) { throw 'Cockpit installation not found.' }
    if ((Get-Item -LiteralPath $cockpitExe).VersionInfo.ProductVersion -ne '1.3.66') { throw 'Unsupported Cockpit version. Rebuild/review the patch before installation.' }
    if (Test-Path -LiteralPath $recordPath) {
        $previousRecord = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if ((File-Hash $targetExe) -eq $previousRecord.patched_sha256) {
            $runningState = & (Join-Path $projectRoot '.venv/Scripts/python.exe') -m fenjue.outbound_control status
            if ($LASTEXITCODE -ne 0) { throw 'Cannot verify Cockpit is idle; deployment cancelled.' }
            if (($runningState | ConvertFrom-Json).active -ne 0) { throw 'Cockpit has guarded requests in flight; wait for them before deploying/restoring.' }
        }
    }
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    New-Item -ItemType Directory -Path $controlDir -Force | Out-Null
    $identityName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    foreach ($directory in @($runtimePath,$controlDir)) {
        & icacls.exe $directory /inheritance:r /grant:r "${identityName}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot protect deployment/control directory.' }
    }
    if ($Restore) {
        $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
        if ((File-Hash $backupExe) -ne $record.original_sha256) { throw 'Original backup hash mismatch.' }
        if ((File-Hash $targetExe) -ne $record.patched_sha256) { throw 'Installed binary changed; refusing to overwrite an update.' }
        $replacement = $backupExe
    } else {
        if (-not (Test-Path -LiteralPath $builtExe)) { throw 'Build and test the patch first.' }
        if (Test-Path -LiteralPath $recordPath) {
            $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
            if ((File-Hash $backupExe) -ne $record.original_sha256) { throw 'Original backup hash mismatch.' }
            if ((File-Hash $targetExe) -notin @($record.original_sha256,$record.patched_sha256)) { throw 'Installed binary changed; refusing to overwrite an update.' }
        } else {
            if (Test-Path -LiteralPath $backupExe) { throw 'Unregistered backup exists; inspect it before continuing.' }
            Copy-Item -LiteralPath $targetExe -Destination $backupExe
            $record = [pscustomobject]@{schema=1;original_sha256=(File-Hash $targetExe);patched_sha256=(File-Hash $builtExe);source_revision='5273335ef9a96611336c4b927759c125a0f092d7';build='fenjue-outbound-2'}
            if ((File-Hash $backupExe) -ne $record.original_sha256) { throw 'Backup verification failed.' }
        }
        $record.patched_sha256 = File-Hash $builtExe
        $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
        $replacement = $builtExe
    }
    # Switching binaries is a maintenance operation, never an automatic update.
    Stop-CockpitExact
    try {
        '{"schema":1,"enabled":false}' | Set-Content -LiteralPath $controlPath -Encoding ASCII
        Copy-Item -LiteralPath $replacement -Destination $targetExe -Force
        if ((File-Hash $targetExe) -ne (File-Hash $replacement)) { throw 'Installed binary verification failed.' }
    } catch {
        Copy-Item -LiteralPath $backupExe -Destination $targetExe -Force
        Start-Process -FilePath $cockpitExe -WorkingDirectory $installPath -WindowStyle Hidden | Out-Null
        throw
    }
    Start-Process -FilePath $cockpitExe -WorkingDirectory $installPath -WindowStyle Hidden | Out-Null
    if ($Restore) { Write-Output 'Original Cockpit sidecar restored. Fenjue takeover disabled.' }
    else { Write-Output 'Patched Cockpit sidecar installed; takeover is OFF until Start-Guard succeeds.' }
} finally {
    if ($switchLock) { $switchMutex.ReleaseMutex() }
    $switchMutex.Dispose()
    Pop-Location
}
