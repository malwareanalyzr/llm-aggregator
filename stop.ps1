Set-Location $PSScriptRoot
$pidFile = Join-Path $PSScriptRoot 'logs\pids.txt'
if (Test-Path $pidFile) {
    Get-Content $pidFile | ForEach-Object {
        $name, $id = $_ -split '='
        taskkill /T /F /PID $id 2>$null | Out-Null   # /T kills npx -> node -> uvx child tree
        Write-Host "stopped $name (pid $id)"
    }
    Remove-Item $pidFile
}
docker compose down
