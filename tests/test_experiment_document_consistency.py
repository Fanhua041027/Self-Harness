from __future__ import annotations

import json
from pathlib import Path

from eval.scripts import capture_environment_lock
from paper import audit_paper_consistency, build_paired_rerun_plan

ROOT = Path(__file__).resolve().parents[1]


def test_sealed_protocol_does_not_claim_unresolved_invalid_cells_are_fixed() -> None:
    protocol = (ROOT / "paper" / "SEALED_PROTOCOL.md").read_text(encoding="utf-8")
    section = protocol.split("## 执行前置条件", 1)[1].split("## 执行约束", 1)[0]

    assert "必须在 Sealed 执行前全部解决" in section
    assert "当前尚未解决" in section
    assert "已解决" not in section


def test_sealed_design_counts_match_protocol_and_paper_sections() -> None:
    config = json.loads(
        (ROOT / "configs" / "experiments" / "ei_confirmation_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["sealed_split"]["tasks"] == 21
    assert config["sealed_split"]["attempts_per_harness"] == 42

    protocol = (ROOT / "paper" / "SEALED_PROTOCOL.md").read_text(encoding="utf-8")
    english = (ROOT / "paper" / "EXPERIMENTS_EN.md").read_text(encoding="utf-8")
    chinese = (ROOT / "paper" / "EXPERIMENTS_ZH.md").read_text(encoding="utf-8")
    assert "21 个任务 × 2 attempts，共 84 个评测单元" in protocol
    assert "84 statistical evaluation cells" in english
    assert "84 个统计评测单元" in chinese


def test_checked_in_qwen_rerun_plan_matches_authoritative_results() -> None:
    baseline = ROOT / "runs" / "clean64-qwen-baseline" / "result.json"
    candidate = (
        ROOT
        / "runs"
        / "clean64-qwen-self-harness"
        / "branches"
        / "baseline"
        / "candidates"
        / "anti_workaround_execution"
        / "eval"
        / "result.json"
    )
    generated = json.loads(
        (ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json").read_text(
            encoding="utf-8"
        )
    )
    rebuilt = build_paired_rerun_plan.build(
        baseline_path=baseline,
        candidate_path=candidate,
        timeout_s=3900,
        retries=2,
        concurrency=4,
    )
    for key in (
        "baseline_result_sha256",
        "candidate_result_sha256",
        "pair_counts",
        "incomplete_pairs",
        "rerun_cells",
        "reason_counts",
        "limits",
        "total_budget",
        "phases",
    ):
        assert generated[key] == rebuilt[key], key

    english = (ROOT / "paper" / "EXPERIMENTS_EN.md").read_text(encoding="utf-8")
    chinese = (ROOT / "paper" / "EXPERIMENTS_ZH.md").read_text(encoding="utf-8")
    baseline_invalid = rebuilt["pair_counts"]["baseline_invalid_only"] + rebuilt["pair_counts"]["both_invalid"]
    candidate_invalid = rebuilt["pair_counts"]["candidate_invalid_only"] + rebuilt["pair_counts"]["both_invalid"]
    assert f"resolve {baseline_invalid} baseline and {candidate_invalid} candidate" in english
    assert f"baseline {baseline_invalid} 个、candidate {candidate_invalid} 个有效 invalid 待全部解决" in chinese


def test_paper_numeric_tables_match_generated_statistics_and_plan() -> None:
    audit = audit_paper_consistency.run_audit()
    assert audit["passed"], [check for check in audit["checks"] if not check["ok"]]


def test_paper_audit_rejects_missing_stability_provenance() -> None:
    checks = audit_paper_consistency.provenance_checks(
        stats={"analysis_input_integrity": {"stable_before_after_analysis": False, "tracked_input_count": 0}},
        paired={"source_hashes_stable": False},
        phase_manifests=[
            (
                "phase.json",
                {
                    "format": "self_harness.rerun_cell_manifest.v2",
                    "source_hashes_stable": False,
                    "baseline_result_sha256": "short",
                    "candidate_result_sha256": "short",
                },
            )
        ],
        single_plans=[
            ("single.json", {"source_result_stable": False, "source_result_sha256": "short"})
        ],
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_non_null_inference_when_invalid_gate_blocks() -> None:
    stats = {
        "validity_policy": {"confirmatory_fields_null_when_inference_blocked": True},
        "summary": [
            {
                "inference_valid": False,
                "inference_blocked_reason": "unresolved invalid",
                "mcnemar_exact_p": 0.01,
                "task_permutation_p": None,
                "cluster_bootstrap_ci_low": None,
                "cluster_bootstrap_ci_high": None,
            }
        ],
    }
    checks = audit_paper_consistency.confirmatory_inference_checks(
        stats,
        "without confirmatory intervals or significance claims",
        "不报告确认性置信区间或显著性",
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_inference_flag_not_derived_from_invalid_counts() -> None:
    stats = {
        "validity_policy": {"confirmatory_fields_null_when_inference_blocked": True},
        "summary": [
            {
                "baseline_invalid": 0,
                "final_invalid": 0,
                "inference_valid": False,
                "inference_blocked_reason": "confirmatory inference withheld because baseline or final contains unresolved infrastructure-invalid cells",
                "mcnemar_exact_p": None,
                "task_permutation_p": None,
                "cluster_bootstrap_ci_low": None,
                "cluster_bootstrap_ci_high": None,
            }
        ],
    }
    checks = audit_paper_consistency.confirmatory_inference_checks(
        stats,
        "without confirmatory intervals or significance claims",
        "涓嶆姤鍛婄‘璁ゆ€х疆淇″尯闂存垨鏄捐憲鎬?",
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_valid_inference_with_unresolved_invalid_counts() -> None:
    stats = {
        "validity_policy": {"confirmatory_fields_null_when_inference_blocked": True},
        "summary": [
            {
                "baseline_invalid": 1,
                "final_invalid": 0,
                "inference_valid": True,
                "inference_blocked_reason": None,
                "mcnemar_exact_p": 0.5,
                "task_permutation_p": 0.5,
                "cluster_bootstrap_ci_low": -0.1,
                "cluster_bootstrap_ci_high": 0.1,
            }
        ],
    }
    checks = audit_paper_consistency.confirmatory_inference_checks(
        stats,
        "without confirmatory intervals or significance claims",
        "涓嶆姤鍛婄‘璁ゆ€х疆淇″尯闂存垨鏄捐憲鎬?",
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_stale_narrative_invalid_claim() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    english = (ROOT / "paper" / "EXPERIMENTS_EN.md").read_text(encoding="utf-8")
    chinese = (ROOT / "paper" / "EXPERIMENTS_ZH.md").read_text(encoding="utf-8")
    stale_english = english.replace("105/128 effective invalid cells", "104/128 effective invalid cells", 1)
    checks = audit_paper_consistency.narrative_claim_checks(stats, paired, stale_english, chinese)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_task_attempt_unit_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["summary"][0]["attempts"] = 43
    checks = audit_paper_consistency.statistical_unit_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_summary_value_not_rebuilt_from_raw_results() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["summary"][0]["baseline_passed"] += 1
    checks = audit_paper_consistency.statistics_summary_rebuild_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_statistical_configuration_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["bootstrap_samples"] = 10_000
    checks = audit_paper_consistency.statistical_configuration_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_statistical_design_metadata_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["statistical_design"]["attempts_per_task"] = 3
    checks = audit_paper_consistency.statistical_design_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_provenance_hash_mismatch() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    phase_paths = sorted((ROOT / "paper" / "generated" / "paired-rerun").glob("phase_*.json"))
    phase_manifests = [(path.name, audit_paper_consistency.load_json(path)) for path in phase_paths]
    single_paths = sorted((ROOT / "paper" / "generated" / "rerun-plans").glob("*.json"))
    single_plans = [(path.name, audit_paper_consistency.load_json(path)) for path in single_paths]
    mutated = json.loads(json.dumps(stats))
    mutated["source_files"][0]["sha256"] = "0" * 64
    checks = audit_paper_consistency.provenance_checks(mutated, paired, phase_manifests, single_plans)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_incomplete_statistics_source_inventory() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    phase_paths = sorted((ROOT / "paper" / "generated" / "paired-rerun").glob("phase_*.json"))
    phase_manifests = [(path.name, audit_paper_consistency.load_json(path)) for path in phase_paths]
    single_paths = sorted((ROOT / "paper" / "generated" / "rerun-plans").glob("*.json"))
    single_plans = [(path.name, audit_paper_consistency.load_json(path)) for path in single_paths]
    mutated = json.loads(json.dumps(stats))
    unrelated = ROOT / "README_REPRODUCTION.md"
    mutated["source_files"][0]["path"] = str(unrelated)
    mutated["source_files"][0]["sha256"] = audit_paper_consistency.sha256_file(unrelated)
    checks = audit_paper_consistency.provenance_checks(mutated, paired, phase_manifests, single_plans)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_inconsistent_canary_outcome_counts() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    paired = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json")
    payload = {
        "format": "self_harness.invalid_rerun_plan.v1",
        "selection": "cell_manifest",
        "selection_side": "baseline",
        "selection_phase": "phase_0_infrastructure_canary",
        "source_result": str(ROOT / "runs" / "clean64-qwen-baseline" / "result.json"),
        "source_result_sha256": audit_paper_consistency.sha256_file(ROOT / "runs" / "clean64-qwen-baseline" / "result.json"),
        "source_result_stable": True,
        "canary_outcome": {
            "selected_cell_count": 2,
            "numeric_verifier_outcome_count": 2,
            "infrastructure_invalid_count": 0,
            "missing_cell_count": 0,
            "behavioral_pass_count": 2,
            "behavioral_fail_count": 0,
            "canary_ready": True,
            "details": [],
        },
    }
    checks = audit_paper_consistency.provenance_checks(stats, paired, [], [("canary.json", payload)])
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_execution_invalid_count_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    paired = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json")
    source = ROOT / "runs" / "clean64-qwen-baseline" / "result.json"
    payload = {
        "format": "self_harness.invalid_rerun_plan.v1",
        "selection": "all_effective_invalid",
        "selection_side": None,
        "selection_phase": None,
        "source_result": str(source),
        "source_result_sha256": audit_paper_consistency.sha256_file(source),
        "source_result_stable": True,
        "cases": [],
        "execution_provenance": {
            "status": "completed",
            "evaluator_returncode": 0,
            "pre_source_result_sha256": audit_paper_consistency.sha256_file(source),
            "post_source_result_sha256": audit_paper_consistency.sha256_file(source),
            "post_source_result_stable": True,
            "selected_invalid_after": 0,
            "global_invalid_after": 0,
        },
    }
    checks = audit_paper_consistency.provenance_checks(stats, paired, [], [("executed.json", payload)])
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_missing_manifest_inventory() -> None:
    # run_audit 的清单数量门禁必须把缺失 phase/single 文件视为失败。
    checks = audit_paper_consistency.artifact_inventory_checks(5, 9)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_candidate_provenance_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["candidates"][0]["acceptance_path"] = str(ROOT / "missing-acceptance.json")
    checks = audit_paper_consistency.candidate_provenance_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_historical_acceptance_summary_drift(tmp_path: Path) -> None:
    def result(train_passed: int, heldout_passed: int) -> dict:
        def split(name: str, total: int, passed: int) -> list[dict]:
            rows = []
            for repeat in (1, 2):
                case_results = [
                    {"case_id": f"{name}-{repeat}-{index}", "repeat": repeat, "passed": index < passed}
                    for index in range(total)
                ]
                rows.append({"repeat": repeat, "passed": passed, "total": total, "case_results": case_results})
            return rows

        return {"splits": {"train": split("train", 2, train_passed), "heldout": split("heldout", 2, heldout_passed)}}

    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    acceptance_path = tmp_path / "acceptance.json"
    baseline_path.write_text(json.dumps(result(0, 0)), encoding="utf-8")
    candidate_path.write_text(json.dumps(result(0, 0)), encoding="utf-8")
    acceptance_path.write_text(
        json.dumps(
            {
                "format": "self_harness.acceptance_gate.v0",
                "accepted": True,
                "decision": "accepted",
                "reason": "accepted: improved train with no split drops",
                "rule": {
                    "splits": ["train", "heldout"],
                    "expected_repeats": 2,
                    "average_metric": "pass_rate",
                    "accept_if": "no split drops and at least one split improves",
                },
                "baseline_result": str(baseline_path),
                "candidate_result": str(candidate_path),
                "splits": {
                    split: {
                        "baseline_average_pass_rate": 0.0,
                        "candidate_average_pass_rate": 0.0,
                        "delta": 0.0,
                        "status": "unchanged",
                        "baseline_repeats": [
                            {"repeat": 1, "passed": 0, "total": 2, "pass_rate": 0.0},
                            {"repeat": 2, "passed": 0, "total": 2, "pass_rate": 0.0},
                        ],
                        "candidate_repeats": [
                            {"repeat": 1, "passed": 0, "total": 2, "pass_rate": 0.0},
                            {"repeat": 2, "passed": 0, "total": 2, "pass_rate": 0.0},
                        ],
                    }
                    for split in ("train", "heldout")
                },
            }
        ),
        encoding="utf-8",
    )
    stats = {
        "source_files": [
            {"path": str(path), "sha256": audit_paper_consistency.sha256_file(path)}
            for path in (acceptance_path, baseline_path, candidate_path)
        ],
        "candidates": [
            {
                "acceptance_path": str(acceptance_path),
                "decision": "accepted",
                "train_delta": 0.0,
                "heldout_delta": 0.0,
                "baseline_invalid": 0,
                "candidate_invalid": 0,
                "strict_gate_reproducible": True,
            }
        ],
    }
    checks = audit_paper_consistency.candidate_provenance_checks(stats)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_decision_source_label_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    mutated = json.loads(json.dumps(stats))
    mutated["candidates"][0]["decision_source"] = "strict"
    checks = audit_paper_consistency.candidate_provenance_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_paired_plan_semantic_drift() -> None:
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    mutated = json.loads(json.dumps(paired))
    mutated["pair_counts"]["both_valid"] += 1
    checks = audit_paper_consistency.paired_plan_semantic_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_phase_manifest_cell_drift() -> None:
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    phase_path = sorted((ROOT / "paper" / "generated" / "paired-rerun").glob("phase_*.json"))[0]
    manifest = audit_paper_consistency.load_json(phase_path)
    mutated = json.loads(json.dumps(manifest))
    mutated["cells"] = []
    checks = audit_paper_consistency.phase_manifest_semantic_checks(
        paired, [(phase_path.name, mutated)]
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_paired_derived_artifact_drift() -> None:
    paired = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json"
    )
    markdown = (ROOT / "paper" / "generated" / "paired-rerun" / "PAIRED_RERUN_PLAN.md").read_text(
        encoding="utf-8"
    )
    mutated_markdown = markdown.replace("待补跑 cell：183", "待补跑 cell：182", 1)
    checks = audit_paper_consistency.paired_derived_artifact_checks(
        paired, markdown=mutated_markdown, csv_rows=[]
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_statistics_derived_artifact_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    markdown = (ROOT / "paper" / "generated" / "STATISTICAL_AUDIT.md").read_text(encoding="utf-8")
    mutated_markdown = markdown.replace("Clean64 实验统计审计", "Clean64 实验统计审计（过期）", 1)
    checks = audit_paper_consistency.statistics_derived_artifact_checks(
        stats, summary_rows=[], candidate_rows=[], markdown=mutated_markdown
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_statistics_figure_drift() -> None:
    stats = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "statistics.json")
    effect_svg = (ROOT / "paper" / "generated" / "effect_sizes.svg").read_text(encoding="utf-8")
    invalid_svg = (ROOT / "paper" / "generated" / "invalid_runs.svg").read_text(encoding="utf-8")
    checks = audit_paper_consistency.statistics_figure_checks(
        stats,
        effect_svg=effect_svg.replace("Historical fixed-denominator effect", "stale figure", 1),
        invalid_svg=invalid_svg,
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_mechanism_figure_drift() -> None:
    svg_path = ROOT / "paper" / "generated" / "mechanism" / "mechanism_evidence.svg"
    svg = svg_path.read_text(encoding="utf-8")
    checks = audit_paper_consistency.supplementary_artifact_checks(
        mechanism_svg=svg.replace("Mechanism evidence chain", "stale mechanism figure", 1)
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_split_coverage_figure_drift() -> None:
    svg_path = ROOT / "paper" / "generated" / "split-coverage" / "split_coverage.svg"
    svg = svg_path.read_text(encoding="utf-8")
    checks = audit_paper_consistency.supplementary_artifact_checks(
        split_svg=svg.replace("Outcome-blind task-category coverage", "stale coverage figure", 1)
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_mechanism_source_hash_drift() -> None:
    mechanism = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "mechanism" / "mechanism_evidence.json"
    )
    mutated = json.loads(json.dumps(mechanism))
    mutated["source_files"][0]["sha256"] = "0" * 64
    checks = audit_paper_consistency.supplementary_provenance_checks(mechanism=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_split_source_hash_drift() -> None:
    split = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "split-coverage" / "split_coverage.json"
    )
    mutated = json.loads(json.dumps(split))
    mutated["source_files"][0]["sha256"] = "0" * 64
    checks = audit_paper_consistency.supplementary_provenance_checks(split=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_container_source_hash_drift() -> None:
    container = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "container-audit" / "container_reproducibility.json"
    )
    mutated = json.loads(json.dumps(container))
    mutated["source_files"][0]["sha256"] = "0" * 64
    checks = audit_paper_consistency.container_artifact_checks(container=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_design_source_hash_drift() -> None:
    design = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "design-audit" / "confirmation_design_audit.json"
    )
    mutated = json.loads(json.dumps(design))
    mutated["source_files"][0]["sha256"] = "0" * 64
    checks = audit_paper_consistency.design_artifact_checks(design=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_container_resolution_preview_tamper() -> None:
    snapshot = audit_paper_consistency.load_json(
        ROOT
        / "paper"
        / "generated"
        / "container-audit"
        / "sealed21_container_resolution.preview.json"
    )
    mutated = json.loads(json.dumps(snapshot))
    mutated["pull_performed"] = True
    checks = audit_paper_consistency.container_resolution_preview_checks(snapshot=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_execution_plan_cell_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["evaluation_cells"] = 83
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_statistical_design_drift() -> None:
    prereg = audit_paper_consistency.load_json(ROOT / "configs" / "experiments" / "ei_confirmation_v1.json")
    mutated = json.loads(json.dumps(prereg))
    mutated["primary_endpoint"]["alpha"] = 0.10
    checks = audit_paper_consistency.sealed_statistical_design_checks(preregistration=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_plan_primary_endpoint_contract_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["primary_endpoint"]["alpha"] = 0.10
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_plan_statistical_design_contract_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["statistical_design"]["bootstrap"]["seed"] = 7
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_statistics_status_drift() -> None:
    payload = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21" / "sealed_statistics.json") if (ROOT / "paper" / "generated" / "sealed21" / "sealed_statistics.json").is_file() else {
        "analysis_status": "incomplete",
        "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False,
        "primary_endpoint_alpha": 0.05,
        "primary_success_rule": "delta > 0, 95% CI lower bound > 0, and p < 0.05",
        "primary": {"inference_valid": False, "baseline_invalid": 1, "final_invalid": 0, "mcnemar_exact_p": None, "task_permutation_p": None, "cluster_bootstrap_ci_low": None, "cluster_bootstrap_ci_high": None},
        "result_integrity": {"baseline_unresolved_invalid": 1, "candidate_unresolved_invalid": 0, "baseline_invalid_keys": [["sealed", 1, "task"]], "candidate_invalid_keys": []},
    }
    mutated = json.loads(json.dumps(payload))
    mutated["analysis_status"] = "complete"
    checks = audit_paper_consistency.sealed_statistics_status_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_statistics_alpha_detached_from_preregistration() -> None:
    payload = {
        "analysis_status": "incomplete",
        "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False,
        "primary_endpoint_alpha": 0.10,
        "primary_success_rule": "delta > 0, 95% CI lower bound > 0, and p < 0.05",
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
    }
    checks = audit_paper_consistency.sealed_statistics_status_checks(payload)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_statistics_success_rule_detached_from_preregistration() -> None:
    payload = {
        "analysis_status": "incomplete",
        "inference_blocked_reason": "unresolved invalid",
        "pre_registered_success": False,
        "primary_endpoint_alpha": 0.05,
        "primary_success_rule": "delta > 0",
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
    }
    checks = audit_paper_consistency.sealed_statistics_status_checks(payload)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_unrecomputed_acceptance_plan_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["strict_acceptance_recomputed"] = True
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_ready_flag_without_readiness() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["ready_to_execute"] = True
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_ready_flag_without_strict_acceptance() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["strict_acceptance"] = True
    mutated["readiness"]["strict_acceptance"] = True
    mutated["readiness"]["strict_acceptance_recomputed"] = True
    mutated["strict_acceptance_recomputed"] = True
    mutated["ready_to_execute"] = True
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_strict_source_binding_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["strict_candidate_result_sha256"] = "0" * 64
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_sealed_candidate_directory_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["candidate_dir"] = str(ROOT / "runs" / "outside-candidate")
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_strict_artifact_hash_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["strict_acceptance_sha256"] = "0" * 64
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_readiness_flag_not_matching_disk_artifact() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["strict_acceptance_artifact_present"] = True
    mutated["readiness"]["strict_acceptance_artifact_present"] = True
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_source_snapshot_flag_not_matching_environment() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["readiness"]["source_snapshot_ready"] = True
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_environment_git_head_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["execution_environment"]["git_head"] = "0" * 40
    checks = audit_paper_consistency.environment_identity_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_dependency_environment_identity_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["dependency_lock"]["harbor_cli"]["version"] = "0.0.0-tampered"
    checks = audit_paper_consistency.environment_identity_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_project_python_identity_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["dependency_lock"]["project_python"]["version"] = "Python tampered"
    checks = audit_paper_consistency.environment_identity_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_runtime_binary_hash_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["dependency_lock"]["project_python"]["executable_sha256"] = "0" * 64
    checks = audit_paper_consistency.environment_identity_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_runtime_version_drift() -> None:
    plan = audit_paper_consistency.load_json(ROOT / "paper" / "generated" / "sealed21-execution-plan.json")
    mutated = json.loads(json.dumps(plan))
    mutated["dependency_lock"]["harbor_cli"]["version"] = "Harbor tampered"
    checks = audit_paper_consistency.environment_identity_checks(mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_readiness_blocker_list_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["readiness_blockers"] = []
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_stale_readiness_blocker_narrative() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    english = (ROOT / "paper" / "EXPERIMENTS_EN.md").read_text(encoding="utf-8")
    chinese = (ROOT / "paper" / "EXPERIMENTS_ZH.md").read_text(encoding="utf-8")
    stale_english = english.replace("strict_decision_missing", "strict_decision_stale", 1)
    checks = audit_paper_consistency.sealed_readiness_narrative_checks(
        plan=plan,
        english=stale_english,
        chinese=chinese,
    )
    assert not all(check["ok"] for check in checks)


def test_paper_audit_models_malformed_strict_artifact_as_single_blocker() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["strict_decision"] = "malformed"
    mutated["strict_acceptance_artifact_present"] = True
    mutated["strict_acceptance_artifact_malformed"] = True
    mutated["strict_acceptance_recomputed"] = False
    mutated["readiness"]["strict_acceptance_artifact_present"] = True
    mutated["readiness"]["strict_acceptance_artifact_malformed"] = True
    mutated["readiness"]["strict_acceptance_recomputed"] = False
    expected = audit_paper_consistency.expected_readiness_blockers(
        plan=mutated, strict_decision="malformed"
    )
    assert "strict_acceptance_artifact_malformed" in expected
    assert "strict_acceptance_boolean_inconsistent" not in expected
    assert "strict_acceptance_source_hashes_unstable" not in expected
    assert "strict_acceptance_result_binding_failed" not in expected


def test_paper_audit_rejects_run_identity_role_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["baseline_run_identity"] = mutated["candidate_run_identity"]
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_dependency_lock_content_drift() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    mutated["dependency_lock"]["project_python"]["packages"].append("tampered==0")
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_dependency_lock_schema_drift_after_hash_recompute() -> None:
    plan = audit_paper_consistency.load_json(
        ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
    )
    mutated = json.loads(json.dumps(plan))
    lock = mutated["dependency_lock"]
    lock["project_python"]["package_count"] += 1
    lock["bundle_sha256"] = capture_environment_lock.dependency_bundle_sha256(lock)
    checks = audit_paper_consistency.sealed_execution_plan_checks(plan=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_derives_present_acceptance_decision(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "acceptance.strict.json"
    artifact.write_text(json.dumps({"decision": "accepted"}), encoding="utf-8")
    monkeypatch.setattr(
        audit_paper_consistency,
        "verify_acceptance_artifact",
        lambda **_kwargs: {"decision": "accepted"},
    )

    decision, recomputed = audit_paper_consistency.sealed_acceptance_plan_state(
        {
            "strict_acceptance": str(artifact),
            "strict_baseline_result": str(tmp_path / "baseline.json"),
            "strict_candidate_result": str(tmp_path / "candidate.json"),
        }
    )
    assert (decision, recomputed) == ("accepted", True)


def test_paper_audit_labels_malformed_acceptance_decision(tmp_path: Path, monkeypatch) -> None:
    artifact = tmp_path / "acceptance.strict.json"
    artifact.write_text(json.dumps({"accepted": True}), encoding="utf-8")
    def reject_verifier(**_kwargs):
        raise ValueError("malformed acceptance")

    monkeypatch.setattr(audit_paper_consistency, "verify_acceptance_artifact", reject_verifier)

    decision, recomputed = audit_paper_consistency.sealed_acceptance_plan_state(
        {"strict_acceptance": str(artifact)}
    )
    assert (decision, recomputed) == ("malformed", False)


def test_paper_audit_rejects_sealed_protocol_cell_drift() -> None:
    protocol = (ROOT / "paper" / "SEALED_PROTOCOL.md").read_text(encoding="utf-8")
    mutated = protocol.replace("21 个任务 × 2 attempts，共 84 个评测单元", "21 个任务 × 2 attempts，共 83 个评测单元", 1)
    checks = audit_paper_consistency.sealed_protocol_checks(protocol=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_primary_endpoint_contract_drift() -> None:
    preregistration = audit_paper_consistency.load_json(ROOT / "configs" / "experiments" / "ei_confirmation_v1.json")
    mutated = json.loads(json.dumps(preregistration))
    mutated["primary_endpoint"]["success_rule"] = "delta > 0"
    checks = audit_paper_consistency.sealed_protocol_checks(preregistration=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_multiplicity_contract_drift() -> None:
    preregistration = audit_paper_consistency.load_json(
        ROOT / "configs" / "experiments" / "ei_confirmation_v1.json"
    )
    mutated = json.loads(json.dumps(preregistration))
    mutated["multiplicity"]["adjustment"] = "holm"
    checks = audit_paper_consistency.sealed_protocol_checks(preregistration=mutated)
    assert not all(check["ok"] for check in checks)


def test_paper_audit_rejects_missingness_completion_rule_drift() -> None:
    preregistration = audit_paper_consistency.load_json(
        ROOT / "configs" / "experiments" / "ei_confirmation_v1.json"
    )
    mutated = json.loads(json.dumps(preregistration))
    mutated["missingness_policy"]["completion_rule"] = "impute unresolved cells"
    checks = audit_paper_consistency.sealed_protocol_checks(preregistration=mutated)
    assert not all(check["ok"] for check in checks)
