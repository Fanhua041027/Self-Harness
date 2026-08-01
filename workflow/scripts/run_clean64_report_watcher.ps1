param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$builder = Join-Path $root "eval\scripts\build_clean64_live_report.py"
$statusPath = Join-Path $root "runs\clean64-baselines-status.json"
$watcherStatus = Join-Path $root "runs\clean64-report-watcher-status.json"

while ($true) {
    & $python $builder
    if ($LASTEXITCODE -ne 0) {
        throw "report builder failed with exit code $LASTEXITCODE"
    }
    $baselineStatus = if (Test-Path $statusPath) {
        Get-Content $statusPath -Raw -Encoding utf8 | ConvertFrom-Json
    } else {
        $null
    }
    @{
        state = "running"
        baseline_stage = if ($baselineStatus) { $baselineStatus.stage } else { "unknown" }
        baseline_state = if ($baselineStatus) { $baselineStatus.state } else { "unknown" }
        updated_at = (Get-Date).ToString("o")
        process_id = $PID
    } | ConvertTo-Json | Set-Content -LiteralPath $watcherStatus -Encoding utf8
    if ($baselineStatus -and $baselineStatus.state -in @("complete", "failed")) {
        break
    }
    Start-Sleep -Seconds 300
}

@{
    state = "complete"
    baseline_stage = $baselineStatus.stage
    baseline_state = $baselineStatus.state
    updated_at = (Get-Date).ToString("o")
    process_id = $PID
} | ConvertTo-Json | Set-Content -LiteralPath $watcherStatus -Encoding utf8
