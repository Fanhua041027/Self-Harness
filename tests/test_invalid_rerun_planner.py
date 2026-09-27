from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_rerun_module():
    path = Path(__file__).resolve().parents[1] / "eval" / "scripts" / "rerun_invalid_cases.py"
    spec = importlib.util.spec_from_file_location("self_harness_invalid_rerun", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_fixture(output_dir: Path) -> None:
    valid = {
        "case_id": "valid-case",
        "split": "train",
        "repeat": 1,
        "status": "passed",
        "passed": True,
        "reward": 1.0,
    }
    invalid = {
        "case_id": "invalid-case",
        "split": "train",
        "repeat": 1,
        "status": "invalid",
        "passed": False,
        "attempt_count": 2,
        "infrastructure_error": "agent timeout",
    }
    repeat = {"split": "train", "repeat": 1, "passed": 1, "total": 2, "case_results": [valid, invalid]}
    result = {
        "name": "fixture",
        "model": "openai:test-model",
        "passed": 1,
        "total": 2,
        "splits": {"train": [repeat]},
    }
    paths = [
        output_dir / "result.json",
        output_dir / "splits" / "train" / "repeat-01" / "result.json",
        output_dir / "splits" / "train" / "repeat-01" / "cases" / "invalid-case" / "result.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = invalid if path.parent.name == "invalid-case" else (repeat if "repeat-01" in path.parts else result)
        path.write_text(json.dumps(payload), encoding="utf-8")
    harbor = paths[-1].parent / "harbor" / "trial"
    harbor.mkdir(parents=True)
    (harbor / "evidence.txt").write_text("preserve", encoding="utf-8")


def write_config(path: Path) -> None:
    path.write_text(
        """[eval]
timeout_s = 100
infrastructure_retries = 2
case_concurrency = 3
""",
        encoding="utf-8",
    )


def test_read_json_rejects_duplicate_nested_key_and_accepts_bom(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    path = tmp_path / "legacy.json"
    path.write_text('{"outer": {"status": "failed", "status": "invalid"}}', encoding="utf-8-sig")

    try:
        rerun.read_json(path)
    except ValueError as exc:
        assert "duplicate JSON key: status" in str(exc)
    else:
        raise AssertionError("duplicate JSON key was accepted by rerun planner")


def test_dry_run_writes_plan_without_clearing_checkpoints(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    plan_out = tmp_path / "plan.json"
    write_fixture(output_dir)
    write_config(config)

    exit_code = rerun.main(
        ["--output-dir", str(output_dir), "--config", str(config), "--plan-out", str(plan_out)]
    )

    assert exit_code == 0
    assert (output_dir / "result.json").is_file()
    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["invalid_count"] == 1
    assert plan["reason_counts"] == {"timeout": 1}
    assert plan["max_model_attempts"] == 3
    assert plan["cases"][0]["case_id"] == "invalid-case"
    assert plan["source_result_stable"] is True


def test_rerun_plan_rejects_result_change_during_read(monkeypatch, tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    write_fixture(output_dir)
    write_config(config)
    values = iter(("before", "after"))
    monkeypatch.setattr(rerun, "sha256_file", lambda _path: next(values))

    try:
        rerun.build_plan(output_dir=output_dir, config_path=config, candidate_workspace=None)
    except ValueError as exc:
        assert "changed while rerun plan was being built" in str(exc)
    else:
        raise AssertionError("changed result was accepted by rerun planner")


def test_plan_includes_legacy_failed_cell_without_verifier_reward(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    write_fixture(output_dir)
    write_config(config)
    result_path = output_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    legacy = {
        "case_id": "legacy-outage",
        "split": "train",
        "repeat": 1,
        "status": "failed",
        "passed": False,
        "reward": None,
        "trace_metadata": {"failure": "APIConnectionError: Connection error."},
    }
    result["splits"]["train"][0]["case_results"].append(legacy)
    result_path.write_text(json.dumps(result), encoding="utf-8")

    plan = rerun.build_plan(output_dir=output_dir, config_path=config, candidate_workspace=None)

    assert plan["invalid_count"] == 2
    legacy_plan = next(item for item in plan["cases"] if item["case_id"] == "legacy-outage")
    assert legacy_plan["recorded_status"] == "failed"
    assert "APIConnectionError" in legacy_plan["infrastructure_error"]


def test_cell_manifest_selects_exact_invalid_subset(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    write_fixture(output_dir)
    write_config(config)

    plan = rerun.build_plan(
        output_dir=output_dir,
        config_path=config,
        candidate_workspace=None,
        selected_cells={("train", 1, "invalid-case")},
    )

    assert plan["selection"] == "cell_manifest"
    assert plan["selection_side"] == "baseline"
    assert plan["global_invalid_count_at_plan_time"] == 1
    assert plan["invalid_count"] == 1


def test_canary_outcome_ignores_behavioral_pass_fail_but_requires_numeric_reward(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    result = {
        "splits": {
            "train": [
                {
                    "repeat": 1,
                    "case_results": [
                        {"case_id": "behavior-fail", "passed": False, "status": "failed", "reward": 0.0},
                        {"case_id": "provider-error", "passed": False, "status": "failed", "reward": None},
                    ],
                }
            ]
        }
    }
    outcome = rerun.evaluate_canary_outcomes(
        result,
        {("train", 1, "behavior-fail"), ("train", 1, "provider-error")},
    )
    assert outcome["numeric_verifier_outcome_count"] == 1
    assert outcome["behavioral_fail_count"] == 1
    assert outcome["infrastructure_invalid_count"] == 1
    assert outcome["canary_ready"] is False


def test_canary_outcome_is_ready_for_numeric_behavioral_failure(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    result = {
        "splits": {
            "train": [{"repeat": 1, "case_results": [{"case_id": "case", "passed": False, "status": "failed", "reward": 0.0}]}]
        }
    }
    outcome = rerun.evaluate_canary_outcomes(result, {("train", 1, "case")})
    assert outcome["canary_ready"] is True
    assert outcome["numeric_verifier_outcome_count"] == 1
    assert outcome["behavioral_pass_count"] == 0


def test_execution_provenance_binds_pre_and_post_result_hashes(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    write_fixture(output_dir)
    write_config(config)
    plan = rerun.build_plan(output_dir=output_dir, config_path=config, candidate_workspace=None)
    provenance, result = rerun.build_execution_provenance(
        plan=plan,
        result_path=output_dir / "result.json",
        evaluator_returncode=0,
        selected_cells=None,
        selection_phase=None,
    )
    assert result["model"] == "openai:test-model"
    assert provenance["status"] == "completed"
    assert provenance["pre_source_result_sha256"] == plan["source_result_sha256"]
    assert provenance["post_source_result_sha256"] == rerun.sha256_file(output_dir / "result.json")
    assert provenance["post_source_result_stable"] is True


def test_execution_provenance_counts_from_stable_result_snapshot(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    write_fixture(tmp_path)
    result = rerun.read_json(tmp_path / "result.json")
    assert rerun.count_invalids_from_result(result) == 1
    assert rerun.count_selected_invalids_from_result(
        result, {("train", 1, "invalid-case")}
    ) == 1


def test_cell_manifest_rejects_stale_or_valid_cell(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    write_fixture(output_dir)
    write_config(config)

    try:
        rerun.build_plan(
            output_dir=output_dir,
            config_path=config,
            candidate_workspace=None,
            selected_cells={("train", 1, "valid-case")},
        )
    except ValueError as exc:
        assert "rebuild the phased plan" in str(exc)
    else:
        raise AssertionError("valid cell was accepted by rerun manifest")


def test_cell_manifest_rejects_role_mismatch(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    write_fixture(tmp_path)
    source = tmp_path / "result.json"
    manifest = tmp_path / "candidate.json"
    payload = {
        "format": rerun.CELL_MANIFEST_FORMAT,
        "side": "candidate",
        "source_hashes_stable": True,
        "baseline_result": str(source.resolve()),
        "baseline_result_sha256": rerun.sha256_file(source),
        "candidate_result": str(source.resolve()),
        "candidate_result_sha256": rerun.sha256_file(source),
        "cells": [{"split": "train", "repeat": 1, "case_id": "invalid-case"}],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    try:
        rerun.load_cell_filter(manifest, expected_side="baseline")
    except ValueError as exc:
        assert "side mismatch" in str(exc)
    else:
        raise AssertionError("manifest with candidate side was accepted for baseline execution")


def test_cell_manifest_rejects_role_mismatch(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    write_fixture(tmp_path)
    source = tmp_path / "result.json"
    manifest = tmp_path / "candidate.json"
    payload = {
        "format": rerun.CELL_MANIFEST_FORMAT,
        "side": "candidate",
        "source_hashes_stable": True,
        "baseline_result": str(source.resolve()),
        "baseline_result_sha256": rerun.sha256_file(source),
        "candidate_result": str(source.resolve()),
        "candidate_result_sha256": rerun.sha256_file(source),
        "cells": [{"split": "train", "repeat": 1, "case_id": "invalid-case"}],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    try:
        rerun.load_cell_filter(manifest, expected_side="baseline")
    except ValueError as exc:
        assert "side mismatch" in str(exc)
    else:
        raise AssertionError("manifest with candidate side was accepted for baseline execution")


def test_cell_manifest_rejects_changed_source_result(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    write_fixture(output_dir)
    candidate = tmp_path / "candidate.json"
    candidate.write_bytes((output_dir / "result.json").read_bytes())
    manifest = tmp_path / "phase.json"
    manifest_payload = {
        "format": rerun.CELL_MANIFEST_FORMAT,
        "phase": "phase_1_one_sided_bridge",
        "side": "baseline",
        "baseline_result": str((output_dir / "result.json").resolve()),
        "baseline_result_sha256": rerun.sha256_file(output_dir / "result.json"),
        "candidate_result": str(candidate.resolve()),
        "candidate_result_sha256": rerun.sha256_file(candidate),
        "cells": [{"split": "train", "repeat": 1, "case_id": "invalid-case"}],
    }
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    try:
        rerun.load_cell_filter(manifest)
    except ValueError as exc:
        assert "stable source-hash provenance" in str(exc)
    else:
        raise AssertionError("manifest without stable provenance was accepted")

    manifest_payload["source_hashes_stable"] = True
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

    assert rerun.load_cell_filter(manifest) == {("train", 1, "invalid-case")}
    candidate.write_text(candidate.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    try:
        rerun.load_cell_filter(manifest)
    except ValueError as exc:
        assert "candidate source result hash mismatch" in str(exc)
    else:
        raise AssertionError("changed source result was accepted by cell manifest")


def test_archive_clears_only_json_checkpoints_and_preserves_harbor(tmp_path: Path) -> None:
    rerun = load_rerun_module()
    output_dir = tmp_path / "run"
    config = tmp_path / "rerun.toml"
    archive = tmp_path / "archive"
    write_fixture(output_dir)
    write_config(config)
    plan = rerun.build_plan(output_dir=output_dir, config_path=config, candidate_workspace=None)

    rerun.archive_and_clear_checkpoints(output_dir=output_dir, plan=plan, archive_dir=archive)

    assert not (output_dir / "result.json").exists()
    assert not (output_dir / "splits" / "train" / "repeat-01" / "result.json").exists()
    assert not (
        output_dir / "splits" / "train" / "repeat-01" / "cases" / "invalid-case" / "result.json"
    ).exists()
    assert (
        output_dir
        / "splits"
        / "train"
        / "repeat-01"
        / "cases"
        / "invalid-case"
        / "harbor"
        / "trial"
        / "evidence.txt"
    ).read_text(encoding="utf-8") == "preserve"
    assert (archive / "result.json").is_file()


def test_archive_directory_is_collision_free(tmp_path: Path, monkeypatch) -> None:
    rerun = load_rerun_module()
    monkeypatch.setattr(rerun.time, "strftime", lambda _format: "20260827-120000")
    first = rerun.create_archive_dir(tmp_path)
    second = rerun.create_archive_dir(tmp_path)
    assert first != second
    assert first.is_dir() and second.is_dir()
    assert second.name.endswith("-01")


    rerun = load_rerun_module()
    monkeypatch.setenv("SELF_HARNESS_MODEL", "openai:test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    plan = {"model": "openai:test-model"}

    try:
        rerun.validate_execution_environment(plan, tmp_path)
    except RuntimeError as exc:
        assert "candidate workspace has no repo_baseline.py" in str(exc)
    else:
        raise AssertionError("invalid candidate workspace was accepted")
