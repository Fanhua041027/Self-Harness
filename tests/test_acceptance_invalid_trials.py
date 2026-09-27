from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_acceptance_module():
    path = Path(__file__).resolve().parents[1] / "acceptance" / "scripts" / "run_acceptance_gate.py"
    spec = importlib.util.spec_from_file_location("self_harness_acceptance_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def minimal_result_payload(*, passed: bool) -> dict:
    return {
        "splits": {
            split: [
                {
                    "repeat": repeat,
                    "passed": int(passed),
                    "total": 1,
                    "case_results": [
                        {
                            "case_id": f"{split}-{repeat}",
                            "status": "passed" if passed else "failed",
                            "passed": passed,
                            "reward": 1.0 if passed else 0.0,
                        }
                    ],
                }
                for repeat in (1, 2)
            ]
            for split in ("train", "heldout")
        }
    }


def test_acceptance_requires_exact_repeat_ids() -> None:
    gate = load_acceptance_module()
    payload = minimal_result_payload(passed=True)
    payload["splits"]["train"][0]["repeat"] = 10
    payload["splits"]["train"][1]["repeat"] = 11
    with pytest.raises(ValueError, match="repeat ids"):
        gate.run_acceptance_gate(
            baseline_result=payload,
            candidate_result=payload,
            splits=("train",),
        )


def test_acceptance_rejects_baseline_candidate_case_set_mismatch() -> None:
    gate = load_acceptance_module()
    baseline = minimal_result_payload(passed=True)
    candidate = minimal_result_payload(passed=True)
    candidate["splits"]["train"][0]["case_results"][0]["case_id"] = "replacement"
    with pytest.raises(ValueError, match="case sets are not identical"):
        gate.run_acceptance_gate(
            baseline_result=baseline,
            candidate_result=candidate,
            splits=("train",),
        )
def test_invalid_infrastructure_trial_cannot_enter_acceptance() -> None:
    gate = load_acceptance_module()
    repeat = {
        "repeat": 1,
        "passed": 0,
        "total": 1,
        "case_results": [{"case_id": "sample", "status": "invalid", "passed": False}],
    }

    with pytest.raises(ValueError, match="invalid infrastructure trials: sample"):
        gate.repeat_metric(repeat, split="heldout")


def test_legacy_provider_failure_cannot_enter_acceptance() -> None:
    gate = load_acceptance_module()
    repeat = {
        "repeat": 1,
        "passed": 0,
        "total": 1,
        "case_results": [
            {
                "case_id": "legacy-outage",
                "status": "failed",
                "passed": False,
                "reward": None,
                "trace_metadata": {"failure": "APIConnectionError: Connection error."},
            }
        ],
    }

    with pytest.raises(ValueError, match="legacy-outage.*APIConnectionError"):
        gate.repeat_metric(repeat, split="train")


def test_acceptance_loader_uses_shared_bom_and_duplicate_key_rules(tmp_path: Path) -> None:
    gate = load_acceptance_module()
    valid = tmp_path / "valid.json"
    valid.write_text('{"format": "fixture"}', encoding="utf-8-sig")
    assert gate.read_json(valid) == {"format": "fixture"}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"outer": {"status": "failed", "status": "invalid"}}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key: status"):
        gate.read_json(duplicate)


def test_write_json_replaces_artifact_atomically_and_cleans_temporary_files(tmp_path: Path) -> None:
    gate = load_acceptance_module()
    output = tmp_path / "acceptance.strict.json"
    output.write_text('{"old": true}\n', encoding="utf-8")
    gate.write_json(output, {"new": True})
    assert output.read_text(encoding="utf-8") == '{\n  "new": true\n}\n'
    assert list(tmp_path.glob(".acceptance.strict.json.*.tmp")) == []


def test_acceptance_gate_strictly_requires_reward_field() -> None:
    gate = load_acceptance_module()
    repeat = {
        "repeat": 1,
        "passed": 0,
        "total": 1,
        "case_results": [{"case_id": "missing-reward", "status": "failed", "passed": False}],
    }
    with pytest.raises(ValueError, match="missing-reward.*missing verifier reward"):
        gate.repeat_metric(repeat, split="train")


def test_acceptance_gate_rejects_aggregate_passed_mismatch() -> None:
    gate = load_acceptance_module()
    repeat = {
        "repeat": 1,
        "passed": 1,
        "total": 1,
        "case_results": [{"case_id": "failed-case", "status": "failed", "passed": False, "reward": 0.0}],
    }
    with pytest.raises(ValueError, match="aggregate mismatch.*passed=1.*case sum=0"):
        gate.repeat_metric(repeat, split="train")


