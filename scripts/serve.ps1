param(
    [switch]$DirectModel,
    [switch]$Reload,
    [ValidateRange(1024, 65535)][int]$Port = 18080
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $backendPython)) { throw 'Create the project virtual environment first with uv sync.' }
$previousNoProxy = $env:NO_PROXY
Push-Location $projectRoot
try {
    if ($DirectModel) {
        # Bypass only this model host; preserve system proxy settings and secrets.
        $modelDirectHost = & $backendPython -c 'from urllib.parse import urlsplit; from src.agent.config import get_settings; print(urlsplit(get_settings().model_base_url).hostname)'
        if ($LASTEXITCODE -ne 0 -or -not $modelDirectHost -or $modelDirectHost -eq 'None') { throw 'Cannot read the configured model hostname.' }
        $env:NO_PROXY = (@($previousNoProxy, '127.0.0.1', 'localhost', $modelDirectHost.Trim()) | Where-Object { $_ }) -join ','
        Write-Host 'Model requests bypass the environment proxy for the configured model host only.'
    }
    $serverArguments = @('-m', 'uvicorn', 'src.api_view.app:app', '--host', '127.0.0.1', '--port', "$Port")
    if ($Reload) { $serverArguments += '--reload' }
    & $backendPython @serverArguments
    $backendExitCode = $LASTEXITCODE
} finally {
    $env:NO_PROXY = $previousNoProxy
    Pop-Location
}
exit $backendExitCode
