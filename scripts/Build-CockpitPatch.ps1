$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourcePath = Join-Path $projectRoot 'runtime/cockpit-source/sidecars/cockpit-cliproxy'
$goExe = Join-Path $projectRoot 'runtime/toolchain/go/bin/go.exe'
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
& $pythonExe (Join-Path $PSScriptRoot 'prepare_cockpit_patch.py')
if ($LASTEXITCODE -ne 0) { throw 'Source overlay failed.' }
Push-Location $sourcePath
try {
    & $goExe test . -count=1
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit tests failed; build stopped.' }
    & $goExe build -trimpath -o (Join-Path $projectRoot 'runtime/guard/cockpit-cliproxy-fenjue.exe') .
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit build failed.' }
} finally { Pop-Location }
