param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
Push-Location -LiteralPath $projectRoot
try {
    if (-not $CliArgs -or $CliArgs.Count -eq 0) { $CliArgs = @('status') }
    & $pythonExe -X utf8 -m fenjue.operator @CliArgs
    exit $LASTEXITCODE
} finally { Pop-Location }
