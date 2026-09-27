from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from paper import analyze_experiments, analyze_sealed
from eval.scripts.capture_environment_lock import dependency_bundle_sha256
from eval.scripts.capture_container_resolution import resolution_bundle_sha256


TASKS = [f"sealed-{index:02d}" for index in range(21)]


def test_load_json_accepts_legacy_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "legacy-powershell.json"
    path.write_text(json.dumps({"format": "fixture"}), encoding="utf-8-sig")

    assert analyze_sealed.load_json(path) == {"format": "fixture"}


def test_load_json_rejects_duplicate_nested_key(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"outer": {"passed": false, "passed": true}}', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="duplicate JSON key: passed"):
        analyze_sealed.load_json(path)


def test_sealed_rendering_requires_confirmatory_statistics() -> None:
    blocked = {
        "inference_valid": False,
        "inference_blocked_reason": "unresolved invalid",
        "cluster_bootstrap_ci_low": None,
        "cluster_bootstrap_ci_high": None,
        "mcnemar_exact_p": None,
        "task_permutation_p": None,
    }
    with pytest.raises(ValueError, match="complete confirmatory statistics"):
        analyze_sealed.require_confirmatory_stats(blocked)


def test_sealed_analysis_status_distinguishes_incomplete_from_complete() -> None:
    assert analyze_sealed.sealed_analysis_status({"inference_valid": False}) == "incomplete"
    assert analyze_sealed.sealed_analysis_status({"inference_valid": True}) == "complete"


