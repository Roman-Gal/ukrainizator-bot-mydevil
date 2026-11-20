$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Path data/backups -Force | Out-Null
# SQLite backup includes committed WAL transactions without stopping the worker.
@'
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from translator.config import Settings
source_path = Path(Settings.from_env().database)
if source_path.exists():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    destination_path = Path('data/backups') / f'translator-{stamp}.sqlite'
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(destination_path)) as destination:
        source.backup(destination)
'@ | & $python -X utf8 -
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed; no process stopped.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue)
foreach ($processId in ($listeners.OwningProcess | Sort-Object -Unique)) {
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$([uint32]$processId)"
    if (-not $processInfo -or $processInfo.Name -ne 'python.exe' -or $processInfo.CommandLine -notmatch '\s-m\s+translator\s*$') {
        throw 'Port 8081 belongs to an unrelated process; nothing stopped.'
    }
    Stop-Process -Id $processId
}
$serviceProcess = Start-Process -FilePath $python -ArgumentList @('-u','-X','utf8','-m','translator') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput data/translator.stdout.log -RedirectStandardError data/translator.stderr.log -PassThru
$metadata = if (Test-Path -LiteralPath data/runtime.shared.json) { Get-Content -Raw -LiteralPath data/runtime.shared.json | ConvertFrom-Json } else { [PSCustomObject]@{} }
$metadata | Add-Member -NotePropertyName translator -NotePropertyValue @{ pid=$serviceProcess.Id; started_at=(Get-Date).ToUniversalTime().ToString('o') } -Force
$metadata | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath data/runtime.shared.json -Encoding utf8
for ($attempt=0; $attempt -lt 60; $attempt++) {
    if ($serviceProcess.HasExited) { throw 'Translator exited. Check data/translator.stderr.log.' }
    try {
        $health = Invoke-RestMethod http://127.0.0.1:8081/health -TimeoutSec 1
        if ($health.ok -eq $true) { Write-Host 'Translator restarted; gateway, planner, ngrok and LibreTranslate kept running.'; return }
    } catch { }
    Start-Sleep -Milliseconds 500
}
throw 'Translator did not become healthy. Check its local logs.'
