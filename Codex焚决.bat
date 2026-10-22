@echo off
setlocal
chcp 65001 >nul
set "FENJUE_BAT=%~f0"
set "FENJUE_ARG=%~1"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:FENJUE_BAT,[Text.Encoding]::UTF8); $m='#'+'==PS==#'; iex $t.Substring($t.IndexOf($m)+$m.Length)"
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
#==PS==#
# ---- 以下是 PowerShell 明文脚本，cmd 不会执行到这里 ----
# Codex 重试代理菜单：Codex -> 重试代理 -> Cockpit（不修改 Cockpit，Cockpit 更新不受影响）
$ErrorActionPreference = 'Stop'
$u = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = $u
[Console]::OutputEncoding = $u
$OutputEncoding = $u
$env:PYTHONIOENCODING = 'utf-8'
$Host.UI.RawUI.WindowTitle = 'Codex 重试代理'

# 定位目录：环境变量 FENJUE_ROOT > 用户目录\Fenjue-Windows > 原来的固定路径
$candidates = @(
    $env:FENJUE_ROOT,
    (Join-Path $env:USERPROFILE 'Fenjue-Windows'),
    'C:\Users\admin\Fenjue-Windows'
) | Where-Object { $_ }
$root = $candidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'retry_proxy\control.py') -PathType Leaf } | Select-Object -First 1
if (-not $root) {
    Write-Host '找不到 retry_proxy\control.py，已检查：' -ForegroundColor Red
    $candidates | ForEach-Object { Write-Host "  $_" }
    Write-Host '按回车退出。'
    [void][Console]::ReadLine()
    exit 1
}
$python = Join-Path $root '.venv\Scripts\python.exe'
$control = Join-Path $root 'retry_proxy\control.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host "找不到 Python：$python" -ForegroundColor Red
    [void][Console]::ReadLine()
    exit 1
}

$actions = [ordered]@{
    '1' = @{ Name = '开启重试代理（Codex → 代理 → Cockpit，开机自启）'; Arg = 'on' }
    '2' = @{ Name = '关闭重试代理（Codex 直连 Cockpit）';             Arg = 'off' }
    '3' = @{ Name = '查看状态和最近的重试记录';                        Arg = 'status' }
}

# 带参数运行（如 Codex焚决.bat 1）时直接执行对应项后退出
$auto = "$env:FENJUE_ARG".Trim()

while ($true) {
    if ($auto) {
        $a = $auto
    } else {
        Clear-Host
        Write-Host '========================================'
        Write-Host '            Codex 重试代理'
        Write-Host '========================================'
        Write-Host ''
        Write-Host '模型满载 / 连不上时自动等 2、5、10 秒原样重发'
        Write-Host "目录：$root"
        Write-Host ''
        foreach ($k in $actions.Keys) { Write-Host "$k. $($actions[$k].Name)" }
        Write-Host '0. 退出菜单'
        Write-Host ''
        Write-Host '请输入 1/2/3/0，然后按回车：' -NoNewline
        $a = [Console]::ReadLine()
        if ($null -eq $a) { break }
        $a = $a.Trim()
        if ($a -eq '0' -or $a -ieq 'q') { break }
    }

    $act = $actions[$a]
    if (-not $act) {
        if ($auto) { Write-Host "未知参数：$auto（可用 1/2/3）" -ForegroundColor Red; exit 2 }
        continue
    }

    Write-Host ''
    $rc = 1
    try {
        & $python -X utf8 $control $act.Arg
        $rc = $LASTEXITCODE
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
    } finally {
        [Console]::InputEncoding = $u
        [Console]::OutputEncoding = $u
    }

    Write-Host ''
    if ($rc -eq 0) {
        Write-Host "√ $($act.Name)：完成" -ForegroundColor Green
    } else {
        Write-Host "× $($act.Name)：失败（退出码 $rc）" -ForegroundColor Red
    }

    if ($auto) { exit $rc }
    Write-Host '按回车返回菜单…' -NoNewline
    [void][Console]::ReadLine()
}
