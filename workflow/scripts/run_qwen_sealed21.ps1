param(
    [string]$CandidateId = "anti_workaround_execution",
    [switch]$Execute,
    [switch]$Foreground,
    [switch]$AllowDirtyWorktree
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $root "venv\Scripts\python.exe"
$builder = Join-Path $root "eval\scripts\build_sealed_split.py"
$runner = Join-Path $root "eval\scripts\run_harbor_eval.py"
$analyzer = Join-Path $root "paper\analyze_sealed.py"
$manifest = Join-Path $root "configs\splits\sealed21.json"
$manifestLock = Join-Path $root "configs\splits\sealed21.json.sha256"
$preregistration = Join-Path $root "configs\experiments\ei_confirmation_v1.json"
$sealedConfig = Join-Path $root "eval\configs\harbor_local_sealed21.toml"
$cleanConfig = Join-Path $root "eval\configs\harbor_local_clean64.toml"
$taskRoot = (Resolve-Path (Join-Path $root "runs\terminal-bench-2-reliable-v2")).Path
$queuePath = Join-Path $root "runs\clean64-qwen-self-harness\candidate_queue.json"
$cleanBaselineResult = (Resolve-Path -LiteralPath (Join-Path $root "runs\clean64-qwen-baseline\result.json")).Path
$baselineSurface = Join-Path $root "eval\harness_workspace\repo_baseline.py"
$baselineOutput = Join-Path $root "runs\sealed21-qwen-baseline"
$candidateOutput = Join-Path $root "runs\sealed21-qwen-$CandidateId"
$analysisOutput = Join-Path $root "paper\generated\sealed21"
$freezePath = Join-Path $root "runs\sealed21-qwen-freeze.json"
$planPath = Join-Path $root "paper\generated\sealed21-execution-plan.json"
$wrapperPath = Join-Path $root "eval\harness_workspace\self_harness_harbor\harbor_wrapper.py"
$bridgePath = Join-Path $root "eval\harness_workspace\self_harness_harbor\backend_bridge.py"
$analysisCorePath = Join-Path $root "paper\analyze_experiments.py"
$acceptanceGatePath = Join-Path $root "acceptance\scripts\run_acceptance_gate.py"
$validityPath = Join-Path $root "eval\scripts\result_validity.py"
$environmentLockScript = Join-Path $root "eval\scripts\capture_environment_lock.py"
$environmentLockPath = Join-Path $root "paper\generated\sealed21-environment-lock.json"
$containerResolutionScript = Join-Path $root "eval\scripts\capture_container_resolution.py"
$containerResolutionPath = Join-Path $root "runs\sealed21-qwen-container-resolution.json"

function Read-StrictJsonObject {
    param([Parameter(Mandatory = $true)][string]$Path)
    $lines = & $python $validityPath --result $Path --emit-object
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

function Get-StringSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $algorithm.Dispose()
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

$sealedDesignScript = @'
import json
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
raw = tomllib.loads(path.read_text(encoding="utf-8"))
eval_config = raw.get("eval", {})
tasks = len(raw.get("cases", []))
repeats = int(eval_config.get("repeats", 0))
retries = int(eval_config.get("infrastructure_retries", 0))
concurrency = int(eval_config.get("case_concurrency", 0))
timeout_s = float(eval_config.get("timeout_s", 0))
if tasks < 1 or repeats < 1 or retries < 0 or concurrency < 1 or timeout_s <= 0:
    raise SystemExit("invalid Sealed TOML design fields")
evaluation_cells = tasks * repeats * 2
budget_hours = tasks * repeats * 2 * (retries + 1) * timeout_s / concurrency / 3600
print(json.dumps({
    "tasks": tasks,
    "repeats": repeats,
    "infrastructure_retries": retries,
    "case_concurrency": concurrency,
    "timeout_s": timeout_s,
    "evaluation_cells": evaluation_cells,
    "configured_outer_timeout_upper_hours": round(budget_hours, 6),
}))
'@
$sealedDesignJson = $sealedDesignScript | & $python - $sealedConfig
if ($LASTEXITCODE -ne 0) { throw "failed to derive Sealed design from TOML" }
$sealedDesign = ($sealedDesignJson -join [Environment]::NewLine) | ConvertFrom-Json
$sealedTasks = [int]$sealedDesign.tasks
$sealedRepeats = [int]$sealedDesign.repeats
$sealedInfrastructureRetries = [int]$sealedDesign.infrastructure_retries
$sealedCaseConcurrency = [int]$sealedDesign.case_concurrency
$sealedTimeoutSeconds = [double]$sealedDesign.timeout_s
$sealedEvaluationCells = [int]$sealedDesign.evaluation_cells
$sealedBudgetHours = [double]$sealedDesign.configured_outer_timeout_upper_hours

& $python $builder `
    --task-root $taskRoot `
    --clean-config $cleanConfig `
    --runs-root (Join-Path $root "runs") `
    --output $manifest `
    --config-output $sealedConfig `
    --verify
if ($LASTEXITCODE -ne 0) { throw "Sealed21 manifest verification failed" }

$queue = Read-StrictJsonObject $queuePath
$preregistrationObject = Read-StrictJsonObject $preregistration
$primaryEndpointContract = $preregistrationObject.primary_endpoint
$statisticalDesignContract = $preregistrationObject.statistical_design
$missingnessPolicyContract = $preregistrationObject.missingness_policy
$multiplicityContract = $preregistrationObject.multiplicity
if (-not $primaryEndpointContract -or -not $statisticalDesignContract -or -not $missingnessPolicyContract -or -not $multiplicityContract) {
    throw "preregistration is missing one or more frozen statistical contract sections"
}
$matches = @($queue.candidates | Where-Object { $_.candidate_id -eq $CandidateId })
if ($matches.Count -ne 1) {
    throw "expected exactly one candidate '$CandidateId' in $queuePath; found $($matches.Count)"
}
$candidateWorkspaceRoot = (Resolve-Path -LiteralPath (Join-Path $root "runs\clean64-qwen-self-harness\branches")).Path
$candidateDir = Resolve-QueuePath ([string]$matches[0].candidate_dir)
Assert-PathUnder -Path $candidateDir -RootPath $candidateWorkspaceRoot -Label "candidate_dir"
$candidateManifestPath = Join-Path $candidateDir "manifest.json"
if (-not (Test-Path -LiteralPath $candidateManifestPath)) { throw "candidate manifest not found: $candidateManifestPath" }
$candidateManifest = Read-StrictJsonObject $candidateManifestPath
if ([string]$candidateManifest.candidate_id -ne $CandidateId) {
    throw "candidate manifest id does not match requested candidate: $CandidateId"
}
$expectedCandidateResult = Resolve-QueuePath (Join-Path $candidateDir "eval\result.json")
$candidateCleanResult = Resolve-QueuePath ([string]$matches[0].eval_result)
if ($candidateCleanResult -ne $expectedCandidateResult) {
    throw "candidate queue eval_result is not bound to candidate manifest directory: $candidateCleanResult"
}
$surfaceRelative = $candidateManifest.surface_files.baseline
if (-not $surfaceRelative) { throw "candidate manifest has no baseline surface" }
$candidateSurface = (Resolve-Path -LiteralPath (Join-Path $candidateDir $surfaceRelative)).Path
$candidateHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidateSurface).Hash.ToLowerInvariant()
$baselineHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $baselineSurface).Hash.ToLowerInvariant()
$sealedManifestHash = (Get-Content -Raw -Encoding ASCII $manifestLock).Trim()
$preregistrationHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $preregistration).Hash.ToLowerInvariant()
$sealedConfigHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sealedConfig).Hash.ToLowerInvariant()
$cleanBaselineResultHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $cleanBaselineResult).Hash.ToLowerInvariant()
$cleanCandidateResultHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidateCleanResult).Hash.ToLowerInvariant()
$runnerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $runner).Hash.ToLowerInvariant()
$wrapperHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wrapperPath).Hash.ToLowerInvariant()
$bridgeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $bridgePath).Hash.ToLowerInvariant()
$analyzerHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $analyzer).Hash.ToLowerInvariant()
$analysisCoreHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $analysisCorePath).Hash.ToLowerInvariant()
$acceptanceGateHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $acceptanceGatePath).Hash.ToLowerInvariant()
$builderHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $builder).Hash.ToLowerInvariant()
$validityHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $validityPath).Hash.ToLowerInvariant()
$environmentLockScriptHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $environmentLockScript).Hash.ToLowerInvariant()
$containerResolutionScriptHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $containerResolutionScript).Hash.ToLowerInvariant()
$executionCodeCanonical = "backend_bridge=$bridgeHash`ncapture_container_resolution=$containerResolutionScriptHash`ncapture_environment_lock=$environmentLockScriptHash`nharbor_wrapper=$wrapperHash`nrun_harbor_eval=$runnerHash`n"
$analysisCodeCanonical = "acceptance_gate=$acceptanceGateHash`nanalyze_experiments=$analysisCoreHash`nanalyze_sealed=$analyzerHash`nbuild_sealed_split=$builderHash`nresult_validity=$validityHash`n"
$executionCodeHash = Get-StringSha256 $executionCodeCanonical
$analysisCodeHash = Get-StringSha256 $analysisCodeCanonical
$strictPath = Join-Path $candidateDir "acceptance.strict.json"
$strictReady = $false
$strictArtifactResultBinding = $false
$strictAcceptedFlag = $false
$strictSourceHashesStable = $false
$strictArtifactRecomputed = $false
$strictArtifactPresent = $false
$strictArtifactMalformed = $false
$strictDecision = "missing"
$strictHash = $null
if (Test-Path -LiteralPath $strictPath) {
    $strictArtifactPresent = $true
    try {
        $strict = Read-StrictJsonObject $strictPath
    } catch {
        $strictArtifactMalformed = $true
        $strictDecision = "malformed"
        $strictHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $strictPath).Hash.ToLowerInvariant()
    }
    if ($strictArtifactMalformed) {
        # 保持所有 strict 条件为 false，后续 readiness 只记录 blocker，不放行执行。
        $strict = $null
    } else {
    $strictDecision = [string]$strict.decision
    $strictAcceptedFlag = ($strict.PSObject.Properties.Name -contains "accepted") -and ($strict.accepted -is [bool]) -and [bool]$strict.accepted
    $strictSourceHashesStable = ($strict.PSObject.Properties.Name -contains "source_hashes_stable") -and ($strict.source_hashes_stable -is [bool]) -and [bool]$strict.source_hashes_stable
    $strictBaselinePathMatches = $false
    $strictCandidatePathMatches = $false
    try {
        $strictBaselinePathMatches = (Resolve-Path -LiteralPath ([string]$strict.baseline_result)).Path -eq $cleanBaselineResult
        $strictCandidatePathMatches = (Resolve-Path -LiteralPath ([string]$strict.candidate_result)).Path -eq $candidateCleanResult
    } catch {
        $strictBaselinePathMatches = $false
        $strictCandidatePathMatches = $false
    }
    $strictResultHashesMatch = ([string]$strict.baseline_result_sha256 -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $cleanBaselineResult).Hash.ToLowerInvariant()) -and
        ([string]$strict.candidate_result_sha256 -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $candidateCleanResult).Hash.ToLowerInvariant())
    $strictArtifactResultBinding = $strictBaselinePathMatches -and $strictCandidatePathMatches -and $strictResultHashesMatch
    if ($strictArtifactResultBinding) {
        $verificationOutput = & $python $acceptanceGatePath `
            --baseline-result $cleanBaselineResult `
            --candidate-result $candidateCleanResult `
            --verify-artifact $strictPath `
            --split train `
            --split heldout `
            --expected-repeats 2 2>&1
        $strictArtifactRecomputed = $LASTEXITCODE -eq 0
    }
    $strictReady = $strictDecision -eq "accepted" -and $strictAcceptedFlag -and $strictSourceHashesStable -and $strictArtifactResultBinding -and $strictArtifactRecomputed
        $strictHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $strictPath).Hash.ToLowerInvariant()
    }
}
$pythonVersion = (& $python --version 2>&1 | Out-String).Trim()
$harborCommand = Get-Command harbor -ErrorAction SilentlyContinue
$harborExecutable = if ($harborCommand) { $harborCommand.Source } else { $null }
$harborVersion = if ($harborCommand) { (& $harborCommand.Source --version 2>&1 | Out-String).Trim() } else { $null }
$harborPython = if ($harborExecutable) { Join-Path (Split-Path -Parent $harborExecutable) "python.exe" } else { $null }
$dependencyLock = $null
$dependencyBundleHash = "unavailable"
if ($harborPython -and (Test-Path -LiteralPath $harborPython)) {
    & $python $environmentLockScript `
        --project-python $python `
        --harbor-python $harborPython `
        --harbor-executable $harborExecutable `
        --output $environmentLockPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "environment dependency lock capture failed" }
    $dependencyLock = Read-StrictJsonObject $environmentLockPath
    $dependencyBundleHash = [string]$dependencyLock.bundle_sha256
}
$baselineRunIdentity = "sealed21:${sealedManifestHash}:${preregistrationHash}:${sealedConfigHash}:${executionCodeHash}:${dependencyBundleHash}:baseline:$baselineHash"
$candidateRunIdentity = "sealed21:${sealedManifestHash}:${preregistrationHash}:${sealedConfigHash}:${executionCodeHash}:${dependencyBundleHash}:candidate:$candidateHash"
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerVersionRaw = if ($dockerCommand) { (& $dockerCommand.Source version --format "{{.Client.Version}}|{{.Server.Version}}" 2>$null | Out-String).Trim() } else { "" }
$dockerAvailable = [bool]$dockerCommand -and $LASTEXITCODE -eq 0 -and $dockerVersionRaw.Contains("|")
$dockerVersions = if ($dockerAvailable) { $dockerVersionRaw.Split("|", 2) } else { @($null, $null) }
$gitHead = (& git -C $root rev-parse HEAD 2>$null | Out-String).Trim()
$gitDirty = @(& git -C $root status --porcelain 2>$null).Count -gt 0
$apiKeyAvailable = -not [string]::IsNullOrWhiteSpace(
    [Environment]::GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")
)
$sourceSnapshotReady = (-not $gitDirty) -or [bool]$AllowDirtyWorktree
$environmentReady = [bool]$harborCommand -and $dockerAvailable -and [bool]$dependencyLock
$readyToExecute = $strictReady -and $sourceSnapshotReady -and $environmentReady -and $apiKeyAvailable
$readinessBlockers = @()
if ($strictDecision -eq "missing") { $readinessBlockers += "strict_decision_missing" }
elseif ($strictDecision -ne "accepted") { $readinessBlockers += "strict_decision_not_accepted" }
if (-not $strictArtifactPresent) {
    $readinessBlockers += "strict_acceptance_artifact_missing"
} elseif ($strictArtifactMalformed) {
    $readinessBlockers += "strict_acceptance_artifact_malformed"
} else {
    if (-not $strictAcceptedFlag) { $readinessBlockers += "strict_acceptance_boolean_inconsistent" }
    if (-not $strictSourceHashesStable) { $readinessBlockers += "strict_acceptance_source_hashes_unstable" }
    if (-not $strictArtifactResultBinding) { $readinessBlockers += "strict_acceptance_result_binding_failed" }
}
if (-not $strictArtifactRecomputed) { $readinessBlockers += "strict_acceptance_not_recomputed" }
if (-not $sourceSnapshotReady) { $readinessBlockers += "source_snapshot_not_ready" }
if (-not $harborCommand) { $readinessBlockers += "harbor_unavailable" }
if (-not $dockerAvailable) { $readinessBlockers += "docker_unavailable" }
if (-not $apiKeyAvailable) { $readinessBlockers += "api_key_unavailable" }
if (-not $dependencyLock) { $readinessBlockers += "dependency_lock_unavailable" }
$readiness = [ordered]@{
    strict_acceptance = $strictReady
    strict_acceptance_result_binding = $strictArtifactResultBinding
    strict_acceptance_boolean_consistent = $strictAcceptedFlag
    strict_acceptance_source_hashes_stable = $strictSourceHashesStable
    strict_acceptance_recomputed = $strictArtifactRecomputed
    strict_acceptance_artifact_present = $strictArtifactPresent
    strict_acceptance_artifact_malformed = $strictArtifactMalformed
    readiness_blockers = @($readinessBlockers)
    clean_git_worktree = -not $gitDirty
    dirty_worktree_override = [bool]$AllowDirtyWorktree
    source_snapshot_ready = $sourceSnapshotReady
    harbor_available = [bool]$harborCommand
    docker_available = $dockerAvailable
    api_key_available = $apiKeyAvailable
    dependency_lock_available = [bool]$dependencyLock
}
$environmentSnapshot = [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    os_description = [Runtime.InteropServices.RuntimeInformation]::OSDescription
    os_architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    python_executable = $python
    python_version = $pythonVersion
    harbor_executable = $harborExecutable
    harbor_version = $harborVersion
    docker_available = $dockerAvailable
    docker_client_version = $dockerVersions[0]
    docker_server_version = $dockerVersions[1]
    git_head = $gitHead
    git_dirty = $gitDirty
    dirty_worktree_override = [bool]$AllowDirtyWorktree
    model = "openai:qwen3.7-plus"
    endpoint = "https://dashscope.aliyuncs.com/compatible-mode/v1"
}
$executionCode = [ordered]@{
    bundle_sha256 = $executionCodeHash
    files = [ordered]@{
        backend_bridge = [ordered]@{ path = $bridgePath; sha256 = $bridgeHash }
        capture_container_resolution = [ordered]@{ path = $containerResolutionScript; sha256 = $containerResolutionScriptHash }
        capture_environment_lock = [ordered]@{ path = $environmentLockScript; sha256 = $environmentLockScriptHash }
        harbor_wrapper = [ordered]@{ path = $wrapperPath; sha256 = $wrapperHash }
        run_harbor_eval = [ordered]@{ path = $runner; sha256 = $runnerHash }
    }
}
$analysisCode = [ordered]@{
    bundle_sha256 = $analysisCodeHash
    files = [ordered]@{
        acceptance_gate = [ordered]@{ path = $acceptanceGatePath; sha256 = $acceptanceGateHash }
        analyze_experiments = [ordered]@{ path = $analysisCorePath; sha256 = $analysisCoreHash }
        analyze_sealed = [ordered]@{ path = $analyzer; sha256 = $analyzerHash }
        build_sealed_split = [ordered]@{ path = $builder; sha256 = $builderHash }
        result_validity = [ordered]@{ path = $validityPath; sha256 = $validityHash }
    }
}