def test_validate_sealed_statistics_payload_rejects_status_drift() -> None:
    payload = {
        "analysis_status": "incomplete",
        "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False,
        "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {
            "classification": "infrastructure-invalid is neither pass nor behavioral fail",
            "reruns": 2,
            "replacement_tasks": False,
            "imputation": False,
            "completion_rule": "both harnesses must have zero unresolved invalid cells",
            "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
        },
        "multiplicity": {
            "primary_hypotheses": 1,
            "adjustment": "none",
            "rationale": "one pre-registered model, split, contrast, and primary endpoint",
        },
        "primary": {
            "inference_valid": False,
            "baseline_invalid": 1,
            "final_invalid": 0,
            "mcnemar_exact_p": None,
            "task_permutation_p": None,
            "cluster_bootstrap_ci_low": None,
            "cluster_bootstrap_ci_high": None,
        },
        "result_integrity": {
            "baseline_unresolved_invalid": 1,
            "candidate_unresolved_invalid": 0,
            "baseline_invalid_keys": [["sealed", 1, "task"]],
            "candidate_invalid_keys": [],
        },
        "categories": [],
    }
    analyze_sealed.validate_sealed_statistics_payload(payload)
    payload["primary"]["task_permutation_p"] = 0.01
    with pytest.raises(ValueError, match="null confirmatory"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_delta_drift() -> None:
    payload = {
        "analysis_status": "incomplete",
        "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False,
        "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {
            "classification": "infrastructure-invalid is neither pass nor behavioral fail",
            "reruns": 2, "replacement_tasks": False, "imputation": False,
            "completion_rule": "both harnesses must have zero unresolved invalid cells",
            "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
        },
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "baseline_invalid": 1, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 1, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [["sealed", 1, "task"]], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 2, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 1.0}, "delta": 0.4}],
    }
    with pytest.raises(ValueError, match="category delta"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_total_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "tasks": 2, "attempts": 4, "baseline_passed": 2, "final_passed": 2, "baseline_invalid": 1, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 1, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [["sealed", 1, "task"]], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 2, "baseline_invalid": 1, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 1.0}, "delta": 0.5}],
    }
    with pytest.raises(ValueError, match="do not sum to primary tasks"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_passed_total_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 2, "final_passed": 2, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 2, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 1.0}, "delta": 0.5}],
    }
    with pytest.raises(ValueError, match="baseline_passed does not sum to primary baseline_passed"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_missing_categories_for_positive_primary() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [],
    }
    with pytest.raises(ValueError, match="categories must not be empty"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_attempt_design_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "design": {"attempts_per_task": 2, "roles": ["baseline", "candidate"]},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 3, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 3, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 1, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 1 / 3, "candidate": 1 / 3}, "delta": 0.0}],
    }
    with pytest.raises(ValueError, match="do not match attempts_per_task"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_role_metadata_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "design": {"attempts_per_task": 2, "roles": ["control", "treatment"]},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 1, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 0.5}, "delta": 0.0}],
    }
    with pytest.raises(ValueError, match="design roles are malformed"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_comparison_unit_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "design": {"comparison_unit": "attempt", "attempts_per_task": 2, "roles": ["baseline", "candidate"]},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 1, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 0.5}, "delta": 0.0}],
    }
    with pytest.raises(ValueError, match="design comparison_unit is malformed"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_observation_unit_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "design": {"comparison_unit": "task", "observation_unit": "task", "attempts_per_task": 2, "roles": ["baseline", "candidate"]},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 1, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 0.5}, "delta": 0.0}],
    }
    with pytest.raises(ValueError, match="design observation_unit is malformed"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_bootstrap_design_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "design": {"comparison_unit": "task", "observation_unit": "attempt", "attempts_per_task": 2, "roles": ["baseline", "candidate"], "bootstrap": {"unit": "task", "resamples": 1000, "seed": 7, "percentile_method": "wrong"}},
        "primary": {"inference_valid": False, "tasks": 1, "attempts": 2, "baseline_passed": 1, "final_passed": 1, "baseline_invalid": 0, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 0, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 1, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 0.5}, "delta": 0.0}],
    }
    with pytest.raises(ValueError, match="bootstrap design is malformed"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_rate_pass_count_drift() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "baseline_invalid": 1, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 1, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [["sealed", 1, "task"]], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 0, "candidate_passed": 2, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 1.0}, "delta": 0.5}],
    }
    with pytest.raises(ValueError, match="rates disagree with passed counts"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_validate_sealed_statistics_payload_rejects_category_pass_invalid_overlap() -> None:
    payload = {
        "analysis_status": "incomplete", "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False, "primary_endpoint_alpha": 0.05,
        "primary_success_rule": analyze_sealed.PRIMARY_SUCCESS_RULE,
        "missingness_policy": {"classification": "infrastructure-invalid is neither pass nor behavioral fail", "reruns": 2, "replacement_tasks": False, "imputation": False, "completion_rule": "both harnesses must have zero unresolved invalid cells", "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete"},
        "multiplicity": {"primary_hypotheses": 1, "adjustment": "none", "rationale": "one pre-registered model, split, contrast, and primary endpoint"},
        "primary": {"inference_valid": False, "baseline_invalid": 1, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 1, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [["sealed", 1, "task"]], "candidate_invalid_keys": []},
        "categories": [{"category": "demo", "task_clusters": 1, "attempts": 2, "comparison_unit": "task", "baseline_passed": 1, "candidate_passed": 2, "baseline_invalid": 2, "candidate_invalid": 0, "role_rates": {"baseline": 0.5, "candidate": 1.0}, "delta": 0.5}],
    }
    with pytest.raises(ValueError, match="passed and invalid counts overlap"):
        analyze_sealed.validate_sealed_statistics_payload(payload)


def test_category_rows_excludes_non_boolean_passed_from_descriptive_counts() -> None:
    manifest = {"tasks": [{"case_id": "demo", "category": "cat"}]}
    baseline_key = ("sealed", 1, "demo")
    candidate_key = ("sealed", 1, "demo")
    baseline = {baseline_key: {"passed": "true", "status": "invalid", "reward": 1.0}}
    candidate = {candidate_key: {"passed": True, "status": "passed", "reward": 1.0}}
    rows = analyze_sealed.category_rows(manifest=manifest, baseline=baseline, candidate=candidate)
    assert rows == [{
        "category": "cat", "attempts": 1, "task_clusters": 1, "comparison_unit": "task",
        "baseline_passed": 0, "candidate_passed": 1,
        "baseline_invalid": 1, "candidate_invalid": 0,
        "baseline_rate": 0.0, "candidate_rate": 1.0,
        "role_rates": {"baseline": 0.0, "candidate": 1.0}, "delta": 1.0,
    }]


def test_primary_success_uses_preregistered_alpha() -> None:
    stats = {
        "inference_valid": True,
        "delta": 0.1,
        "cluster_bootstrap_ci_low": 0.01,
        "task_permutation_p": 0.08,
    }
    assert analyze_sealed.evaluate_primary_success(stats, alpha=0.10) is True
    assert analyze_sealed.evaluate_primary_success(stats, alpha=0.05) is False


def test_incomplete_sealed_report_renders_na_without_none() -> None:
    stats = {
        "inference_valid": False,
        "inference_blocked_reason": "unresolved invalid",
        "baseline_rate": 0.25,
        "final_rate": 0.5,
        "delta": 0.25,
        "cluster_bootstrap_ci_low": None,
        "cluster_bootstrap_ci_high": None,
        "task_permutation_p": None,
        "baseline_invalid": 1,
        "final_invalid": 0,
        "improved_pairs": 2,
        "regressed_pairs": 0,
    }
    report = analyze_sealed.render_markdown(
        stats=stats,
        categories=[],
        success=False,
        container_resolution={"resolved_count": 0, "reference_count": 5, "complete": False},
        baseline_result_sha256="a" * 64,
        candidate_result_sha256="b" * 64,
        manifest_sha256="c" * 64,
        freeze_sha256="d" * 64,
        container_resolution_sha256="e" * 64,
        provenance={
            "preregistration_sha256": "f" * 64,
            "sealed_config_sha256": "0" * 64,
            "strict_acceptance_sha256": "1" * 64,
            "execution_code_bundle_sha256": "2" * 64,
            "analysis_code_bundle_sha256": "3" * 64,
            "dependency_bundle_sha256": "4" * 64,
            "baseline_surface_sha256": "5" * 64,
            "candidate_surface_sha256": "6" * 64,
        },
    )
    assert "analysis_status=`incomplete`" in report
    assert "primary_endpoint_alpha=0.05" in report
    assert f"primary_success_rule=`{analyze_sealed.PRIMARY_SUCCESS_RULE}`" in report
    assert "NA" in report
    assert "None" not in report
    assert "inference_blocked_reason=unresolved invalid" in report


def test_sealed_report_renders_nondefault_alpha_in_success_text() -> None:
    stats = {
        "inference_valid": False,
        "inference_blocked_reason": "unresolved invalid",
        "baseline_rate": None,
        "final_rate": None,
        "delta": None,
        "cluster_bootstrap_ci_low": None,
        "cluster_bootstrap_ci_high": None,
        "task_permutation_p": None,
        "baseline_invalid": 1,
        "final_invalid": 0,
        "improved_pairs": 0,
        "regressed_pairs": 0,
    }
    rule = "delta > 0, 95% CI lower bound > 0, and p < 0.10"
    report = analyze_sealed.render_markdown(
        stats=stats,
        categories=[],
        success=False,
        container_resolution={"resolved_count": 0, "reference_count": 0, "complete": False},
        baseline_result_sha256="a" * 64,
        candidate_result_sha256="b" * 64,
        manifest_sha256="c" * 64,
        freeze_sha256="d" * 64,
        container_resolution_sha256="e" * 64,
        provenance={"preregistration_sha256": "f" * 64, "sealed_config_sha256": "0" * 64, "strict_acceptance_sha256": "1" * 64, "execution_code_bundle_sha256": "2" * 64, "analysis_code_bundle_sha256": "3" * 64, "dependency_bundle_sha256": "4" * 64, "baseline_surface_sha256": "5" * 64, "candidate_surface_sha256": "6" * 64},
        primary_endpoint_alpha=0.10,
        primary_success_rule=rule,
    )
    assert "p < 0.10" in report
    assert "p < 0.05" not in report


def test_sealed_report_renders_design_from_metadata() -> None:
    stats = {
        "inference_valid": False, "inference_blocked_reason": "unresolved invalid",
        "baseline_rate": None, "final_rate": None, "delta": None,
        "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None,
        "task_permutation_p": None, "baseline_invalid": 1, "final_invalid": 0,
        "improved_pairs": 0, "regressed_pairs": 0,
    }
    design = {
        "split_task_counts": {"sealed": 3}, "repeat_ids": [1, 2, 3],
        "roles": ["control", "treatment"],
        "bootstrap": {"unit": "item", "resamples": 123, "seed": 77},
        "permutation": {"unit": "item", "sidedness": "one-sided", "exact": False, "aggregation": "median effect", "algorithm": "permutation sampler"},
    }
    report = analyze_sealed.render_markdown(
        stats=stats,
        categories=[{"category": "demo", "task_clusters": 1, "comparison_unit": "task", "attempts": 2, "baseline_invalid": 0, "candidate_invalid": 0, "role_rates": {"control": 0.25, "treatment": 0.75}, "delta": 0.5}],
        success=False,
        container_resolution={"resolved_count": 0, "reference_count": 0, "complete": False},
        baseline_result_sha256="a" * 64, candidate_result_sha256="b" * 64,
        manifest_sha256="c" * 64, freeze_sha256="d" * 64,
        container_resolution_sha256="e" * 64,
        provenance={"preregistration_sha256": "f" * 64, "sealed_config_sha256": "0" * 64, "strict_acceptance_sha256": "1" * 64, "execution_code_bundle_sha256": "2" * 64, "analysis_code_bundle_sha256": "3" * 64, "dependency_bundle_sha256": "4" * 64, "baseline_surface_sha256": "5" * 64, "candidate_surface_sha256": "6" * 64},
        design=design,
    )
    assert "3 个任务 × 3 次 repeat × 2 个角色 = 18 个评测单元" in report
    assert "Bootstrap 重采样=123 次，seed=77" in report
    assert "| control | treatment |" in report
    assert "item 聚类 95% CI" in report
    assert "item 置换 p" in report
    assert "unit=item" in report
    assert "sidedness=one-sided" in report
    assert "exact=False" in report
    assert "| demo | 1 | 2 | 25.00% | 75.00% | 0 | 0 | 50.00% |" in report


def test_validate_execution_environment_rejects_incomplete_snapshot() -> None:
    snapshot = {
        "captured_at": "2026-08-12T00:00:00+00:00",
        "os_description": "fixture OS",
        "os_architecture": "AMD64",
        "python_executable": "python",
        "python_version": "Python fixture",
        "harbor_executable": "harbor",
        "harbor_version": "0.20.0",
        "docker_available": True,
        "docker_client_version": "1",
        "docker_server_version": "",
        "git_head": "fixture-git-head",
        "git_dirty": False,
        "dirty_worktree_override": False,
        "model": "openai:test-model",
        "endpoint": "https://example.test/v1",
    }

    with pytest.raises(ValueError, match="docker_server_version"):
        analyze_sealed.validate_execution_environment_snapshot(
            snapshot, expected_model="openai:test-model"
        )


def test_require_stable_file_hash_rejects_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("{}", encoding="utf-8")
    expected = analyze_sealed.sha256_file(path)
    path.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="changed during analysis"):
        analyze_sealed.require_stable_file_hash(path, expected, "result")


