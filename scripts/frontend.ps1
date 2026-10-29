param([ValidateSet('dev', 'build', 'preview', 'install')][string]$Action = 'dev')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$frontendPath = Join-Path $projectRoot 'frontend'
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
$bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$nodeCandidates = @($env:CHEXU_NODE_EXE, $nodeCommand.Source, $bundledNode) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
$frontendNode = $null
foreach ($candidate in $nodeCandidates) {
    $versionText = (& $candidate --version).TrimStart('v')
    if ([version]$versionText -ge [version]'22.12.0') { $frontendNode = $candidate; break }
}
if (-not $frontendNode) { throw 'Node.js 22.12 or newer is required. Install it on E: or set CHEXU_NODE_EXE to an existing runtime.' }
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
$npmCandidates = @((Join-Path (Split-Path $frontendNode) 'node_modules\npm\bin\npm-cli.js'))
if ($npmCommand) { $npmCandidates += Join-Path (Split-Path $npmCommand.Source) 'node_modules\npm\bin\npm-cli.js' }
$frontendNpm = $npmCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $frontendNpm) { throw 'npm was not found beside Node.js. Install a complete Node.js distribution on E:.' }
$previousPath = $env:Path
$previousCache = $env:npm_config_cache
try {
    $env:Path = (Split-Path $frontendNode) + ';' + $env:Path
    $env:npm_config_cache = Join-Path $projectRoot '.cache\npm'
    Push-Location $frontendPath
    Write-Host "Using Node.js: $frontendNode"
    if ($Action -eq 'install') { & $frontendNode $frontendNpm ci --no-audit --no-fund }
    else { & $frontendNode $frontendNpm run $Action }
    $frontendExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    $env:Path = $previousPath
    $env:npm_config_cache = $previousCache
}
exit $frontendExitCode
