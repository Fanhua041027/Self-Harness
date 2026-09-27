param(
    [Parameter(Mandatory = $true)][ValidateSet("deepseek", "qwen")][string]$Label,
    [Parameter(Mandatory = $true)][string]$CandidateId
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$queuePath = Join-Path $root "runs\clean64-$Label-self-harness\candidate_queue.json"
$baselineResult = Join-Path $root "runs\clean64-$Label-baseline\result.json"
$acceptanceScript = Join-Path $root "acceptance\scripts\run_acceptance_gate.py"
$analysisScript = Join-Path $root "paper\analyze_experiments.py"
$sealedLauncher = Join-Path $root "workflow\scripts\run_qwen_sealed21.ps1"
$paperAuditScript = Join-Path $root "paper\audit_paper_consistency.py"
$validityScript = Join-Path $root "eval\scripts\result_validity.py"

if (-not (Test-Path -LiteralPath $queuePath)) { throw "candidate queue not found: $queuePath" }
if (-not (Test-Path -LiteralPath $baselineResult)) { throw "baseline result not found: $baselineResult" }

function Read-StrictJsonObject {
    param([Parameter(Mandatory = $true)][string]$Path)
    $lines = & $python $validityScript --result $Path --emit-object
    if ($LASTEXITCODE -ne 0) {
        throw "strict JSON object validation failed: $Path"
    }
    $serialized = ($lines -join [Environment]::NewLine)
    return $serialized | ConvertFrom-Json
}

function Resolve-QueuePath {
    param([Parameter(Mandatory = $true)][string]$Value)
    $candidate = if ([System.IO.Path]::IsPathRooted($Value)) {
        $Value
    } else {
        Join-Path $root $Value
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Assert-PathUnder {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $normalizedPath = $Path.TrimEnd('\')
    $normalizedRoot = $RootPath.TrimEnd('\') + '\'
    if (-not $normalizedPath.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label is outside the expected experiment workspace: $Path"
    }
}

$queue = Read-StrictJsonObject $queuePath
$matches = @($queue.candidates | Where-Object { $_.candidate_id -eq $CandidateId })
if ($matches.Count -ne 1) {
    throw "expected exactly one candidate '$CandidateId' in $queuePath; found $($matches.Count)"
}
$candidateWorkspaceRoot = (Resolve-Path -LiteralPath (Join-Path $root "runs\clean64-$Label-self-harness\branches")).Path
$candidateDir = Resolve-QueuePath ([string]$matches[0].candidate_dir)
Assert-PathUnder -Path $candidateDir -RootPath $candidateWorkspaceRoot -Label "candidate_dir"
$candidateManifestPath = Join-Path $candidateDir "manifest.json"
if (-not (Test-Path -LiteralPath $candidateManifestPath)) {
    throw "candidate manifest not found: $candidateManifestPath"
}
$candidateManifest = Read-StrictJsonObject $candidateManifestPath
if ([string]$candidateManifest.candidate_id -ne $CandidateId) {
    throw "candidate manifest id does not match requested candidate: $CandidateId"
}
$expectedCandidateResult = Resolve-QueuePath (Join-Path $candidateDir "eval\result.json")
$candidateResult = Resolve-QueuePath ([string]$matches[0].eval_result)
if ($candidateResult -ne $expectedCandidateResult) {
    throw "candidate queue eval_result is not bound to candidate manifest directory: $candidateResult"
}
$strictOutput = Join-Path $candidateDir "acceptance.strict.json"

function Get-EffectiveInvalidCount([string]$ResultPath) {
    # 收口前置审计必须与 acceptance gate 使用同一 strict reward 口径。
    $raw = & $python $validityScript --result $ResultPath --count --require-reward
    if ($LASTEXITCODE -ne 0) { throw "effective-invalid audit failed: $ResultPath" }
    return [int]$raw
}

$baselineInvalid = Get-EffectiveInvalidCount $baselineResult
$candidateInvalid = Get-EffectiveInvalidCount $candidateResult
if ($baselineInvalid -gt 0 -or $candidateInvalid -gt 0) {
    throw "strict acceptance blocked: baseline invalid=$baselineInvalid, candidate invalid=$candidateInvalid"
}

& $python $acceptanceScript `
    --baseline-result $baselineResult `
    --candidate-result $candidateResult `
    --output $strictOutput
if ($LASTEXITCODE -ne 0) {
    throw "strict acceptance failed; unresolved invalid results must be rerun first"
}

& $python $analysisScript
if ($LASTEXITCODE -ne 0) { throw "experiment analysis rebuild failed with exit code $LASTEXITCODE" }

# 生成 strict artifact 后立即刷新 Sealed readiness plan 和论文审计，避免
# acceptance 已更新而 execution plan 仍停留在 strict_decision=missing 的旧状态。
& $sealedLauncher
if ($LASTEXITCODE -ne 0) { throw "Sealed21 readiness plan refresh failed with exit code $LASTEXITCODE" }
& $python $paperAuditScript
if ($LASTEXITCODE -ne 0) { throw "paper consistency audit failed after strict finalization" }

$decision = Read-StrictJsonObject $strictOutput
Write-Output "strict decision: $($decision.decision)"
Write-Output "reason: $($decision.reason)"
Write-Output "artifact: $strictOutput"
Write-Output "paper tables rebuilt under: $(Join-Path $root 'paper\generated')"
