param(
    [string]$Languages = 'en,uk,ru,pl',
    [ValidateRange(1,65535)][int]$Port = 5000,
    [switch]$UpdateModels
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtimePython = Join-Path $projectRoot '.tools\libretranslate\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) { throw 'Run scripts/setup-libretranslate.ps1 first.' }
if ($Languages -notmatch '^[a-z]{2}(,[a-z]{2})*$') { throw 'Languages must be comma-separated ISO codes, e.g. en,uk,ru,pl.' }
$languageList = $Languages.Split(',')
foreach ($requiredLanguage in @('en','uk','ru')) {
    if ($requiredLanguage -notin $languageList) { throw 'Include en,uk,ru so Russian can translate to Ukrainian through English.' }
}
$runtimeArguments = @('-X', 'utf8', 'scripts/run_libretranslate.py', '--host', '127.0.0.1', '--port', $Port, '--load-only', $Languages, '--disable-web-ui', '--disable-files-translation')
if ($UpdateModels) { $runtimeArguments += '--update-models' }
& $runtimePython @runtimeArguments
if ($LASTEXITCODE -ne 0) { throw "LibreTranslate exited with code $LASTEXITCODE" }
