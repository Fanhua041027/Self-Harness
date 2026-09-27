from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "workflow" / "scripts" / "run_qwen_sealed21.ps1"


def test_sealed_launcher_writes_python_json_without_bom() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "New-Object System.Text.UTF8Encoding($false)" in source
    assert "[System.IO.File]::WriteAllText($Path, $Value, $encoding)" in source
    assert "Write-Utf8NoBom -Path $planPath -Value $planJson" in source
    assert "Write-Utf8NoBom -Path $freezePath -Value $freezeJson" in source
    assert "Set-Content -Encoding UTF8 $planPath" not in source
    assert "Set-Content -Encoding UTF8 $freezePath" not in source
    assert "[string]::IsNullOrWhiteSpace(" in source
    assert 'GetEnvironmentVariable("SELF_HARNESS_QWEN_API_KEY", "User")' in source
    assert "missing or blank" in source
    assert "Read-StrictJsonObject" in source
    assert "--emit-object" in source
    assert "Get-Content -Raw -Encoding UTF8 $queuePath | ConvertFrom-Json" not in source
    assert "$sealedDesignScript" in source
    assert "$sealedDesignScript | & $python - $sealedConfig" in source
    assert "tomllib" in source
    assert "$sealedEvaluationCells" in source
    assert "$sealedBudgetHours" in source
    assert "function Resolve-QueuePath" in source
    assert "function Assert-PathUnder" in source


def test_sealed_launcher_binds_strict_acceptance_to_current_clean64_results() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "$cleanBaselineResult" in source
    assert "$candidateCleanResult" in source
    assert "candidate_dir = $candidateDir" in source
    assert "strict_baseline_result_sha256" in source
    assert "strict_candidate_result_sha256" in source
    assert "strict_acceptance_result_binding" in source
    assert "strict_acceptance_boolean_consistent" in source
    assert "strict_acceptance_source_hashes_stable" in source
    assert "strict_baseline_result = $cleanBaselineResult" in source
    assert "strict_candidate_result = $candidateCleanResult" in source
    assert "$cleanBaselineResultHash" in source
    assert "$cleanCandidateResultHash" in source
    assert "$strictSourceHashesStable" in source
    assert "$strictArtifactRecomputed" in source
    assert "$strictArtifactPresent" in source
    assert "$strictArtifactMalformed" in source
    assert "strict_acceptance_artifact_malformed" in source
    assert "strict_acceptance_artifact_present = $strictArtifactPresent" in source
    assert "$strictAcceptedFlag" in source
    assert "$strictReady = $strictDecision -eq \"accepted\" -and" in source
    assert "$readinessBlockers" in source
    assert "readiness_blockers = @($readinessBlockers)" in source
    assert "--verify-artifact $strictPath" in source
    assert "--expected-repeats 2" in source
    assert 'Write-Output "strict_acceptance_recomputed: $($readiness.strict_acceptance_recomputed)"' in source
    assert 'Write-Output "readiness_blockers: $($readinessBlockers -join' in source
    assert "Resolve-Path -LiteralPath ([string]$strict.baseline_result)" in source
    assert "Resolve-Path -LiteralPath ([string]$strict.candidate_result)" in source
    assert "$candidateManifestPath = Join-Path $candidateDir \"manifest.json\"" in source
    assert "candidate manifest id does not match requested candidate" in source
    assert "$expectedCandidateResult = Resolve-QueuePath (Join-Path $candidateDir \"eval\\result.json\")" in source
    assert "candidate queue eval_result is not bound to candidate manifest directory" in source
    assert "$existing.candidate_id -ne $CandidateId" in source
    assert "$existing.candidate_dir -ne $candidateDir" in source
    assert "$existing.candidate_surface -ne $candidateSurface" in source


def test_sealed_launcher_bundles_acceptance_gate_source() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "$acceptanceGatePath" in source
    assert "$acceptanceGateHash" in source
    assert "acceptance_gate = [ordered]@{ path = $acceptanceGatePath; sha256 = $acceptanceGateHash }" in source
    assert "acceptance_gate=$acceptanceGateHash" in source