$plan = [ordered]@{
    format = "self_harness.sealed_execution_plan.v4"
    model = "openai:qwen3.7-plus"
    candidate_id = $CandidateId
    candidate_dir = $candidateDir
    strict_acceptance = $strictPath
    strict_decision = $strictDecision
    strict_acceptance_recomputed = $strictArtifactRecomputed
    strict_acceptance_artifact_present = $strictArtifactPresent
    strict_acceptance_artifact_malformed = $strictArtifactMalformed
    readiness_blockers = @($readinessBlockers)
    ready_to_execute = $readyToExecute
    readiness = $readiness
    sealed_manifest = $manifest
    sealed_manifest_sha256 = $sealedManifestHash
    preregistration = $preregistration
    preregistration_sha256 = $preregistrationHash
    primary_endpoint = $primaryEndpointContract
    statistical_design = $statisticalDesignContract
    missingness_policy = $missingnessPolicyContract
    multiplicity = $multiplicityContract
    sealed_config = $sealedConfig
    sealed_config_sha256 = $sealedConfigHash
    baseline_surface_sha256 = $baselineHash
    candidate_surface_sha256 = $candidateHash
    strict_acceptance_sha256 = $strictHash
    strict_baseline_result = $cleanBaselineResult
    strict_candidate_result = $candidateCleanResult
    strict_baseline_result_sha256 = $cleanBaselineResultHash
    strict_candidate_result_sha256 = $cleanCandidateResultHash
    baseline_run_identity = $baselineRunIdentity
    candidate_run_identity = $candidateRunIdentity
    execution_code = $executionCode
    analysis_code = $analysisCode
    execution_environment = $environmentSnapshot
    dependency_lock = $dependencyLock
    container_resolution_output = $containerResolutionPath
    tasks = $sealedTasks
    repeats = $sealedRepeats
    infrastructure_retries = $sealedInfrastructureRetries
    case_concurrency = $sealedCaseConcurrency
    timeout_seconds = $sealedTimeoutSeconds
    evaluation_cells = $sealedEvaluationCells
    configured_outer_timeout_upper_hours = $sealedBudgetHours
    baseline_output = $baselineOutput
    candidate_output = $candidateOutput
    analysis_output = $analysisOutput
}
New-Item -ItemType Directory -Force (Split-Path -Parent $planPath) | Out-Null
$planJson = $plan | ConvertTo-Json -Depth 5
Write-Utf8NoBom -Path $planPath -Value $planJson

