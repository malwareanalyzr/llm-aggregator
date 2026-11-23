$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$logs = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null
$pidFile = Join-Path $logs 'pids.txt'
if (Test-Path $pidFile) { Write-Host 'Already running? Run .\stop.ps1 first.' -ForegroundColor Yellow; exit 1 }

# .env: create, generate local secrets once, lock the file to the current user
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
foreach ($name in 'LITELLM_MASTER_KEY', 'ROUTER_API_KEY', 'MCP_API_KEY', 'WEBUI_SECRET_KEY') {
    if (-not (Select-String -Path .env -Pattern "^$name=sk-" -Quiet)) {
        $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
        $k = 'sk-' + -join ($b | ForEach-Object { '{0:x2}' -f $_ })
        (@(Get-Content .env) | Where-Object { $_ -notmatch "^$name=" }) + "$name=$k" | Set-Content .env -Encoding ascii
    }
}
icacls .env /inheritance:r /grant:r "${env:USERNAME}:F" | Out-Null

Get-Content .env | Where-Object { $_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$' } | ForEach-Object {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim().Trim('"'), 'Process')
}
$venv = Join-Path $PSScriptRoot '.venv\Scripts'
$env:PATH = "$venv;$env:LOCALAPPDATA\agy\bin;$env:PATH"   # uvx for supergateway, agy for the router

# Ollama
try { ollama list | Out-Null } catch { Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden; Start-Sleep 3 }
if (-not (ollama list | Select-String 'qwen2.5:3b')) { ollama pull qwen2.5:3b }

# Docker
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed (is Docker Desktop running?)' }

function Start-Bg($name, $exe, $argList) {
    $p = Start-Process -FilePath $exe -ArgumentList $argList -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "$logs\$name.log" -RedirectStandardError "$logs\$name.err.log"
    Add-Content $pidFile "$name=$($p.Id)"
    Write-Host "started $name (pid $($p.Id))"
}
if ($env:AA_API_KEY) {  # refresh the Artificial Analysis routing table (1 of 100 free daily requests)
    & "$venv\python.exe" aa_routing.py; if ($LASTEXITCODE -ne 0) { Write-Host 'AA refresh failed; using existing routing.json' -ForegroundColor Yellow }
}
Start-Bg 'router' "$venv\python.exe" 'router.py'
Start-Bg 'media_mcp' "$venv\python.exe" 'media_mcp.py'
Start-Bg 'elevenlabs' 'npx.cmd' "-y supergateway --stdio `"uvx elevenlabs-mcp`" --port 8001 --host 127.0.0.1 --apiKey $env:MCP_API_KEY --outputTransport streamableHttp"

# Phone access: tailnet-only HTTPS proxy to the loopback-bound Open WebUI
$tsExe = 'C:\Program Files\Tailscale\tailscale.exe'
$tsUrl = $null
if (Test-Path $tsExe) {
    if ((& $tsExe serve status 2>&1 | Out-String) -notmatch '3000') {
        # first run blocks until Serve is enabled for the tailnet; don't hang the whole start on it
        $p = Start-Process $tsExe -ArgumentList 'serve --bg 3000' -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logs\tailscale.log"
        if (-not $p.WaitForExit(15000)) { $p.Kill(); Write-Host "tailscale serve not enabled yet; see $logs\tailscale.log" -ForegroundColor Yellow }
    }
    $dns = ((& $tsExe status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.')
    if ($dns) { $tsUrl = "https://$dns" }
}

Write-Host "`nOpen WebUI : http://localhost:3000"
Write-Host "Router     : http://localhost:8010/v1  (Bearer ROUTER_API_KEY)"
Write-Host "MCP        : http://host.docker.internal:8001/mcp  |  http://host.docker.internal:8002/mcp  (Bearer MCP_API_KEY)"
if ($tsUrl) { Write-Host "Tailscale  : $tsUrl" } else { Write-Host 'Tailscale  : not detected' -ForegroundColor Yellow }
Write-Host "Logs       : $logs"
