$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the bot dependencies first.' }
New-Item -ItemType Directory -Path data -Force | Out-Null
$metadata = @{}
foreach ($service in @(
    @{ Name='translator'; Module='translator'; Url='http://127.0.0.1:8081/health' },
    @{ Name='gateway'; Module='translator.gateway'; Url='http://127.0.0.1:8082/gateway/health' }
)) {
    $healthy = $false
    try {
        $health = Invoke-RestMethod $service.Url -TimeoutSec 2
        $healthy = $health.ok -eq $true
    } catch { }
    if (-not $healthy) {
        $serviceProcess = Start-Process -FilePath $python -ArgumentList @('-u','-X','utf8','-m',$service.Module) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput "data/$($service.Name).stdout.log" -RedirectStandardError "data/$($service.Name).stderr.log" -PassThru
        $metadata[$service.Name] = @{ pid=$serviceProcess.Id; started_at=(Get-Date).ToUniversalTime().ToString('o') }
        for ($attempt=0; $attempt -lt 60; $attempt++) {
            if ($serviceProcess.HasExited) { throw "$($service.Name) exited. Check data/$($service.Name).stderr.log." }
            try {
                $health = Invoke-RestMethod $service.Url -TimeoutSec 1
                if ($health.ok -eq $true) { $healthy = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $healthy) { throw "$($service.Name) did not become healthy. Check its local logs." }
}
if ($metadata.Count) { $metadata | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath data/runtime.shared.json -Encoding utf8 }
& $python -X utf8 -m translator.shared_tunnel
if ($LASTEXITCODE -ne 0) { throw 'Could not connect the shared tunnel. Check local services and ngrok limits; this script does not replace or delete existing endpoints.' }
Write-Host 'Translator and shared gateway are running in the background. The planner process was not restarted.'