if (-not $Execute) {
    Write-Output "Dry-run only; no model calls were made."
    Write-Output "ready_to_execute: $readyToExecute"
    Write-Output "strict_decision: $strictDecision"
    Write-Output "strict_acceptance_recomputed: $($readiness.strict_acceptance_recomputed)"
    Write-Output "readiness_blockers: $($readinessBlockers -join ',')"
    Write-Output "clean_git_worktree: $(-not $gitDirty)"
    Write-Output "harbor_available: $([bool]$harborCommand)"
    Write-Output "docker_available: $dockerAvailable"
    Write-Output "api_key_available: $apiKeyAvailable"
    Write-Output "dependency_lock_available: $([bool]$dependencyLock)"
    Write-Output "dependency_bundle_sha256: $dependencyBundleHash"
    Write-Output "evaluation_cells: $sealedEvaluationCells"
    Write-Output "configured outer-timeout upper bound: $sealedBudgetHours h"
    Write-Output "plan: $planPath"
    exit 0
}

if (-not $strictReady) {
    throw "sealed execution blocked: strict acceptance decision is '$strictDecision', expected 'accepted'"
}

if (-not $harborCommand) { throw "Harbor CLI is not available" }
if (-not $dockerAvailable) { throw "Docker client/server version check failed" }
if (-not $dependencyLock) { throw "Python/Harbor dependency lock is unavailable" }
if ($gitDirty -and -not $AllowDirtyWorktree) {
    throw "sealed execution blocked: Git worktree is dirty; commit/archive the source snapshot or explicitly pass -AllowDirtyWorktree"
}

