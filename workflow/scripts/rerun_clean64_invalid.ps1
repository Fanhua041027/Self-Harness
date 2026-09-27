param(
    [Parameter(Mandatory = $true)][ValidateSet("deepseek", "qwen")][string]$Label,
    [string]$CandidateId,
    [string]$CellsFile,
    [switch]$Execute,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$rerunScript = Join-Path $root "eval\scripts\rerun_invalid_cases.py"
$config = Join-Path $root "eval\configs\harbor_clean64_rerun.toml"
$taskRoot = (Resolve-Path (Join-Path $root "runs\terminal-bench-2-reliable-v2")).Path

if ($Label -eq "deepseek") {
    $model = "openai:deepseek-v4-flash"
    $baseUrl = "https://api.deepseek.com"
    $apiKeyName = "SELF_HARNESS_DEEPSEEK_API_KEY"
} else {
    $model = "openai:qwen3.7-plus"
    $baseUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    $apiKeyName = "SELF_HARNESS_QWEN_API_KEY"
}

$candidateArgs = @()
if ($CandidateId) {
    $queuePath = Join-Path $root "runs\clean64-$Label-self-harness\candidate_queue.json"
    if (-not (Test-Path -LiteralPath $queuePath)) { throw "candidate queue not found: $queuePath" }
    $queue = Get-Content -Raw -Encoding UTF8 $queuePath | ConvertFrom-Json
    $matches = @($queue.candidates | Where-Object { $_.candidate_id -eq $CandidateId })
    if ($matches.Count -ne 1) {
        throw "expected exactly one candidate '$CandidateId' in $queuePath; found $($matches.Count)"
    }
    $candidateDir = (Resolve-Path -LiteralPath $matches[0].candidate_dir).Path
    $outputDir = Split-Path -Parent (Resolve-Path -LiteralPath $matches[0].eval_result).Path
    $candidateArgs = @("--candidate-workspace", $candidateDir)
    $targetName = "$Label-$CandidateId"
} else {
    $outputDir = (Resolve-Path -LiteralPath (Join-Path $root "runs\clean64-$Label-baseline")).Path
    $targetName = "$Label-baseline"
}

$planDir = Join-Path $root "paper\generated\rerun-plans"
New-Item -ItemType Directory -Force -Path $planDir | Out-Null
if ($CellsFile) {
    $manifestName = [IO.Path]::GetFileNameWithoutExtension($CellsFile)
    $safeManifestName = $manifestName -replace '[^A-Za-z0-9._-]', '_'
    $planPath = Join-Path $planDir "$targetName-$safeManifestName.json"
} else {
    $planPath = Join-Path $planDir "$targetName.json"
}
$commonArgs = @(
    $rerunScript,
    "--output-dir", $outputDir,
    "--config", $config,
    "--plan-out", $planPath
) + $candidateArgs
if ($CellsFile) {
    $resolvedCellsFile = (Resolve-Path -LiteralPath $CellsFile).Path
    $commonArgs += @("--cells-file", $resolvedCellsFile)
}

if (-not $Execute) {
    & $python @commonArgs
    if ($LASTEXITCODE -ne 0) { throw "$targetName dry-run failed with exit code $LASTEXITCODE" }
    Write-Output "No model calls were made. Review plan: $planPath"
    exit 0
}

$apiKey = [Environment]::GetEnvironmentVariable($apiKeyName, "User")
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "$apiKeyName is missing or blank in the Windows user environment"
}
$env:OPENAI_API_KEY = $apiKey
$env:OPENAI_BASE_URL = $baseUrl
$env:SELF_HARNESS_MODEL = $model
$env:SELF_HARNESS_TB2_ROOT = $taskRoot
$env:PYTHONUTF8 = "1"

$logPath = Join-Path $root "runs\clean64-$targetName-rerun-invalid.log"
$executeArgs = $commonArgs + @("--execute")

if ($Foreground) {
    & $python @executeArgs 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) { throw "$targetName invalid-case rerun failed with exit code $LASTEXITCODE" }
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdout = Join-Path $root "runs\clean64-$targetName-rerun-$stamp.stdout.log"
$stderr = Join-Path $root "runs\clean64-$targetName-rerun-$stamp.stderr.log"
$quotedArgs = ($executeArgs | ForEach-Object { '"' + ($_ -replace '"', '`"') + '"' }) -join " "
$command = @"
`$ErrorActionPreference = "Stop"
`$apiKey = [Environment]::GetEnvironmentVariable("$apiKeyName", "User")
if ([string]::IsNullOrWhiteSpace(`$apiKey)) { throw "$apiKeyName is missing or blank in the Windows user environment" }
`$env:OPENAI_API_KEY = `$apiKey
`$env:OPENAI_BASE_URL = "$baseUrl"
`$env:SELF_HARNESS_MODEL = "$model"
`$env:SELF_HARNESS_TB2_ROOT = "$taskRoot"
`$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath "$root"
& "$python" $quotedArgs 2>&1 | Tee-Object -FilePath "$logPath" -Append
if (`$LASTEXITCODE -ne 0) { throw "$targetName invalid-case rerun failed with exit code `$LASTEXITCODE" }
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$process = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Output "$targetName invalid-case rerun started in background (PID: $($process.Id))"
Write-Output "plan: $planPath"
Write-Output "stdout: $stdout"
Write-Output "stderr: $stderr"