def write_manifest(path: Path) -> None:
    task_root = path.parent / "sealed-tasks"
    task_root.mkdir()
    task_rows = []
    for index, case_id in enumerate(TASKS):
        task_dir = task_root / case_id
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(
            "[metadata]\ncategory = 'fixture'\ndifficulty = 'medium'\n", encoding="utf-8"
        )
        (task_dir / "environment").mkdir()
        (task_dir / "environment" / "Dockerfile").write_text(
            "FROM ubuntu:24.04\n", encoding="utf-8"
        )
        task_rows.append(
            {
                "case_id": case_id,
                "category": "fixture-a" if index < 10 else "fixture-b",
                "task_sha256": analyze_sealed.task_digest(task_dir),
            }
        )
    path.write_text(
        json.dumps(
            {
                "format": "self_harness.sealed_split.v1",
                "classification": "frozen-before-sealed-evaluation",
                "prior_result_references": 0,
                "task_count": 21,
                "task_root": str(task_root),
                "tasks": task_rows,
            }
        ),
        encoding="utf-8",
    )


def write_result(
    path: Path,
    *,
    passed: bool,
    status: str = "passed",
    tasks: list[str] | None = None,
    run_identity: str | None = None,
    config_sha256: str | None = None,
) -> None:
    selected = TASKS if tasks is None else tasks
    path.write_text(
        json.dumps(
            {
                "model": "openai:test-model",
                "run_identity": run_identity,
                "config_sha256": config_sha256,
                "splits": {
                    "sealed": [
                        {
                            "repeat": repeat,
                            "passed": len(selected) if passed else 0,
                            "total": len(selected),
                            "case_results": [
                                {
                                    "case_id": case_id,
                                    "repeat": repeat,
                                    "passed": passed,
                                    "reward": 1.0 if passed else 0.0,
                                    "status": status,
                                }
                                for case_id in selected
                            ],
                        }
                        for repeat in (1, 2)
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def write_clean_result(path: Path, *, passed: bool) -> None:
    """写入可供 acceptance gate 重算的最小 Clean64 结果。"""
    path.write_text(
        json.dumps(
            {
                "splits": {
                    split: [
                        {
                            "repeat": repeat,
                            "passed": 1 if passed else 0,
                            "total": 1,
                            "case_results": [
                                {
                                    "case_id": f"{split}-case",
                                    "repeat": repeat,
                                    "passed": passed,
                                    "reward": 1.0 if passed else 0.0,
                                    "status": "passed" if passed else "failed",
                                }
                            ],
                        }
                        for repeat in (1, 2)
                    ]
                    for split in ("train", "heldout")
                }
            }
        ),
        encoding="utf-8",
    )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bind_strict_artifact(tmp_path: Path, freeze: Path, baseline: Path, candidate: Path) -> None:
    strict = tmp_path / "acceptance.strict.json"
    strict_baseline = tmp_path / "strict-baseline.json"
    strict_candidate = tmp_path / "strict-candidate.json"
    strict.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v1",
                "accepted": True,
                "decision": "accepted",
                "reason": "accepted: improved train, heldout with no split drops",
                "source_hashes_stable": True,
                "rule": {
                    "splits": ["train", "heldout"],
                    "expected_repeats": 2,
                    "average_metric": "pass_rate",
                    "accept_if": "no split drops and at least one split improves",
                },
                "splits": {
                    split: {
                        "baseline_average_pass_rate": 0.0,
                        "candidate_average_pass_rate": 1.0,
                        "status": "improved",
                        "delta": 1.0,
                        "baseline_repeats": [
                            {"repeat": 1, "passed": 0, "total": 1, "pass_rate": 0.0},
                            {"repeat": 2, "passed": 0, "total": 1, "pass_rate": 0.0},
                        ],
                        "candidate_repeats": [
                            {"repeat": 1, "passed": 1, "total": 1, "pass_rate": 1.0},
                            {"repeat": 2, "passed": 1, "total": 1, "pass_rate": 1.0},
                        ],
                    }
                    for split in ("train", "heldout")
                },
                "baseline_result": str(strict_baseline.resolve()),
                "candidate_result": str(strict_candidate.resolve()),
                "baseline_result_sha256": file_hash(strict_baseline),
                "candidate_result_sha256": file_hash(strict_candidate),
            }
        ),
        encoding="utf-8",
    )
    freeze_payload = json.loads(freeze.read_text(encoding="utf-8"))
    freeze_payload["strict_acceptance_sha256"] = file_hash(strict)
    freeze_payload["strict_baseline_result"] = str(strict_baseline.resolve())
    freeze_payload["strict_candidate_result"] = str(strict_candidate.resolve())
    freeze_payload["strict_baseline_result_sha256"] = file_hash(strict_baseline)
    freeze_payload["strict_candidate_result_sha256"] = file_hash(strict_candidate)
    freeze.write_text(json.dumps(freeze_payload), encoding="utf-8")


def write_freeze(tmp_path: Path, manifest: Path) -> tuple[Path, str, str, str]:
    baseline_surface = tmp_path / "baseline.py"
    candidate_surface = tmp_path / "candidate.py"
    strict = tmp_path / "acceptance.strict.json"
    strict_baseline = tmp_path / "strict-baseline.json"
    strict_candidate = tmp_path / "strict-candidate.json"
    preregistration = tmp_path / "preregistration.json"
    sealed_config = tmp_path / "sealed.toml"
    baseline_surface.write_text("BASELINE = True\n", encoding="utf-8")
    candidate_surface.write_text("CANDIDATE = True\n", encoding="utf-8")
    strict.write_text(json.dumps({"decision": "accepted"}), encoding="utf-8")
    write_clean_result(strict_baseline, passed=False)
    write_clean_result(strict_candidate, passed=True)
    preregistration.write_text(
        json.dumps(
            {
                "format": "self_harness.ei_confirmation_preregistration.v1",
                "status": "frozen-before-sealed-evaluation",
                "primary_model": "openai:test-model",
                "primary_candidate": "fixture",
                "sealed_split": {
                    "manifest": str(manifest.resolve()),
                    "manifest_sha256": file_hash(manifest),
                    "config": str(sealed_config.resolve()),
                    "tasks": 21,
                    "attempts_per_harness": 42,
                    "outcome_blind": True,
                },
                "analysis_integrity": {
                    "bootstrap_seed": 20260812,
                    "permutation_implementation": "two-sided exact task-level sign permutation via dynamic programming over rational task effects",
                },
                "primary_endpoint": {
                    "name": "paired Sealed21 pass-rate difference",
                    "contrast": "frozen candidate minus baseline",
                    "unit": "Terminal-Bench task",
                    "metric": "verifier pass fraction over two attempts per task",
                    "confidence_interval": "task-clustered nonparametric bootstrap, 20000 resamples",
                    "hypothesis_test": "two-sided task-level paired sign permutation test",
                    "alpha": 0.05,
                    "success_rule": "delta > 0, 95% CI lower bound > 0, and p < 0.05",
                },
                "statistical_design": {
                    "comparison_unit": "task",
                    "observation_unit": "attempt",
                    "split_task_counts": {"sealed": 21},
                    "repeat_ids": [1, 2],
                    "attempts_per_task": 2,
                    "roles": ["baseline", "candidate"],
                    "bootstrap": {
                        "unit": "task",
                        "resamples": 20000,
                        "seed": 20260812,
                        "seed_derivation": "fixed preregistered bootstrap seed",
                        "percentile_method": "linear interpolation on sorted samples, q*(n-1) (Hyndman-Fan type 7)",
                    },
                    "permutation": {
                        "unit": "task",
                        "sidedness": "two-sided",
                        "exact": True,
                        "aggregation": "mean signed pass difference across attempts within task",
                        "algorithm": "dynamic programming over rational task effects",
                    },
                },
                "missingness_policy": {
                    "classification": "infrastructure-invalid is neither pass nor behavioral fail",
                    "reruns": 2,
                    "replacement_tasks": False,
                    "imputation": False,
                    "completion_rule": "both harnesses must have zero unresolved invalid cells",
                    "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
                },
                "multiplicity": {
                    "primary_hypotheses": 1,
                    "adjustment": "none",
                    "rationale": "one pre-registered model, split, contrast, and primary endpoint",
                },
            }
        ),
        encoding="utf-8",
    )
    sealed_config.write_text("[eval]\nname = 'fixture'\n", encoding="utf-8")
    manifest_hash = file_hash(manifest)
    baseline_hash = file_hash(baseline_surface)
    candidate_hash = file_hash(candidate_surface)
    preregistration_hash = file_hash(preregistration)
    config_hash = file_hash(sealed_config)
    code_root = tmp_path / "code"
    code_root.mkdir()

    def code_group(names: list[str]) -> dict:
        files = {}
        for name in names:
            source = code_root / f"{name}.py"
            source.write_text(f"NAME = {name!r}\n", encoding="utf-8")
            files[name] = {"path": str(source), "sha256": file_hash(source)}
        return {
            "files": files,
            "bundle_sha256": analyze_sealed.code_bundle_sha256(files),
        }

    execution_code = code_group(["backend_bridge", "harbor_wrapper", "run_harbor_eval"])
    analysis_code = code_group(
        ["acceptance_gate", "analyze_experiments", "analyze_sealed", "build_sealed_split", "result_validity"]
    )
    dependency_lock = {
        "format": "self_harness.environment_lock.v1",
        "captured_at": "2026-08-12T00:00:00+00:00",
        "project_python": {
            "executable": "project",
            "executable_sha256": "d" * 64,
            "version": "Python fixture",
            "package_count": 1,
            "packages": ["project==1"],
        },
        "harbor_python": {
            "executable": "harbor-python",
            "executable_sha256": "e" * 64,
            "version": "Python fixture",
            "package_count": 1,
            "packages": ["harbor==0.20.0"],
        },
        "harbor_cli": {
            "executable": "harbor",
            "executable_sha256": "f" * 64,
            "version": "0.20.0",
        },
    }
    dependency_lock["bundle_sha256"] = dependency_bundle_sha256(dependency_lock)
    baseline_identity = analyze_sealed.sealed_run_identity(
        manifest_sha256=manifest_hash,
        preregistration_sha256=preregistration_hash,
        config_sha256=config_hash,
        execution_code_sha256=execution_code["bundle_sha256"],
        dependency_bundle_sha256=dependency_lock["bundle_sha256"],
        role="baseline",
        surface_sha256=baseline_hash,
    )
    candidate_identity = analyze_sealed.sealed_run_identity(
        manifest_sha256=manifest_hash,
        preregistration_sha256=preregistration_hash,
        config_sha256=config_hash,
        execution_code_sha256=execution_code["bundle_sha256"],
        dependency_bundle_sha256=dependency_lock["bundle_sha256"],
        role="candidate",
        surface_sha256=candidate_hash,
    )
    freeze = tmp_path / "freeze.json"
    freeze.write_text(
        json.dumps(
            {
                "format": "self_harness.sealed_freeze.v3",
                "model": "openai:test-model",
                "candidate_id": "fixture",
                "sealed_manifest_sha256": manifest_hash,
                "preregistration": str(preregistration),
                "preregistration_sha256": preregistration_hash,
                "primary_endpoint": {
                    "name": "paired Sealed21 pass-rate difference",
                    "contrast": "frozen candidate minus baseline",
                    "unit": "Terminal-Bench task",
                    "metric": "verifier pass fraction over two attempts per task",
                    "confidence_interval": "task-clustered nonparametric bootstrap, 20000 resamples",
                    "hypothesis_test": "two-sided task-level paired sign permutation test",
                    "alpha": 0.05,
                    "success_rule": "delta > 0, 95% CI lower bound > 0, and p < 0.05",
                },
                "statistical_design": {
                    "comparison_unit": "task",
                    "observation_unit": "attempt",
                    "split_task_counts": {"sealed": 21},
                    "repeat_ids": [1, 2],
                    "attempts_per_task": 2,
                    "roles": ["baseline", "candidate"],
                    "bootstrap": {
                        "unit": "task",
                        "resamples": 20000,
                        "seed": 20260812,
                        "seed_derivation": "fixed preregistered bootstrap seed",
                        "percentile_method": "linear interpolation on sorted samples, q*(n-1) (Hyndman-Fan type 7)",
                    },
                    "permutation": {
                        "unit": "task",
                        "sidedness": "two-sided",
                        "exact": True,
                        "aggregation": "mean signed pass difference across attempts within task",
                        "algorithm": "dynamic programming over rational task effects",
                    },
                },
                "missingness_policy": {
                    "classification": "infrastructure-invalid is neither pass nor behavioral fail",
                    "reruns": 2,
                    "replacement_tasks": False,
                    "imputation": False,
                    "completion_rule": "both harnesses must have zero unresolved invalid cells",
                    "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
                },
                "multiplicity": {
                    "primary_hypotheses": 1,
                    "adjustment": "none",
                    "rationale": "one pre-registered model, split, contrast, and primary endpoint",
                },
                "sealed_config": str(sealed_config),
                "sealed_config_sha256": config_hash,
                "baseline_surface": str(baseline_surface),
                "baseline_surface_sha256": baseline_hash,
                "candidate_surface": str(candidate_surface),
                "candidate_surface_sha256": candidate_hash,
                "strict_acceptance": str(strict),
                "strict_acceptance_sha256": file_hash(strict),
                "strict_baseline_result": str(strict_baseline),
                "strict_candidate_result": str(strict_candidate),
                "strict_baseline_result_sha256": file_hash(strict_baseline),
                "strict_candidate_result_sha256": file_hash(strict_candidate),
                "execution_code": execution_code,
                "analysis_code": analysis_code,
                "execution_environment": {
                    "captured_at": "2026-08-12T00:00:00+00:00",
                    "os_description": "fixture OS",
                    "os_architecture": "AMD64",
                    "python_executable": "python",
                    "python_version": "Python fixture",
                    "harbor_executable": "harbor",
                    "harbor_version": "0.20.0",
                    "docker_available": True,
                    "docker_client_version": "1",
                    "docker_server_version": "1",
                    "git_head": "fixture-git-head",
                    "git_dirty": False,
                    "dirty_worktree_override": False,
                    "model": "openai:test-model",
                    "endpoint": "https://example.test/v1",
                },
                "dependency_lock": dependency_lock,
            }
        ),
        encoding="utf-8",
    )
    return freeze, baseline_identity, candidate_identity, config_hash


def write_container_resolution(tmp_path: Path, case_ids: list[str] | None = None) -> Path:
    selected = sorted(TASKS if case_ids is None else case_ids)
    snapshot = {
        "format": "self_harness.container_resolution.v1",
        "captured_at": "fixture",
        "outcome_blind": True,
        "pull_performed": False,
        "docker_executable": "docker",
        "docker_client_version": "1",
        "docker_server_version": "1",
        "case_ids": selected,
        "reference_count": 1,
        "resolved_count": 1,
        "complete": True,
        "images": [
            {
                "reference": "ubuntu:24.04",
                "used_by": [{"case_id": selected[0], "dockerfile": "environment/Dockerfile", "line": 1}],
                "found": True,
                "image_id": "sha256:" + "1" * 64,
                "repo_digests": ["ubuntu@sha256:" + "2" * 64],
                "repo_tags": ["ubuntu:24.04"],
                "created": "fixture",
                "os": "linux",
                "architecture": "amd64",
            }
        ],
    }
    snapshot["bundle_sha256"] = resolution_bundle_sha256(snapshot)
    path = tmp_path / "container-resolution.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def test_main_reports_pre_registered_success(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "output"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    container_resolution = write_container_resolution(tmp_path)
    write_result(
        baseline,
        passed=False,
        status="failed",
        run_identity=baseline_identity,
        config_sha256=config_hash,
    )
    write_result(
        candidate,
        passed=True,
        run_identity=candidate_identity,
        config_sha256=config_hash,
    )
    bind_strict_artifact(tmp_path, freeze, baseline, candidate)

    assert (
        analyze_sealed.main(
            [
                "--baseline-result",
                str(baseline),
                "--candidate-result",
                str(candidate),
                "--manifest",
                str(manifest),
                "--freeze",
                str(freeze),
                "--container-resolution",
                str(container_resolution),
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads((output / "sealed_statistics.json").read_text(encoding="utf-8"))
    assert payload["pre_registered_success"] is True
    assert payload["missingness_policy"]["imputation"] is False
    assert payload["multiplicity"]["primary_hypotheses"] == 1
    assert payload["provenance_verified"] is True
    assert payload["strict_acceptance_verified"] is True
    assert payload["strict_acceptance_decision"] == "accepted"
    assert payload["container_resolution_complete"] is True
    assert payload["baseline_result_sha256"] == file_hash(baseline)
    assert payload["candidate_result_sha256"] == file_hash(candidate)
    assert payload["manifest_sha256"] == file_hash(manifest)
    assert payload["freeze_sha256"] == file_hash(freeze)
    assert payload["container_resolution_sha256"] == file_hash(container_resolution)
    assert payload["analysis_input_integrity"]["stable_before_after_validation"] is True
    assert payload["format"] == "self_harness.sealed_analysis.v6"
    assert payload["design"] == {
        "comparison_unit": "task",
        "observation_unit": "attempt",
        "split_task_counts": {"sealed": 21},
        "repeat_ids": [1, 2],
        "attempts_per_task": 2,
        "roles": ["baseline", "candidate"],
        "bootstrap": {
            "unit": "task",
            "resamples": 20000,
            "seed": 20260812,
            "seed_derivation": "fixed preregistered bootstrap seed",
            "percentile_method": "linear interpolation on sorted samples, q*(n-1) (Hyndman-Fan type 7)",
        },
        "permutation": {
            "unit": "task",
            "sidedness": "two-sided",
            "exact": True,
            "aggregation": "mean signed pass difference across attempts within task",
            "algorithm": "dynamic programming over rational task effects",
        },
    }
    assert payload["result_integrity"]["baseline_unresolved_invalid"] == 0
    assert payload["result_integrity"]["candidate_unresolved_invalid"] == 0
    assert payload["primary"]["attempts"] == 42
    assert payload["primary"]["tasks"] == 21
    assert payload["primary"]["delta"] == 1.0
    assert (output / "SEALED_REPORT.md").is_file()
    report = (output / "SEALED_REPORT.md").read_text(encoding="utf-8")
    assert "21 个任务 × 2 次 repeat × 2 个角色 = 84 个评测单元" in report
    assert "Bootstrap 重采样=20000 次，seed=20260812" in report
    assert "9.53674e-07" in report
    assert payload["baseline_result_sha256"] in report
    assert payload["candidate_result_sha256"] in report
    assert payload["manifest_sha256"] in report
    assert payload["freeze_sha256"] in report
    assert payload["container_resolution_sha256"] in report
    for field in (
        "preregistration_sha256",
        "sealed_config_sha256",
        "strict_acceptance_sha256",
        "execution_code_bundle_sha256",
        "analysis_code_bundle_sha256",
        "dependency_bundle_sha256",
        "baseline_surface_sha256",
        "candidate_surface_sha256",
    ):
        assert payload["provenance"][field] in report
    assert (output / "sealed_categories.csv").is_file()


def test_validate_frozen_provenance_rejects_statistical_contract_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    freeze, _baseline_identity, _candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    payload = json.loads(freeze.read_text(encoding="utf-8"))
    payload["primary_endpoint"]["alpha"] = 0.10
    freeze.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="primary_endpoint contract"):
        analyze_sealed.validate_frozen_provenance(
            freeze_path=freeze,
            manifest_path=manifest,
            baseline_payload={"config_sha256": config_hash},
            candidate_payload={"config_sha256": config_hash},
        )


def test_main_rejects_manifest_changed_during_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "output"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    container_resolution = write_container_resolution(tmp_path)
    write_result(
        baseline,
        passed=False,
        status="failed",
        run_identity=baseline_identity,
        config_sha256=config_hash,
    )
    write_result(
        candidate,
        passed=True,
        run_identity=candidate_identity,
        config_sha256=config_hash,
    )
    bind_strict_artifact(tmp_path, freeze, baseline, candidate)
    original_analyze_split = analyze_sealed.analyze_split

    def mutate_manifest_during_statistics(*args: object, **kwargs: object) -> dict:
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return original_analyze_split(*args, **kwargs)

    monkeypatch.setattr(analyze_sealed, "analyze_split", mutate_manifest_during_statistics)
    with pytest.raises(ValueError, match="sealed manifest changed during analysis"):
        analyze_sealed.main(
            [
                "--baseline-result",
                str(baseline),
                "--candidate-result",
                str(candidate),
                "--manifest",
                str(manifest),
                "--freeze",
                str(freeze),
                "--container-resolution",
                str(container_resolution),
                "--output-dir",
                str(output),
            ]
        )


def test_fmt_p_preserves_small_exact_values() -> None:
    assert analyze_sealed.fmt_p(2 / (2**21)) == "9.53674e-07"
    assert analyze_sealed.fmt_p(0.03125) == "0.0312"
    with pytest.raises(ValueError, match=r"within \[0, 1\]"):
        analyze_sealed.fmt_p(1.1)


def test_main_rejects_preregistration_changed_during_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    output = tmp_path / "output"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    container_resolution = write_container_resolution(tmp_path)
    write_result(baseline, passed=False, status="failed", run_identity=baseline_identity, config_sha256=config_hash)
    write_result(candidate, passed=True, run_identity=candidate_identity, config_sha256=config_hash)
    bind_strict_artifact(tmp_path, freeze, baseline, candidate)
    preregistration = tmp_path / "preregistration.json"
    original_analyze_split = analyze_sealed.analyze_split

    def mutate_preregistration_during_statistics(*args: object, **kwargs: object) -> dict:
        preregistration.write_text(
            preregistration.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        return original_analyze_split(*args, **kwargs)

    monkeypatch.setattr(analyze_sealed, "analyze_split", mutate_preregistration_during_statistics)
    with pytest.raises(ValueError, match="preregistration changed during analysis"):
        analyze_sealed.main(
            [
                "--baseline-result", str(baseline),
                "--candidate-result", str(candidate),
                "--manifest", str(manifest),
                "--freeze", str(freeze),
                "--container-resolution", str(container_resolution),
                "--output-dir", str(output),
            ]
        )


def test_frozen_provenance_rejects_swapped_run_identity(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    write_result(
        baseline,
        passed=False,
        status="failed",
        run_identity=candidate_identity,
        config_sha256=config_hash,
    )
    write_result(
        candidate,
        passed=True,
        run_identity=baseline_identity,
        config_sha256=config_hash,
    )

    with pytest.raises(ValueError, match="baseline run identity"):
        analyze_sealed.validate_frozen_provenance(
            freeze_path=freeze,
            manifest_path=manifest,
            baseline_payload=json.loads(baseline.read_text(encoding="utf-8")),
            candidate_payload=json.loads(candidate.read_text(encoding="utf-8")),
        )


def test_validate_preregistration_rejects_semantic_design_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads(
        (tmp_path / "preregistration.json").read_text(encoding="utf-8")
    )
    preregistration["sealed_split"]["tasks"] = 20

    with pytest.raises(ValueError, match="21 tasks and 42 attempts"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_statistical_contract_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads(
        (tmp_path / "preregistration.json").read_text(encoding="utf-8")
    )
    preregistration["primary_endpoint"]["alpha"] = 0.10

    with pytest.raises(ValueError, match="primary endpoint drift: alpha"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_primary_endpoint_semantic_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads((tmp_path / "preregistration.json").read_text(encoding="utf-8"))
    preregistration["primary_endpoint"]["contrast"] = "baseline minus frozen candidate"

    with pytest.raises(ValueError, match="primary endpoint drift: contrast"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_multiplicity_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads((tmp_path / "preregistration.json").read_text(encoding="utf-8"))
    preregistration["multiplicity"]["adjustment"] = "holm"

    with pytest.raises(ValueError, match="multiplicity contract drift"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_missingness_classification_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads((tmp_path / "preregistration.json").read_text(encoding="utf-8"))
    preregistration["missingness_policy"]["classification"] = "invalid is behavioral fail"

    with pytest.raises(ValueError, match="missingness policy contract drift"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_missingness_failure_rule_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads((tmp_path / "preregistration.json").read_text(encoding="utf-8"))
    preregistration["missingness_policy"]["failure_rule"] = "drop invalid cells"

    with pytest.raises(ValueError, match="missingness policy contract drift"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_validate_preregistration_rejects_structured_statistical_design_drift(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    write_freeze(tmp_path, manifest)
    preregistration = json.loads((tmp_path / "preregistration.json").read_text(encoding="utf-8"))
    preregistration["statistical_design"]["attempts_per_task"] = 3

    with pytest.raises(ValueError, match="statistical design drift"):
        analyze_sealed.validate_preregistration(
            preregistration,
            model="openai:test-model",
            candidate_id="fixture",
            manifest_path=manifest,
            manifest_sha256=file_hash(manifest),
            config_path=tmp_path / "sealed.toml",
        )


def test_sealed_statistical_design_uses_shared_contract() -> None:
    clean = analyze_experiments.statistical_design_metadata()
    sealed = analyze_sealed.sealed_statistical_design_metadata(21)
    for field in ("comparison_unit", "observation_unit", "repeat_ids", "attempts_per_task", "permutation"):
        assert sealed[field] == clean[field]
    assert sealed["bootstrap"]["unit"] == clean["bootstrap"]["unit"]
    assert sealed["bootstrap"]["resamples"] == clean["bootstrap"]["resamples"]
    assert sealed["bootstrap"]["seed"] == clean["bootstrap"]["seed"]
    assert sealed["bootstrap"]["percentile_method"] == clean["bootstrap"]["percentile_method"]
    assert sealed["split_task_counts"] == {"sealed": 21}
    assert sealed["roles"] == ["baseline", "candidate"]


def test_validate_strict_acceptance_rejects_internal_binding_drift(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text("{}", encoding="utf-8")
    candidate.write_text("{}", encoding="utf-8")
    strict = tmp_path / "acceptance.strict.json"
    strict.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v1",
                "accepted": False,
                "decision": "accepted",
                "source_hashes_stable": True,
                "rule": {
                    "splits": ["train", "heldout"],
                    "expected_repeats": 2,
                    "average_metric": "pass_rate",
                    "accept_if": "no split drops and at least one split improves",
                },
                "splits": {
                    split: {
                        "status": "improved",
                        "delta": 1.0,
                        "baseline_repeats": [{"repeat": 1}, {"repeat": 2}],
                        "candidate_repeats": [{"repeat": 1}, {"repeat": 2}],
                    }
                    for split in ("train", "heldout")
                },
                "baseline_result": str(baseline),
                "candidate_result": str(candidate),
                "baseline_result_sha256": file_hash(baseline),
                "candidate_result_sha256": file_hash(candidate),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="accepted/decision mismatch"):
        analyze_sealed.validate_strict_acceptance_artifact(
            path=strict,
            baseline_result_path=baseline,
            candidate_result_path=candidate,
        )


def test_validate_strict_acceptance_rejects_recomputed_summary_drift(tmp_path: Path) -> None:
    strict_baseline = tmp_path / "strict-baseline.json"
    strict_candidate = tmp_path / "strict-candidate.json"
    write_clean_result(strict_baseline, passed=False)
    write_clean_result(strict_candidate, passed=True)
    freeze = tmp_path / "freeze.json"
    freeze.write_text("{}", encoding="utf-8")
    bind_strict_artifact(tmp_path, freeze, strict_baseline, strict_candidate)
    strict = tmp_path / "acceptance.strict.json"
    artifact = json.loads(strict.read_text(encoding="utf-8"))
    artifact["reason"] = "tampered summary"
    strict.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="reason does not match recomputed gate"):
        analyze_sealed.validate_strict_acceptance_artifact(
            path=strict,
            baseline_result_path=strict_baseline,
            candidate_result_path=strict_candidate,
        )


def test_validate_strict_acceptance_rejects_missing_split_structure(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text("{}", encoding="utf-8")
    candidate.write_text("{}", encoding="utf-8")
    strict = tmp_path / "acceptance.strict.json"
    strict.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v1",
                "accepted": True,
                "decision": "accepted",
                "source_hashes_stable": True,
                "rule": {
                    "splits": ["train", "heldout"],
                    "expected_repeats": 2,
                    "average_metric": "pass_rate",
                    "accept_if": "no split drops and at least one split improves",
                },
                "splits": {"train": {}},
                "baseline_result": str(baseline),
                "candidate_result": str(candidate),
                "baseline_result_sha256": file_hash(baseline),
                "candidate_result_sha256": file_hash(candidate),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="train and heldout comparisons"):
        analyze_sealed.validate_strict_acceptance_artifact(
            path=strict,
            baseline_result_path=baseline,
            candidate_result_path=candidate,
        )


def test_validate_sealed_manifest_rejects_wrong_design_size() -> None:
    manifest = {
        "format": "self_harness.sealed_split.v1",
        "classification": "frozen-before-sealed-evaluation",
        "prior_result_references": 0,
        "task_count": 20,
        "tasks": [{"case_id": f"task-{index}"} for index in range(20)],
    }

    with pytest.raises(ValueError, match="exactly 21 tasks"):
        analyze_sealed.validate_sealed_manifest(manifest)


def test_validate_sealed_manifest_rejects_duplicate_ids() -> None:
    tasks = [{"case_id": f"task-{index}"} for index in range(21)]
    tasks[-1]["case_id"] = tasks[0]["case_id"]
    manifest = {
        "format": "self_harness.sealed_split.v1",
        "classification": "frozen-before-sealed-evaluation",
        "prior_result_references": 0,
        "task_count": 21,
        "tasks": tasks,
    }

    with pytest.raises(ValueError, match="duplicate task IDs"):
        analyze_sealed.validate_sealed_manifest(manifest)


def test_frozen_provenance_rejects_task_content_mutation(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    write_result(
        baseline, passed=False, status="failed", run_identity=baseline_identity, config_sha256=config_hash
    )
    write_result(
        candidate, passed=True, run_identity=candidate_identity, config_sha256=config_hash
    )
    task_file = tmp_path / "sealed-tasks" / TASKS[0] / "task.toml"
    task_file.write_text(task_file.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="task hash does not match"):
        analyze_sealed.validate_frozen_provenance(
            freeze_path=freeze,
            manifest_path=manifest,
            baseline_payload=json.loads(baseline.read_text(encoding="utf-8")),
            candidate_payload=json.loads(candidate.read_text(encoding="utf-8")),
        )


def test_frozen_provenance_rejects_execution_code_mutation(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    write_manifest(manifest)
    freeze, baseline_identity, candidate_identity, config_hash = write_freeze(tmp_path, manifest)
    write_result(
        baseline, passed=False, status="failed", run_identity=baseline_identity, config_sha256=config_hash
    )
    write_result(
        candidate, passed=True, run_identity=candidate_identity, config_sha256=config_hash
    )
    freeze_payload = json.loads(freeze.read_text(encoding="utf-8"))
    runner_path = Path(freeze_payload["execution_code"]["files"]["run_harbor_eval"]["path"])
    runner_path.write_text(runner_path.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="execution_code.run_harbor_eval"):
        analyze_sealed.validate_frozen_provenance(
            freeze_path=freeze,
            manifest_path=manifest,
            baseline_payload=json.loads(baseline.read_text(encoding="utf-8")),
            candidate_payload=json.loads(candidate.read_text(encoding="utf-8")),
        )


def test_validate_result_returns_unresolved_invalid_for_incomplete_analysis(tmp_path: Path) -> None:
    result = tmp_path / "invalid.json"
    write_result(result, passed=False, status="invalid")

    rows, invalid_keys = analyze_sealed.validate_result(
        result_path=result,
        expected_tasks=set(TASKS),
        expected_repeats=2,
    )
    assert len(rows) == 42
    assert len(invalid_keys) == 42


def test_validate_result_rejects_non_boolean_passed_field(tmp_path: Path) -> None:
    result = tmp_path / "malformed.json"
    write_result(result, passed=False, status="failed")
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["splits"]["sealed"][0]["case_results"][0]["passed"] = "false"
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="passed field must be boolean"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


def test_validate_result_rejects_inconsistent_repeat_aggregate(tmp_path: Path) -> None:
    result = tmp_path / "aggregate-mismatch.json"
    write_result(result, passed=True)
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["splits"]["sealed"][0]["passed"] = 0
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="repeat aggregate is inconsistent"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


def test_validate_result_rejects_missing_verifier_reward(tmp_path: Path) -> None:
    result = tmp_path / "missing-reward.json"
    write_result(result, passed=False, status="failed")
    payload = json.loads(result.read_text(encoding="utf-8"))
    for repeat in payload["splits"]["sealed"]:
        for case in repeat["case_results"]:
            case.pop("reward", None)
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="verifier reward is missing"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


@pytest.mark.parametrize("malformed_reward", [float("nan"), float("inf"), float("-inf")])
def test_validate_result_rejects_non_finite_verifier_reward(
    tmp_path: Path, malformed_reward: float
) -> None:
    result = tmp_path / "non-finite-reward.json"
    write_result(result, passed=False, status="failed")
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["splits"]["sealed"][0]["case_results"][0]["reward"] = malformed_reward
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="reward must be finite numeric"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


def test_validate_result_rejects_outer_case_repeat_mismatch(tmp_path: Path) -> None:
    result = tmp_path / "repeat-mismatch.json"
    write_result(result, passed=False, status="failed")
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["splits"]["sealed"][1]["case_results"][0]["repeat"] = 1
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="case repeat does not match"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


def test_validate_result_rejects_exact_key_mismatch(tmp_path: Path) -> None:
    result = tmp_path / "missing.json"
    write_result(result, passed=True, tasks=TASKS[:-1])

    with pytest.raises(ValueError, match="key mismatch"):
        analyze_sealed.validate_result(
            result_path=result,
            expected_tasks=set(TASKS),
            expected_repeats=2,
        )


def test_container_resolution_rejects_sealed_task_mismatch(tmp_path: Path) -> None:
    resolution = write_container_resolution(tmp_path, TASKS[:-1])

    with pytest.raises(ValueError, match="task set does not match"):
        analyze_sealed.validate_container_resolution_evidence(
            path=resolution, expected_tasks=set(TASKS)
        )


def test_container_resolution_rejects_reference_set_mismatch(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_manifest(manifest)
    resolution = write_container_resolution(tmp_path)

    with pytest.raises(ValueError, match="references do not match"):
        analyze_sealed.validate_container_resolution_evidence(
            path=resolution,
            expected_tasks=set(TASKS),
            expected_references={"python:3.13-slim"},
        )
