$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtimePython = Join-Path $projectRoot '.tools\libretranslate\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & py -3.12 -m venv .tools\libretranslate\.venv
    if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12, then rerun this script.' }
}
& $runtimePython -m pip install --disable-pip-version-check -r requirements-libretranslate.txt
if ($LASTEXITCODE -ne 0) { throw 'LibreTranslate installation failed.' }
Write-Host 'LibreTranslate is installed. Run scripts/start-libretranslate.ps1 to download models and start the local API.'
