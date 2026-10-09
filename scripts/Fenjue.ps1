param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Missing project Python environment. See README.md.' }
Push-Location -LiteralPath $projectRoot
try {
    if (-not $CliArgs -or $CliArgs.Count -eq 0) { $CliArgs = @('candidates') }
    & $pythonExe -X utf8 -m fenjue.discovery @CliArgs
    exit $LASTEXITCODE
} finally { Pop-Location }
