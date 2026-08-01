param()

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$runner = Join-Path $root "eval\scripts\run_harbor_eval.py"
$config = Join-Path $root "eval\configs\harbor_local_clean64.toml"
$taskRoot = (Resolve-Path (Join-Path $root "runs\terminal-bench-2-reliable-v2")).Path
$statusPath = Join-Path $root "runs\clean64-baselines-status.json"

if (-not $env:SELF_HARNESS_DEEPSEEK_API_KEY) {
    throw "SELF_HARNESS_DEEPSEEK_API_KEY is required"
}
if (-not $env:SELF_HARNESS_QWEN_API_KEY) {
    throw "SELF_HARNESS_QWEN_API_KEY is required"
}

function Write-RunStatus {
    param([string]$Stage, [string]$State, [string]$Message)
    @{
        stage = $Stage
        state = $State
        message = $Message
        updated_at = (Get-Date).ToString("o")
        process_id = $PID
    } | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding utf8
}

function Invoke-Clean64Baseline {
    param(
        [string]$Label,
        [string]$Model,
        [string]$BaseUrl,
        [string]$ApiKey,
        [string]$OutputName
    )
    $outputDir = Join-Path $root "runs\$OutputName"
    $logPath = Join-Path $root "runs\$OutputName.log"
    $env:OPENAI_API_KEY = $ApiKey
    $env:OPENAI_BASE_URL = $BaseUrl
    $env:SELF_HARNESS_MODEL = $Model
    $env:SELF_HARNESS_TB2_ROOT = $taskRoot
    Remove-Item Env:SELF_HARNESS_CANDIDATE_WORKSPACE -ErrorAction SilentlyContinue
    Write-RunStatus -Stage $Label -State "running" -Message "Clean64 baseline is running"
    & $python $runner --config $config --output-dir $outputDir --reuse-existing 2>&1 |
        Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
    Write-RunStatus -Stage $Label -State "complete" -Message "Clean64 baseline completed"
}

try {
    Invoke-Clean64Baseline `
        -Label "deepseek" `
        -Model "openai:deepseek-v4-flash" `
        -BaseUrl "https://api.deepseek.com" `
        -ApiKey $env:SELF_HARNESS_DEEPSEEK_API_KEY `
        -OutputName "clean64-deepseek-baseline"
    Invoke-Clean64Baseline `
        -Label "qwen" `
        -Model "openai:qwen3.7-plus" `
        -BaseUrl "https://dashscope.aliyuncs.com/compatible-mode/v1" `
        -ApiKey $env:SELF_HARNESS_QWEN_API_KEY `
        -OutputName "clean64-qwen-baseline"
    Write-RunStatus -Stage "all" -State "complete" -Message "Both Clean64 baselines completed"
}
catch {
    Write-RunStatus -Stage "failed" -State "failed" -Message $_.Exception.Message
    throw
}
