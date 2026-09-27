from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "workflow" / "scripts" / "finalize_clean64_strict.ps1"


def test_strict_finalizer_keeps_one_strict_validation_chain() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    # 前置盘点不能退回兼容模式，否则缺失 reward 的单元会使用不同口径。
    assert "--count --require-reward" in source
    assert "$acceptanceScript" in source
    assert "acceptance.strict.json" in source
    assert "$analysisScript" in source
    assert "$sealedLauncher" in source
    assert "$paperAuditScript" in source
    assert "Sealed21 readiness plan refresh failed" in source
    assert "paper consistency audit failed after strict finalization" in source
    assert "Read-StrictJsonObject" in source
    assert "--emit-object" in source
    assert "Get-Content -Raw -Encoding UTF8 $queuePath | ConvertFrom-Json" not in source
    assert "$candidateManifestPath = Join-Path $candidateDir \"manifest.json\"" in source
    assert "candidate manifest id does not match requested candidate" in source
    assert "$expectedCandidateResult = Resolve-QueuePath (Join-Path $candidateDir \"eval\\result.json\")" in source
    assert "candidate queue eval_result is not bound to candidate manifest directory" in source
    assert "function Resolve-QueuePath" in source
    assert "function Assert-PathUnder" in source
    assert "outside the expected experiment workspace" in source


def test_strict_finalizer_does_not_overwrite_historical_acceptance() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "$strictOutput = Join-Path $candidateDir \"acceptance.strict.json\"" in source
    assert "--output $strictOutput" in source
    assert "--output $acceptance" not in source
