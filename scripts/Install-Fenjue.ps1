param(
    [string]$PythonExe,
    [string]$Sub2Install = (Join-Path $env:LOCALAPPDATA 'Sub2API'),
    [ValidateRange(1,2147483647)][int]$AccountId = 1,
    [ValidateRange(1,2147483647)][int]$GroupId = 8,
    [switch]$Online,
    [ValidateSet('Cockpit','Sub2API')][string]$Backend = 'Cockpit'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousPipConfig = $env:PIP_CONFIG_FILE
$env:PIP_CONFIG_FILE = 'NUL'
Push-Location -LiteralPath $projectRoot
try {
    if (-not $PythonExe) {
        $candidates = @(
            (Join-Path $projectRoot '.venv/Scripts/python.exe'),
            (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe')
        )
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($pythonCommand) { $candidates += $pythonCommand.Source }
        $pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyCommand) {
            try {
                $located = & $pyCommand.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0) { $candidates += $located }
            } catch { }
        }
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                try {
                    & $candidate -c 'import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize(chr(80)) == 8 else 1)' 2>$null
                    if ($LASTEXITCODE -eq 0) { $PythonExe = $candidate; break }
                } catch { }
            }
        }
    }
    if (-not $PythonExe) { throw 'Install Python 3.12 x64 or pass -PythonExe with its full path.' }
    & $PythonExe -c 'import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize(chr(80)) == 8 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'This offline package requires Python 3.12 x64.' }
    $venvPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $PythonExe -m venv (Join-Path $projectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
    }
    & $venvPython -c 'import struct,sys; sys.exit(0 if sys.version_info[:2] == (3,12) and struct.calcsize(chr(80)) == 8 else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv is not Python 3.12 x64. Extract into a new directory.' }
    $wheelPath = Join-Path $projectRoot 'wheelhouse'
    if (Test-Path -LiteralPath $wheelPath) {
        & $venvPython -I -m pip --isolated install --no-index --find-links $wheelPath -r requirements.lock
    } elseif ($Online) {
        & $venvPython -I -m pip --isolated install -r requirements.lock
    } else {
        throw 'Missing wheelhouse. Use the complete release ZIP, or explicitly pass -Online for a source checkout.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    & $venvPython -I -m pip --isolated check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
    & $venvPython -I -c 'import httpx,anyio,fastapi,uvicorn,jwt,cryptography,websockets'
    if ($LASTEXITCODE -ne 0) { throw 'Runtime dependency import failed.' }
    & $venvPython -X utf8 -m compileall -q fenjue scripts
    if ($LASTEXITCODE -ne 0) { throw 'Source validation failed.' }
    $configPath = Join-Path $projectRoot 'screen.proposal.json'
    if ($Backend -eq 'Sub2API' -and -not (Test-Path -LiteralPath $configPath)) {
        $config = Get-Content -LiteralPath (Join-Path $projectRoot 'local.example.json') -Raw | ConvertFrom-Json
        $config.local_install = [IO.Path]::GetFullPath($Sub2Install)
        $config.account_id = $AccountId
        $config.group_id = $GroupId
        $config.study_id = 'astra-local-deployment'
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 10), $utf8)
    }
    Write-Output 'Installation complete. Existing configuration is preserved.'
    if ($Backend -eq 'Cockpit') {
        Write-Output 'Run Start-Cockpit.cmd to use Cockpit local access. No Sub2API installation is needed.'
    } else {
        Write-Output 'Run Sub2API-Status.cmd for status, or scripts/Start-Fenjue.ps1 for the legacy workbench.'
    }
    Write-Output 'Installation performs no inference and does not change account settings.'
} finally {
    $env:PIP_CONFIG_FILE = $previousPipConfig
    Pop-Location
}