$apiKey = [Environment]::GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "SELF_HARNESS_QWEN_API_KEY is missing or blank in the Windows user environment"
}
docker info --format "{{.ServerVersion}}" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker is not available" }

$freeze = [ordered]@{
    format = "self_harness.sealed_freeze.v3"
    model = "openai:qwen3.7-plus"
    candidate_id = $CandidateId
    candidate_dir = $candidateDir
    sealed_manifest_sha256 = $sealedManifestHash
    preregistration = $preregistration
    preregistration_sha256 = $preregistrationHash
    primary_endpoint = $primaryEndpointContract
    statistical_design = $statisticalDesignContract
    missingness_policy = $missingnessPolicyContract
    multiplicity = $multiplicityContract
    sealed_config = $sealedConfig
    sealed_config_sha256 = $sealedConfigHash
    baseline_surface = $baselineSurface
    baseline_surface_sha256 = $baselineHash
    candidate_surface = $candidateSurface
    candidate_surface_sha256 = $candidateHash
    strict_acceptance = $strictPath
    strict_acceptance_sha256 = $strictHash
    strict_baseline_result = $cleanBaselineResult
    strict_candidate_result = $candidateCleanResult
    strict_baseline_result_sha256 = $cleanBaselineResultHash
    strict_candidate_result_sha256 = $cleanCandidateResultHash
    execution_code = $executionCode
    analysis_code = $analysisCode
    execution_environment = $environmentSnapshot
    dependency_lock = $dependencyLock
}
$freezeJson = $freeze | ConvertTo-Json -Depth 5
if (Test-Path -LiteralPath $freezePath) {
    $existing = Read-StrictJsonObject $freezePath
    if ($existing.candidate_id -ne $CandidateId -or
        $existing.candidate_dir -ne $candidateDir -or
        $existing.candidate_surface -ne $candidateSurface -or
        $existing.candidate_surface_sha256 -ne $candidateHash -or
        $existing.baseline_surface_sha256 -ne $baselineHash -or
        $existing.sealed_manifest_sha256 -ne $sealedManifestHash -or
        $existing.preregistration_sha256 -ne $preregistrationHash -or
        $existing.sealed_config_sha256 -ne $sealedConfigHash -or
        $existing.execution_code.bundle_sha256 -ne $executionCodeHash -or
        $existing.analysis_code.bundle_sha256 -ne $analysisCodeHash -or
        $existing.dependency_lock.bundle_sha256 -ne $dependencyBundleHash -or
        $existing.strict_acceptance_sha256 -ne $strictHash -or
        $existing.strict_baseline_result_sha256 -ne $cleanBaselineResultHash -or
        $existing.strict_candidate_result_sha256 -ne $cleanCandidateResultHash) {
        throw "sealed freeze mismatch; refusing to run a changed harness or split"
    }
} else {
    Write-Utf8NoBom -Path $freezePath -Value $freezeJson
}