def test_acceptance_artifact_binds_source_result_hashes(tmp_path: Path) -> None:
    gate = load_acceptance_module()
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_bytes(b"baseline-result\n")
    candidate_path.write_bytes(b"candidate-result\n")
    payload = {
        "splits": {
            "train": [
                {
                    "repeat": 1,
                    "passed": 1,
                    "total": 1,
                    "case_results": [
                        {"case_id": "train-1", "status": "passed", "passed": True, "reward": 1.0}
                    ],
                },
                {
                    "repeat": 2,
                    "passed": 1,
                    "total": 1,
                    "case_results": [
                        {"case_id": "train-2", "status": "passed", "passed": True, "reward": 1.0}
                    ],
                },
            ],
            "heldout": [
                {
                    "repeat": 1,
                    "passed": 0,
                    "total": 1,
                    "case_results": [
                        {"case_id": "heldout-1", "status": "failed", "passed": False, "reward": 0.0}
                    ],
                },
                {
                    "repeat": 2,
                    "passed": 0,
                    "total": 1,
                    "case_results": [
                        {"case_id": "heldout-2", "status": "failed", "passed": False, "reward": 0.0}
                    ],
                },
            ],
        }
    }
    baseline_path.write_text(json.dumps({**payload, "role": "baseline"}), encoding="utf-8")
    candidate_path.write_text(json.dumps({**payload, "role": "candidate"}), encoding="utf-8")
    result = gate.run_acceptance_gate(
        baseline_result=payload,
        candidate_result=payload,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
        baseline_result_sha256=gate.sha256_file(baseline_path),
        candidate_result_sha256=gate.sha256_file(candidate_path),
        source_hashes_stable=True,
    )

    assert result["format"] == "self_harness.acceptance_gate.v1"
    assert result["baseline_result_sha256"] == gate.sha256_file(baseline_path)
    assert result["candidate_result_sha256"] == gate.sha256_file(candidate_path)
    assert result["source_hashes_stable"] is True

    artifact_path = tmp_path / "acceptance.strict.json"
    artifact_path.write_text(json.dumps(result), encoding="utf-8")
    assert gate.main(
        [
            "--baseline-result",
            str(baseline_path),
            "--candidate-result",
            str(candidate_path),
            "--verify-artifact",
            str(artifact_path),
        ]
    ) == 0

    result["reason"] = "tampered summary"
    artifact_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="reason does not match recomputed gate"):
        gate.verify_acceptance_artifact(
            artifact=result,
            baseline_result_path=baseline_path,
            candidate_result_path=candidate_path,
        )


def test_acceptance_stable_reader_rejects_hash_change(monkeypatch, tmp_path: Path) -> None:
    gate = load_acceptance_module()
    result_path = tmp_path / "result.json"
    result_path.write_text('{"splits": {}}', encoding="utf-8")
    values = iter(("before", "after"))
    monkeypatch.setattr(gate, "sha256_file", lambda _path: next(values))

    with pytest.raises(ValueError, match="changed while acceptance input was being read"):
        gate.read_json_with_stable_hash(result_path)


def test_acceptance_verifier_rejects_artifact_change_during_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gate = load_acceptance_module()
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(minimal_result_payload(passed=False)), encoding="utf-8")
    candidate_path.write_text(json.dumps(minimal_result_payload(passed=True)), encoding="utf-8")
    artifact_path = tmp_path / "acceptance.strict.json"
    artifact = gate.run_acceptance_gate(
        baseline_result=gate.read_json(baseline_path),
        candidate_result=gate.read_json(candidate_path),
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
        baseline_result_sha256=gate.sha256_file(baseline_path),
        candidate_result_sha256=gate.sha256_file(candidate_path),
        source_hashes_stable=True,
    )
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    artifact_hash = gate.sha256_file(artifact_path)
    original_gate = gate.run_acceptance_gate

    def mutate_artifact(*args, **kwargs):
        result = original_gate(*args, **kwargs)
        artifact_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")
        return result

    monkeypatch.setattr(gate, "run_acceptance_gate", mutate_artifact)
    with pytest.raises(ValueError, match="acceptance artifact changed during acceptance evaluation"):
        gate.verify_acceptance_artifact(
            artifact=artifact,
            baseline_result_path=baseline_path,
            candidate_result_path=candidate_path,
            artifact_path=artifact_path,
            artifact_sha256=artifact_hash,
        )
