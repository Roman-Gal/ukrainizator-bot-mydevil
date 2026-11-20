$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install requirements.txt first.' }
if (-not (Test-Path -LiteralPath '.env')) { throw 'Copy .env.example to .env and set your new bot token and webhook secret.' }
$ngrokCommand = Get-Command ngrok -ErrorAction SilentlyContinue
$localNgrok = Join-Path $projectRoot '.tools\ngrok\ngrok.exe'
$plannerNgrok = Join-Path (Split-Path -Parent $projectRoot) 'telegram-task-planner\.tools\ngrok\ngrok.exe'
$ngrokExecutable = if ($ngrokCommand) { $ngrokCommand.Source } elseif (Test-Path -LiteralPath $localNgrok) { $localNgrok } elseif (Test-Path -LiteralPath $plannerNgrok) { $plannerNgrok } else { $null }
if (-not $ngrokExecutable) { throw 'Install ngrok in PATH or .tools\ngrok\ngrok.exe.' }

# A separate inspector port avoids conflicting with the planner's ngrok agent.
$inspectorPort = 4041
try {
    $null = Invoke-RestMethod "http://127.0.0.1:$inspectorPort/api/tunnels" -TimeoutSec 1
    throw 'Port 4041 already has an ngrok agent. Use the manual README instructions.'
} catch {
    if ($_.Exception.Message -like 'Port 4041*') { throw }
}
$translatorPort = & $python -c 'from translator.config import Settings; print(Settings.from_env().port)'
if ($LASTEXITCODE -ne 0 -or $translatorPort -notmatch '^\d+$' -or [int]$translatorPort -lt 1 -or [int]$translatorPort -gt 65535) { throw 'Invalid .env configuration.' }

$ownConfig = Join-Path $projectRoot '.tools\ngrok\ngrok.yml'
$defaultConfig = Join-Path $env:LOCALAPPDATA 'ngrok\ngrok.yml'
$plannerConfig = Join-Path (Split-Path -Parent $projectRoot) 'telegram-task-planner\.tools\ngrok\ngrok.yml'
$baseConfig = if (Test-Path -LiteralPath $ownConfig) { $ownConfig } elseif (Test-Path -LiteralPath $defaultConfig) { $defaultConfig } elseif (Test-Path -LiteralPath $plannerConfig) { $plannerConfig } else { $null }
if (-not $baseConfig) { throw 'Configure ngrok auth first: ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN' }
$versionLine = Get-Content -LiteralPath $baseConfig | Where-Object { $_ -match '^version\s*:' } | Select-Object -First 1
$configVersion = if ($versionLine -match '["'']?3["'']?\s*$') { 3 } else { 2 }
New-Item -ItemType Directory -Path 'data' -Force | Out-Null
$overrideConfig = Join-Path $projectRoot 'data\ngrok-override.yml'
$overrideText = if ($configVersion -eq 3) { "version: 3`nagent:`n  web_addr: 127.0.0.1:$inspectorPort`n" } else { "version: 2`nweb_addr: 127.0.0.1:$inspectorPort`n" }
Set-Content -LiteralPath $overrideConfig -Value $overrideText -Encoding ascii
$oldUrl = $env:PUBLIC_BASE_URL
$configArgument = '--config="' + $baseConfig + ',' + $overrideConfig + '"'
$ngrokArguments = @('http', "http://127.0.0.1:$translatorPort", $configArgument, '--log=data/ngrok.log', '--log-format=json')
$ngrokProcess = Start-Process -FilePath $ngrokExecutable -ArgumentList $ngrokArguments -WindowStyle Hidden -PassThru
try {
    $tunnelUrl = $null
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($ngrokProcess.HasExited) { throw 'ngrok exited. Check data/ngrok.log and ngrok account limits for simultaneous agents/endpoints.' }
        try {
            $tunnels = Invoke-RestMethod "http://127.0.0.1:$inspectorPort/api/tunnels" -TimeoutSec 1
            $tunnelUrl = ($tunnels.tunnels | Where-Object { $_.proto -eq 'https' -and $_.config.addr -match ":$translatorPort/?$" } | Select-Object -First 1).public_url
            if ($tunnelUrl) { break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $tunnelUrl) { throw 'No HTTPS tunnel found. Check data/ngrok.log.' }
    $env:PUBLIC_BASE_URL = $tunnelUrl
    Write-Host "Translator tunnel: $tunnelUrl"
    & $python -X utf8 -m translator
    if ($LASTEXITCODE -ne 0) { throw "Translator exited with code $LASTEXITCODE" }
} finally {
    $env:PUBLIC_BASE_URL = $oldUrl
    if (-not $ngrokProcess.HasExited) { Stop-Process -Id $ngrokProcess.Id }
}