function Invoke-SealedPair {
    $env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")
    $env:OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    $env:SELF_HARNESS_MODEL = "openai:qwen3.7-plus"
    $env:SELF_HARNESS_TB2_ROOT = $taskRoot
    $env:PYTHONUTF8 = "1"
    Remove-Item Env:SELF_HARNESS_CANDIDATE_WORKSPACE -ErrorAction SilentlyContinue
    & $python $runner --config $sealedConfig --output-dir $baselineOutput --reuse-existing `
        --run-identity $baselineRunIdentity --expected-config-sha256 $sealedConfigHash
    if ($LASTEXITCODE -ne 0) { throw "sealed baseline failed with exit code $LASTEXITCODE" }
    $env:SELF_HARNESS_CANDIDATE_WORKSPACE = $candidateDir
    & $python $runner --config $sealedConfig --output-dir $candidateOutput --reuse-existing `
        --run-identity $candidateRunIdentity --expected-config-sha256 $sealedConfigHash
    if ($LASTEXITCODE -ne 0) { throw "sealed candidate failed with exit code $LASTEXITCODE" }
    & $python $containerResolutionScript `
        --task-root $taskRoot `
        --manifest $manifest `
        --docker-executable $dockerCommand.Source `
        --output $containerResolutionPath
    if ($LASTEXITCODE -ne 0) { throw "sealed container resolution capture failed" }
    & $python $analyzer `
        --baseline-result (Join-Path $baselineOutput "result.json") `
        --candidate-result (Join-Path $candidateOutput "result.json") `
        --manifest $manifest `
        --freeze $freezePath `
        --container-resolution $containerResolutionPath `
        --output-dir $analysisOutput
    if ($LASTEXITCODE -ne 0) { throw "sealed analysis failed; check unresolved invalid cells" }
}

