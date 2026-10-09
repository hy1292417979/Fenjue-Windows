param(
    [Parameter(Mandatory=$true)][string]$Task,
    [ValidateSet('auto','codex_http','sub2api')][string]$Route = 'auto',
    [ValidateSet('low','medium','high','xhigh','max','ultra')][string]$Effort = 'max'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$keyFile = Join-Path $projectRoot 'runtime/gateway-key.local.json'
$credential = Get-Content -LiteralPath $keyFile -Raw | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($credential.api_key)"; 'X-Fenjue-Route' = $Route }
$body = @{model='gpt-6-astra'; reasoning=@{effort=$Effort}; input=$Task; stream=$false} | ConvertTo-Json -Depth 15
$response = Invoke-RestMethod -Uri 'http://127.0.0.1:17877/v1/responses' -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 360
$messages = @($response.output | Where-Object { $_.type -eq 'message' })
$finals = @($messages | Where-Object { $_.phase -eq 'final_answer' })
if ($finals.Count -gt 0) { $messages = $finals }
foreach ($message in $messages) {
    foreach ($part in $message.content) {
        if ($part.type -eq 'output_text') { Write-Output $part.text }
    }
}
