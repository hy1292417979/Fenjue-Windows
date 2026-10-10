param(
    [string]$SourcePath,
    [string]$GoExe,
    [string]$PythonExe
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $SourcePath) { $SourcePath = Join-Path $projectRoot 'runtime/cockpit-source' }
if (-not $GoExe) { $GoExe = Join-Path $projectRoot 'runtime/toolchain/go/bin/go.exe' }
if (-not $PythonExe) { $PythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe' }
$sourcePath = [IO.Path]::GetFullPath($SourcePath)
$goExe = [IO.Path]::GetFullPath($GoExe)
$pythonExe = [IO.Path]::GetFullPath($PythonExe)
$sidecarPath = Join-Path $sourcePath 'sidecars/cockpit-cliproxy'
$outputPath = Join-Path $projectRoot 'runtime/guard/cockpit-cliproxy-fenjue.exe'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Python 3.12 environment not found: $pythonExe. Run Install.cmd first."
}
if (-not (Test-Path -LiteralPath $goExe -PathType Leaf)) {
    throw "Go 1.26.9 windows/amd64 toolchain not found: $goExe. Supply -GoExe."
}
if (-not (Test-Path -LiteralPath $sidecarPath -PathType Container)) {
    throw "Cockpit source checkout is missing sidecars/cockpit-cliproxy: $sourcePath. Supply -SourcePath to the fixed official checkout."
}
foreach ($required in @('go.mod', 'relay_server.go')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sidecarPath $required) -PathType Leaf)) {
        throw "Cockpit source is incomplete; missing $required under $sidecarPath."
    }
}
$goVersion = (& $goExe version 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $goVersion -notmatch 'go1\.26\.9\s+windows/amd64') {
    throw "Unsupported Go toolchain ($goVersion). Cockpit patch requires Go 1.26.9 windows/amd64."
}
$revision = (& git -C $sourcePath rev-parse HEAD 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $revision -ne '5273335ef9a96611336c4b927759c125a0f092d7') {
    throw 'Unexpected Cockpit revision; do not patch an unknown version.'
}
New-Item -ItemType Directory -Path (Split-Path -Parent $outputPath) -Force | Out-Null
& $pythonExe (Join-Path $PSScriptRoot 'prepare_cockpit_patch.py') --source $sourcePath
if ($LASTEXITCODE -ne 0) { throw 'Source overlay failed.' }
Push-Location $sidecarPath
try {
    & $goExe test . -count=1
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit tests failed; build stopped.' }
    & $goExe build -trimpath -o $outputPath .
    if ($LASTEXITCODE -ne 0) { throw 'Cockpit build failed.' }
} finally { Pop-Location }
Write-Output "Cockpit patch built: $outputPath"
