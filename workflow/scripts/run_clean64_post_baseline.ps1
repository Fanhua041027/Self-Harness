param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$loopScript = Join-Path $root "workflow\scripts\run_self_harness_loop.py"
$diagnosisScript = Join-Path $root "diagnosis\scripts\run_tb2_diagnosis.py"
$proposerScript = Join-Path $root "proposer\scripts\call_openai_proposer.py"
$finalReportScript = Join-Path $root "eval\scripts\build_clean64_final_report.py"
$evalConfig = Join-Path $root "eval\configs\harbor_local_clean64.toml"
$surface = "baseline=" + (Join-Path $root "eval\harness_workspace\repo_baseline.py")
$taskRoot = (Resolve-Path (Join-Path $root "runs\terminal-bench-2-reliable-v2")).Path
$statusPath = Join-Path $root "runs\clean64-self-harness-status.json"

if (-not $env:SELF_HARNESS_DEEPSEEK_API_KEY) { throw "SELF_HARNESS_DEEPSEEK_API_KEY is required" }
if (-not $env:SELF_HARNESS_QWEN_API_KEY) { throw "SELF_HARNESS_QWEN_API_KEY is required" }

function Write-PipelineStatus {
    param([string]$Stage, [string]$State, [string]$Message)
    @{
        stage = $Stage
        state = $State
        message = $Message
        updated_at = (Get-Date).ToString("o")
        process_id = $PID
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

function Assert-ValidBaseline {
    param([string]$Path)
    $payload = Get-Content $Path -Raw -Encoding utf8 | ConvertFrom-Json
    if ($payload.total -ne 128) { throw "baseline total must be 128: $Path" }
    $invalid = @(
        $payload.splits.PSObject.Properties.Value |
            ForEach-Object { $_ } |
            ForEach-Object { $_.case_results } |
            Where-Object { $_.status -eq "invalid" }
    )
    if ($invalid.Count -gt 0) {
        throw "baseline contains $($invalid.Count) invalid infrastructure trial(s): $Path"
    }
}

function Invoke-ModelEvolution {
    param(
        [string]$Label,
        [string]$Model,
        [string]$ApiModel,
        [string]$BaseUrl,
        [string]$ApiKey,
        [string]$BaselineResult,
        [string]$WorkName
    )
    Assert-ValidBaseline -Path $BaselineResult
    $workDir = Join-Path $root "runs\$WorkName"
    $baselineTarget = Join-Path $workDir "baseline_eval\result.json"
    New-Item -ItemType Directory -Path (Split-Path $baselineTarget) -Force | Out-Null
    Copy-Item -LiteralPath $BaselineResult -Destination $baselineTarget -Force
    $env:OPENAI_API_KEY = $ApiKey
    $env:OPENAI_BASE_URL = $BaseUrl
    $env:SELF_HARNESS_MODEL = $Model
    $env:SELF_HARNESS_TB2_ROOT = $taskRoot
    $env:PYTHONUTF8 = "1"
    Remove-Item Env:SELF_HARNESS_CANDIDATE_WORKSPACE -ErrorAction SilentlyContinue

    $diagnosisCommand = ('"{0}" "{1}" --result "{{baseline_result}}" --model "{2}" --output "{{diagnosis}}"' -f $python, $diagnosisScript, $ApiModel)
    $proposerCommand = ('"{0}" "{1}" --prompt "{{prompt}}" --output "{{response}}" --model "{2}"' -f $python, $proposerScript, $ApiModel)
    $logPath = Join-Path $root "runs\$WorkName.log"

    for ($round = 1; $round -le 2; $round++) {
        Write-PipelineStatus -Stage "$Label-round-$round" -State "running" -Message "K=3 candidate round is running"
        & $python $loopScript `
            --eval-config $evalConfig `
            --work-dir $workDir `
            --surface $surface `
            --route-count 3 `
            --diagnosis-command $diagnosisCommand `
            --proposer-command $proposerCommand `
            --max-candidates 0 2>&1 | Tee-Object -FilePath $logPath -Append
        if ($LASTEXITCODE -ne 0) { throw "$Label round $round failed with exit code $LASTEXITCODE" }
    }
}

try {
    $deepseekBaseline = Join-Path $root "runs\clean64-deepseek-baseline\result.json"
    $qwenBaseline = Join-Path $root "runs\clean64-qwen-baseline\result.json"
    Write-PipelineStatus -Stage "waiting-baselines" -State "waiting" -Message "Waiting for both complete baseline result files"
    while (-not ((Test-Path $deepseekBaseline) -and (Test-Path $qwenBaseline))) {
        Start-Sleep -Seconds 300
    }
    Invoke-ModelEvolution `
        -Label "deepseek" -Model "openai:deepseek-v4-flash" -ApiModel "deepseek-v4-flash" `
        -BaseUrl "https://api.deepseek.com" -ApiKey $env:SELF_HARNESS_DEEPSEEK_API_KEY `
        -BaselineResult $deepseekBaseline -WorkName "clean64-deepseek-self-harness"
    Invoke-ModelEvolution `
        -Label "qwen" -Model "openai:qwen3.7-plus" -ApiModel "qwen3.7-plus" `
        -BaseUrl "https://dashscope.aliyuncs.com/compatible-mode/v1" -ApiKey $env:SELF_HARNESS_QWEN_API_KEY `
        -BaselineResult $qwenBaseline -WorkName "clean64-qwen-self-harness"
    Write-PipelineStatus -Stage "final-report" -State "running" -Message "Generating final charts, report, and provenance"
    & $python $finalReportScript
    if ($LASTEXITCODE -ne 0) { throw "final report generation failed with exit code $LASTEXITCODE" }
    Write-PipelineStatus -Stage "all" -State "complete" -Message "Two K=3, T=2 model evolution pipelines completed"
}
catch {
    Write-PipelineStatus -Stage "failed" -State "failed" -Message $_.Exception.Message
    throw
}
