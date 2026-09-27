from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from acceptance.scripts import run_acceptance_gate


def load_analysis_module():
    path = Path(__file__).resolve().parents[1] / "paper" / "analyze_experiments.py"
    spec = importlib.util.spec_from_file_location("self_harness_experiment_analysis", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exact_mcnemar_matches_known_discordant_pairs() -> None:
    analysis = load_analysis_module()

    assert analysis.exact_mcnemar_p(11, 1) == 0.00634765625
    assert analysis.exact_mcnemar_p(0, 0) == 1.0


def test_shared_statistics_loader_rejects_duplicate_keys_and_accepts_bom(tmp_path: Path) -> None:
    analysis = load_analysis_module()
    valid = tmp_path / "valid.json"
    valid.write_text('{"format": "fixture"}', encoding="utf-8-sig")
    assert analysis.load_json(valid) == {"format": "fixture"}

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"outer": {"status": "failed", "status": "invalid"}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate JSON key: status"):
        analysis.load_json(duplicate)


def test_flatten_result_rejects_outer_case_repeat_mismatch(tmp_path: Path) -> None:
    analysis = load_analysis_module()
    path = tmp_path / "mismatch.json"
    path.write_text(
        json.dumps(
            {
                "splits": {
                    "train": [
                        {
                            "repeat": 1,
                            "case_results": [
                                {"case_id": "case", "repeat": 2, "passed": False}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="case repeat mismatch"):
        analysis.flatten_result(path)


def test_flatten_result_rejects_empty_case_id(tmp_path: Path) -> None:
    analysis = load_analysis_module()
    path = tmp_path / "empty-case.json"
    path.write_text(
        json.dumps(
            {
                "splits": {
                    "train": [
                        {
                            "repeat": 1,
                            "case_results": [{"case_id": "", "repeat": 1}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid case_id"):
        analysis.flatten_result(path)


def test_clean64_layout_requires_two_repeats_and_fixed_task_counts() -> None:
    analysis = load_analysis_module()
    rows = {
        (split, repeat, f"{split}-{index}"): {}
        for split, task_count in (("train", 43), ("heldout", 21))
        for repeat in (1, 2)
        for index in range(task_count)
    }
    analysis.validate_clean64_layout(rows, label="fixture")

    incomplete = dict(rows)
    incomplete.pop(("train", 2, "train-42"))
    with pytest.raises(ValueError, match="must contain 43 tasks per repeat"):
        analysis.validate_clean64_layout(incomplete, label="fixture")


def test_paired_result_keys_must_match_exactly() -> None:
    analysis = load_analysis_module()
    baseline = {("train", 1, "task-a"): {}}
    candidate = {("train", 1, "task-b"): {}}

    with pytest.raises(ValueError, match="paired result key mismatch"):
        analysis.validate_paired_result_keys(baseline, candidate, label="fixture")


def test_descriptive_rate_counts_non_boolean_passed_as_failure() -> None:
    analysis = load_analysis_module()

    assert analysis.passed_for_descriptive_rate({"passed": True}) is True
    assert analysis.passed_for_descriptive_rate({"passed": "true"}) is False
    assert analysis.passed_for_descriptive_rate({"passed": 1}) is False


def test_inference_fields_are_null_when_invalid_gate_is_incomplete() -> None:
    analysis = load_analysis_module()
    baseline = {
        ("train", 1, "task"): {"passed": False, "status": "failed", "reward": None},
    }
    final = {
        ("train", 1, "task"): {"passed": True, "status": "passed", "reward": 1.0},
    }

    row = analysis.analyze_split(baseline, final, "train", seed=20260812)

    assert row["inference_valid"] is False
    assert row["inference_blocked_reason"]
    assert row["mcnemar_exact_p"] is None
    assert row["task_permutation_p"] is None
    assert row["cluster_bootstrap_ci_low"] is None
    assert row["cluster_bootstrap_ci_high"] is None


def test_statistics_source_manifest_is_sorted_and_hashed() -> None:
    analysis = load_analysis_module()

    records = analysis.collect_source_files()
    paths = [record["path"] for record in records]
    assert paths == sorted(paths)
    assert "paper/analyze_experiments.py" in paths
    assert "eval/scripts/result_validity.py" in paths
    assert "acceptance/scripts/run_acceptance_gate.py" in paths
    assert all(len(record["sha256"]) == 64 for record in records)


def test_analysis_input_stability_rejects_changed_or_added_file() -> None:
    analysis = load_analysis_module()

    with pytest.raises(ValueError, match="changed during analysis"):
        analysis.require_stable_analysis_inputs(
            {"baseline.json": "a"}, {"baseline.json": "b"}
        )

    with pytest.raises(ValueError, match="changed during analysis"):
        analysis.require_stable_analysis_inputs(
            {"baseline.json": "a"},
            {"baseline.json": "a", "candidate.json": "c"},
        )


def test_percentile_uses_explicit_linear_type7_interpolation() -> None:
    analysis = load_analysis_module()

    assert analysis.percentile([0.0, 10.0], 0.25) == 2.5
    assert "Hyndman-Fan type 7" in analysis.BOOTSTRAP_PERCENTILE_METHOD


def test_bootstrap_rejects_nonpositive_sample_count() -> None:
    analysis = load_analysis_module()

    with pytest.raises(ValueError, match="samples must be positive"):
        analysis.clustered_bootstrap_ci(
            [("task", False, True)], seed=20260812, samples=0
        )


def test_task_level_permutation_keeps_repeats_clustered() -> None:
    analysis = load_analysis_module()
    pairs = [
        ("task-a", False, True),
        ("task-a", False, True),
        ("task-b", True, True),
        ("task-b", False, False),
    ]

    assert analysis.task_level_permutation_p(pairs) == 1.0


def test_task_level_permutation_is_exact_for_all_21_nonzero_tasks() -> None:
    analysis = load_analysis_module()
    pairs = [
        (f"task-{index:02d}", False, True)
        for index in range(21)
        for _ in range(2)
    ]

    assert analysis.task_level_permutation_p(pairs) == 2 / (2**21)


def test_checked_in_results_are_not_strict_gate_reproducible() -> None:
    analysis = load_analysis_module()

    for model, config in analysis.MODELS.items():
        rows = analysis.candidate_rows(model, config["queue"])
        assert rows
        assert all(not row["strict_gate_reproducible"] for row in rows)


def test_candidate_rows_prefers_strict_acceptance_artifact(tmp_path: Path) -> None:
    analysis = load_analysis_module()
    def result(passed: bool) -> dict:
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
                                "repeat": repeat,
                                "passed": passed,
                                "status": "passed" if passed else "failed",
                                "reward": 1.0 if passed else 0.0,
                            }
                        ],
                    }
                    for repeat in (1, 2)
                ]
                for split in ("train", "heldout")
            }
        }
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(result(False)), encoding="utf-8")
    candidate.write_text(json.dumps(result(True)), encoding="utf-8")
    candidate_dir = tmp_path / "candidate-dir"
    candidate_dir.mkdir()
    historical = candidate_dir / "acceptance.json"
    strict = candidate_dir / "acceptance.strict.json"
    historical.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v0",
                "baseline_result": str(baseline),
                "candidate_result": str(candidate),
                "decision": "rejected",
                "reason": "fixture",
                "splits": {"train": {"delta": 0.0}, "heldout": {"delta": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    strict_payload = run_acceptance_gate.run_acceptance_gate(
        baseline_result=run_acceptance_gate.read_json(baseline),
        candidate_result=run_acceptance_gate.read_json(candidate),
        baseline_result_path=baseline,
        candidate_result_path=candidate,
        baseline_result_sha256=run_acceptance_gate.sha256_file(baseline),
        candidate_result_sha256=run_acceptance_gate.sha256_file(candidate),
        source_hashes_stable=True,
    )
    strict.write_text(json.dumps(strict_payload), encoding="utf-8")
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "candidate",
                        "mechanism_family": "prompt_instruction",
                        "acceptance_result": str(historical),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = analysis.candidate_rows("model", queue)

    assert rows[0]["decision"] == "accepted"
    assert rows[0]["decision_source"] == "strict"
    assert rows[0]["acceptance_path"] == str(strict)

    tampered = json.loads(strict.read_text(encoding="utf-8"))
    tampered["reason"] = "tampered summary"
    strict.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="reason does not match recomputed gate"):
        analysis.candidate_rows("model", queue)


def test_candidate_rows_rejects_same_size_but_misaligned_case_keys(tmp_path: Path) -> None:
    analysis = load_analysis_module()

    def result(case_id: str) -> dict:
        return {
            "splits": {
                "train": [
                    {"repeat": 1, "case_results": [{"case_id": case_id, "repeat": 1, "passed": True}]}
                ],
                "heldout": [
                    {"repeat": 1, "case_results": [{"case_id": case_id, "repeat": 1, "passed": True}]}
                ],
            }
        }

    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(result("task-a")), encoding="utf-8")
    candidate.write_text(json.dumps(result("task-b")), encoding="utf-8")
    acceptance = tmp_path / "acceptance.json"
    acceptance.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v0",
                "decision": "rejected",
                "baseline_result": str(baseline),
                "candidate_result": str(candidate),
                "splits": {"train": {"delta": 0.0}, "heldout": {"delta": 0.0}},
                "reason": "fixture",
            }
        ),
        encoding="utf-8",
    )
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "candidate_id": "misaligned",
                        "mechanism_family": "fixture",
                        "acceptance_result": str(acceptance),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="paired result key mismatch"):
        analysis.candidate_rows("model", queue)


def test_candidate_rows_rejects_tampered_historical_acceptance_summary(tmp_path: Path) -> None:
    analysis = load_analysis_module()

    def result(passed: bool) -> dict:
        return {
            "splits": {
                split: [
                    {
                        "repeat": repeat,
                        "passed": int(passed),
                        "total": 1,
                        "case_results": [
                            {"case_id": f"{split}-{repeat}", "repeat": repeat, "passed": passed}
                        ],
                    }
                    for repeat in (1, 2)
                ]
                for split in ("train", "heldout")
            }
        }

    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(result(False)), encoding="utf-8")
    candidate.write_text(json.dumps(result(True)), encoding="utf-8")
    acceptance = tmp_path / "acceptance.json"
    valid = analysis.recompute_historical_acceptance(baseline, candidate)
    valid["reason"] = "tampered historical summary"
    acceptance.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v0",
                "baseline_result": str(baseline),
                "candidate_result": str(candidate),
                **valid,
            }
        ),
        encoding="utf-8",
    )
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {"candidates": [{"candidate_id": "tampered", "mechanism_family": "fixture", "acceptance_result": str(acceptance)}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="historical acceptance reason does not match"):
        analysis.candidate_rows("model", queue)