if ($Foreground) {
    Invoke-SealedPair
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stdout = Join-Path $root "runs\sealed21-qwen-$stamp.stdout.log"
$stderr = Join-Path $root "runs\sealed21-qwen-$stamp.stderr.log"
$command = @"
`$ErrorActionPreference = "Stop"
`$env:OPENAI_API_KEY = [Environment]::GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")
`$env:OPENAI_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
`$env:SELF_HARNESS_MODEL = "openai:qwen3.7-plus"
`$env:SELF_HARNESS_TB2_ROOT = "$taskRoot"
`$env:PYTHONUTF8 = "1"
Remove-Item Env:SELF_HARNESS_CANDIDATE_WORKSPACE -ErrorAction SilentlyContinue
Set-Location -LiteralPath "$root"
& "$python" "$runner" --config "$sealedConfig" --output-dir "$baselineOutput" --reuse-existing --run-identity "$baselineRunIdentity" --expected-config-sha256 "$sealedConfigHash"
if (`$LASTEXITCODE -ne 0) { throw "sealed baseline failed" }
`$env:SELF_HARNESS_CANDIDATE_WORKSPACE = "$candidateDir"
& "$python" "$runner" --config "$sealedConfig" --output-dir "$candidateOutput" --reuse-existing --run-identity "$candidateRunIdentity" --expected-config-sha256 "$sealedConfigHash"
if (`$LASTEXITCODE -ne 0) { throw "sealed candidate failed" }
& "$python" "$containerResolutionScript" --task-root "$taskRoot" --manifest "$manifest" --docker-executable "$($dockerCommand.Source)" --output "$containerResolutionPath"
if (`$LASTEXITCODE -ne 0) { throw "sealed container resolution capture failed" }
& "$python" "$analyzer" --baseline-result "$baselineOutput\result.json" --candidate-result "$candidateOutput\result.json" --manifest "$manifest" --freeze "$freezePath" --container-resolution "$containerResolutionPath" --output-dir "$analysisOutput"
if (`$LASTEXITCODE -ne 0) { throw "sealed analysis failed; check unresolved invalid cells" }
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$process = Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru

Write-Output "Sealed21 pair started in background (PID: $($process.Id))"
Write-Output "Do not inspect either result until both runs finish."
Write-Output "stdout: $stdout"
Write-Output "stderr: $stderr"
