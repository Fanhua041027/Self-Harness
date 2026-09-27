#!/usr/bin/env python3
"""对冻结的 Sealed21 baseline/candidate 结果执行预注册配对分析。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.analyze_experiments import (
    ANALYSIS_SEED,
    BOOTSTRAP_SAMPLES as ANALYSIS_BOOTSTRAP_SAMPLES,
    BOOTSTRAP_PERCENTILE_METHOD,
    analyze_split,
    build_statistical_design_metadata,
    flatten_result,
    is_effectively_invalid,
    load_json as load_clean64_json,
    passed_for_descriptive_rate,
)
from acceptance.scripts.run_acceptance_gate import verify_acceptance_artifact
from eval.scripts.build_sealed_split import task_digest
from eval.scripts.capture_environment_lock import validate_lock as validate_environment_lock
from eval.scripts.capture_container_resolution import (
    selected_references,
    validate_snapshot as validate_container_resolution,
)
from eval.scripts.result_validity import repeat_aggregate_consistency_error


BOOTSTRAP_SAMPLES = ANALYSIS_BOOTSTRAP_SAMPLES
BOOTSTRAP_SEED = ANALYSIS_SEED
PRIMARY_SUCCESS_RULE = "delta > 0, 95% CI lower bound > 0, and p < 0.05"


def sealed_statistical_design_metadata(tasks: int) -> dict[str, Any]:
    """返回 Sealed21 与 Clean64 共用字段的结构化统计设计。"""
    if ANALYSIS_SEED != BOOTSTRAP_SEED or BOOTSTRAP_SAMPLES != 20_000:
        raise ValueError("Sealed statistical constants diverge from shared analysis constants")
    return build_statistical_design_metadata(
        split_task_counts={"sealed": tasks},
        roles=["baseline", "candidate"],
        seed_derivation="fixed preregistered bootstrap seed",
    )


def load_json(path: Path) -> dict[str, Any]:
    return load_clean64_json(path)
def validate_execution_environment_snapshot(
    snapshot: Any, *, expected_model: str
) -> dict[str, Any]:
    """Validate the environment evidence required by the frozen Sealed run."""
    if not isinstance(snapshot, dict):
        raise ValueError("sealed freeze is missing execution environment snapshot")
    required_strings = (
        "captured_at",
        "os_description",
        "os_architecture",
        "python_executable",
        "python_version",
        "harbor_executable",
        "harbor_version",
        "docker_client_version",
        "docker_server_version",
        "git_head",
        "model",
        "endpoint",
    )
    for key in required_strings:
        value = snapshot.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"sealed execution environment field is missing or blank: {key}")
    try:
        captured_at = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("sealed execution environment captured_at is not ISO-8601") from exc
    if captured_at.tzinfo is None:
        raise ValueError("sealed execution environment captured_at must include a timezone")
    for key in ("docker_available", "git_dirty", "dirty_worktree_override"):
        if not isinstance(snapshot.get(key), bool):
            raise ValueError(f"sealed execution environment field must be boolean: {key}")
    if snapshot["model"] != expected_model:
        raise ValueError("sealed execution environment model does not match freeze")
    if not snapshot["endpoint"].startswith(("https://", "http://")):
        raise ValueError("sealed execution environment endpoint must be an HTTP(S) URL")
    return snapshot


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_stable_file_hash(path: Path, expected_sha256: str, label: str) -> None:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"{label} changed during analysis")


def code_bundle_sha256(files: dict[str, dict[str, str]]) -> str:
    canonical = "".join(
        f"{name}={files[name]['sha256']}\n" for name in sorted(files)
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_code_group(freeze: dict[str, Any], group: str) -> str:
    payload = freeze.get(group)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        raise ValueError(f"missing frozen {group} provenance")
    files = payload["files"]
    for name, item in files.items():
        path = Path(str(item.get("path", ""))).resolve()
        if not path.is_file() or sha256_file(path) != item.get("sha256"):
            raise ValueError(f"frozen code hash mismatch: {group}.{name}")
    bundle = code_bundle_sha256(files)
    if bundle != payload.get("bundle_sha256"):
        raise ValueError(f"frozen code bundle hash mismatch: {group}")
    return bundle


def sealed_run_identity(
    *,
    manifest_sha256: str,
    preregistration_sha256: str,
    config_sha256: str,
    execution_code_sha256: str,
    dependency_bundle_sha256: str,
    role: str,
    surface_sha256: str,
) -> str:
    if role not in {"baseline", "candidate"}:
        raise ValueError(f"unexpected sealed role: {role}")
    return (
        f"sealed21:{manifest_sha256}:{preregistration_sha256}:{config_sha256}:"
        f"{execution_code_sha256}:"
        f"{dependency_bundle_sha256}:"
        f"{role}:{surface_sha256}"
    )


def manifest_task_root(*, manifest_path: Path, manifest: dict[str, Any]) -> Path:
    repo_root = manifest_path.parents[2]
    task_root_value = Path(str(manifest.get("task_root", "")))
    return (
        task_root_value.resolve()
        if task_root_value.is_absolute()
        else (repo_root / task_root_value).resolve()
    )


def validate_sealed_manifest(manifest: dict[str, Any]) -> set[str]:
    if manifest.get("format") != "self_harness.sealed_split.v1":
        raise ValueError("unexpected sealed manifest format")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 21:
        raise ValueError("sealed manifest must contain exactly 21 tasks")
    if manifest.get("task_count") != 21 or manifest.get("task_count") != len(tasks):
        raise ValueError("sealed manifest task_count must equal 21")
    case_ids = [str(item.get("case_id", "")) for item in tasks if isinstance(item, dict)]
    if len(case_ids) != 21 or any(not case_id for case_id in case_ids):
        raise ValueError("sealed manifest contains malformed task IDs")
    if len(set(case_ids)) != 21:
        raise ValueError("sealed manifest contains duplicate task IDs")
    if manifest.get("classification") != "frozen-before-sealed-evaluation":
        raise ValueError("sealed manifest classification is not outcome-blind")
    if manifest.get("prior_result_references") != 0:
        raise ValueError("sealed manifest contains prior result references")
    return set(case_ids)


def resolve_repository_reference(value: Any) -> Path:
    """将预注册中的相对路径按仓库根解析，避免工作目录影响语义校验。"""
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def validate_preregistration(
    preregistration: dict[str, Any],
    *,
    model: str,
    candidate_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    config_path: Path,
) -> None:
    """验证已冻结预注册的关键设计语义，而不只依赖文件 SHA256。"""
    if preregistration.get("format") != "self_harness.ei_confirmation_preregistration.v1":
        raise ValueError("unexpected Sealed preregistration format")
    if preregistration.get("status") != "frozen-before-sealed-evaluation":
        raise ValueError("Sealed preregistration is not frozen")
    if preregistration.get("primary_model") != model:
        raise ValueError("Sealed preregistration model does not match freeze")
    if preregistration.get("primary_candidate") != candidate_id:
        raise ValueError("Sealed preregistration candidate does not match freeze")

    split = preregistration.get("sealed_split")
    if not isinstance(split, dict):
        raise ValueError("Sealed preregistration is missing sealed_split")
    if resolve_repository_reference(split.get("manifest")) != manifest_path.resolve():
        raise ValueError("Sealed preregistration manifest path does not match analysis input")
    if split.get("manifest_sha256") != manifest_sha256:
        raise ValueError("Sealed preregistration manifest hash does not match analysis input")
    if resolve_repository_reference(split.get("config")) != config_path.resolve():
        raise ValueError("Sealed preregistration config path does not match analysis input")
    if split.get("tasks") != 21 or split.get("attempts_per_harness") != 42:
        raise ValueError("Sealed preregistration design must be 21 tasks and 42 attempts per harness")
    if split.get("outcome_blind") is not True:
        raise ValueError("Sealed preregistration split must be outcome-blind")

    analysis_integrity = preregistration.get("analysis_integrity")
    if not isinstance(analysis_integrity, dict):
        raise ValueError("Sealed preregistration is missing analysis_integrity")
    if analysis_integrity.get("bootstrap_seed") != BOOTSTRAP_SEED:
        raise ValueError("Sealed preregistration bootstrap seed does not match analyzer")
    if analysis_integrity.get("permutation_implementation") != (
        "two-sided exact task-level sign permutation via dynamic programming over rational task effects"
    ):
        raise ValueError("Sealed preregistration permutation method does not match analyzer")
    endpoint = preregistration.get("primary_endpoint")
    if not isinstance(endpoint, dict):
        raise ValueError("Sealed preregistration is missing primary_endpoint")
    expected_endpoint = {
        "name": "paired Sealed21 pass-rate difference",
        "contrast": "frozen candidate minus baseline",
        "unit": "Terminal-Bench task",
        "metric": "verifier pass fraction over two attempts per task",
        "confidence_interval": "task-clustered nonparametric bootstrap, 20000 resamples",
        "hypothesis_test": "two-sided task-level paired sign permutation test",
        "alpha": 0.05,
        "success_rule": PRIMARY_SUCCESS_RULE,
    }
    for field, expected in expected_endpoint.items():
        if endpoint.get(field) != expected:
            raise ValueError(f"Sealed preregistration primary endpoint drift: {field}")
    expected_design = sealed_statistical_design_metadata(int(split["tasks"]))
    if preregistration.get("statistical_design") != expected_design:
        raise ValueError("Sealed preregistration statistical design drift")
    missingness = preregistration.get("missingness_policy")
    if not isinstance(missingness, dict):
        raise ValueError("Sealed preregistration is missing missingness_policy")
    expected_missingness = {
        "classification": "infrastructure-invalid is neither pass nor behavioral fail",
        "reruns": 2,
        "replacement_tasks": False,
        "imputation": False,
        "completion_rule": "both harnesses must have zero unresolved invalid cells",
        "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
    }
    if missingness != expected_missingness:
        raise ValueError("Sealed preregistration missingness policy contract drift")
    expected_multiplicity = {
        "primary_hypotheses": 1,
        "adjustment": "none",
        "rationale": "one pre-registered model, split, contrast, and primary endpoint",
    }
    if preregistration.get("multiplicity") != expected_multiplicity:
        raise ValueError("Sealed preregistration multiplicity contract drift")


def validate_frozen_provenance(
    *,
    freeze_path: Path,
    manifest_path: Path,
    baseline_payload: dict[str, Any],
    candidate_payload: dict[str, Any],
) -> dict[str, Any]:
    """证明结果身份、冻结 harness、划分和严格验收产物仍为同一预注册状态。"""
    freeze = load_json(freeze_path)
    if freeze.get("format") != "self_harness.sealed_freeze.v3":
        raise ValueError("unexpected sealed freeze format")
    manifest_sha256 = sha256_file(manifest_path)
    if freeze.get("sealed_manifest_sha256") != manifest_sha256:
        raise ValueError("sealed manifest hash does not match freeze")
    manifest = load_json(manifest_path)
    validate_sealed_manifest(manifest)
    task_root = manifest_task_root(manifest_path=manifest_path, manifest=manifest)
    for item in manifest.get("tasks", []):
        case_id = str(item["case_id"])
        if task_digest(task_root / case_id) != item.get("task_sha256"):
            raise ValueError(f"sealed task hash does not match manifest: {case_id}")
    preregistration_path = Path(str(freeze.get("preregistration", ""))).resolve()
    preregistration_sha256 = str(freeze.get("preregistration_sha256", ""))
    if (
        not preregistration_path.is_file()
        or sha256_file(preregistration_path) != preregistration_sha256
    ):
        raise ValueError("preregistration hash does not match freeze")
    preregistration = load_json(preregistration_path)
    config_path = Path(str(freeze.get("sealed_config", ""))).resolve()
    config_sha256 = str(freeze.get("sealed_config_sha256", ""))
    if not config_path.is_file() or sha256_file(config_path) != config_sha256:
        raise ValueError("sealed evaluation config hash does not match freeze")
    model = freeze.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("sealed freeze model is missing or blank")
    candidate_id = freeze.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("sealed freeze candidate_id is missing or blank")
    validate_preregistration(
        preregistration,
        model=model,
        candidate_id=candidate_id,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        config_path=config_path,
    )
    for section in ("primary_endpoint", "statistical_design", "missingness_policy", "multiplicity"):
        if freeze.get(section) != preregistration.get(section):
            raise ValueError(f"sealed freeze {section} contract does not match preregistration")
    if baseline_payload.get("config_sha256") != config_sha256:
        raise ValueError("sealed baseline result config hash does not match freeze")
    if candidate_payload.get("config_sha256") != config_sha256:
        raise ValueError("sealed candidate result config hash does not match freeze")
    execution_code_sha256 = validate_code_group(freeze, "execution_code")
    analysis_code_sha256 = validate_code_group(freeze, "analysis_code")
    dependency_lock = freeze.get("dependency_lock")
    if not isinstance(dependency_lock, dict):
        raise ValueError("missing frozen environment dependency lock")
    validate_environment_lock(dependency_lock)
    dependency_bundle = str(dependency_lock["bundle_sha256"])

    environment_snapshot = validate_execution_environment_snapshot(
        freeze.get("execution_environment"), expected_model=model
    )
    if baseline_payload.get("model") != model or candidate_payload.get("model") != model:
        raise ValueError("sealed result model does not match freeze")

    checked_files: dict[str, str] = {}
    for role in ("baseline", "candidate"):
        surface_path = Path(str(freeze.get(f"{role}_surface", ""))).resolve()
        expected_hash = str(freeze.get(f"{role}_surface_sha256", ""))
        if not surface_path.is_file() or sha256_file(surface_path) != expected_hash:
            raise ValueError(f"{role} surface hash does not match freeze")
        checked_files[f"{role}_surface"] = str(surface_path)
        checked_files[f"{role}_surface_sha256"] = expected_hash

    strict_path = Path(str(freeze.get("strict_acceptance", ""))).resolve()
    strict_hash = str(freeze.get("strict_acceptance_sha256", ""))
    if not strict_path.is_file() or sha256_file(strict_path) != strict_hash:
        raise ValueError("strict acceptance artifact hash does not match freeze")
    if load_json(strict_path).get("decision") != "accepted":
        raise ValueError("frozen strict acceptance decision is not accepted")
    strict_baseline_path = Path(str(freeze.get("strict_baseline_result", ""))).resolve()
    strict_candidate_path = Path(str(freeze.get("strict_candidate_result", ""))).resolve()
    strict_baseline_hash = str(freeze.get("strict_baseline_result_sha256", ""))
    strict_candidate_hash = str(freeze.get("strict_candidate_result_sha256", ""))
    if not strict_baseline_path.is_file() or sha256_file(strict_baseline_path) != strict_baseline_hash:
        raise ValueError("frozen strict baseline result hash does not match freeze")
    if not strict_candidate_path.is_file() or sha256_file(strict_candidate_path) != strict_candidate_hash:
        raise ValueError("frozen strict candidate result hash does not match freeze")

    expected_baseline = sealed_run_identity(
        manifest_sha256=manifest_sha256,
        preregistration_sha256=preregistration_sha256,
        config_sha256=config_sha256,
        execution_code_sha256=execution_code_sha256,
        dependency_bundle_sha256=dependency_bundle,
        role="baseline",
        surface_sha256=str(freeze["baseline_surface_sha256"]),
    )
    expected_candidate = sealed_run_identity(
        manifest_sha256=manifest_sha256,
        preregistration_sha256=preregistration_sha256,
        config_sha256=config_sha256,
        execution_code_sha256=execution_code_sha256,
        dependency_bundle_sha256=dependency_bundle,
        role="candidate",
        surface_sha256=str(freeze["candidate_surface_sha256"]),
    )
    if baseline_payload.get("run_identity") != expected_baseline:
        raise ValueError("sealed baseline run identity does not match freeze")
    if candidate_payload.get("run_identity") != expected_candidate:
        raise ValueError("sealed candidate run identity does not match freeze")
    return {
        "freeze": str(freeze_path),
        "manifest_sha256": manifest_sha256,
        "preregistration": str(preregistration_path),
        "preregistration_sha256": preregistration_sha256,
        "sealed_config": str(config_path),
        "sealed_config_sha256": config_sha256,
        "execution_code_bundle_sha256": execution_code_sha256,
        "analysis_code_bundle_sha256": analysis_code_sha256,
        "dependency_bundle_sha256": dependency_bundle,
        "project_package_count": dependency_lock["project_python"]["package_count"],
        "harbor_package_count": dependency_lock["harbor_python"]["package_count"],
        "execution_environment": environment_snapshot,
        "task_content_hashes_verified": len(manifest.get("tasks", [])),
        "strict_acceptance": str(strict_path),
        "strict_acceptance_sha256": strict_hash,
        "strict_baseline_result": str(strict_baseline_path),
        "strict_candidate_result": str(strict_candidate_path),
        "strict_baseline_result_sha256": strict_baseline_hash,
        "strict_candidate_result_sha256": strict_candidate_hash,
        "baseline_run_identity": expected_baseline,
        "candidate_run_identity": expected_candidate,
        **checked_files,
    }


def validate_strict_acceptance_artifact(
    *,
    path: Path,
    baseline_result_path: Path,
    candidate_result_path: Path,
) -> dict[str, Any]:
    """验证 strict acceptance artifact 的格式、决策和两侧结果绑定。"""
    artifact_hash = sha256_file(path)
    return verify_acceptance_artifact(
        artifact=load_json(path),
        baseline_result_path=baseline_result_path,
        candidate_result_path=candidate_result_path,
        artifact_path=path,
        artifact_sha256=artifact_hash,
        splits=("train", "heldout"),
        expected_repeats=2,
    )
def require_stable_frozen_inputs(
    *, freeze_path: Path, manifest_path: Path, provenance: dict[str, Any]
) -> None:
    """在统计完成后再次核对所有冻结来源，覆盖代码、任务和预注册 TOCTOU。"""
    require_stable_file_hash(
        Path(str(provenance["preregistration"])),
        str(provenance["preregistration_sha256"]),
        "preregistration",
    )
    require_stable_file_hash(
        Path(str(provenance["sealed_config"])),
        str(provenance["sealed_config_sha256"]),
        "sealed config",
    )
    require_stable_file_hash(
        Path(str(provenance["strict_acceptance"])),
        str(provenance["strict_acceptance_sha256"]),
        "strict acceptance",
    )
    require_stable_file_hash(
        Path(str(provenance["strict_baseline_result"])),
        str(provenance["strict_baseline_result_sha256"]),
        "strict baseline result",
    )
    require_stable_file_hash(
        Path(str(provenance["strict_candidate_result"])),
        str(provenance["strict_candidate_result_sha256"]),
        "strict candidate result",
    )
    for role in ("baseline", "candidate"):
        require_stable_file_hash(
            Path(str(provenance[role + "_surface"])),
            str(provenance[role + "_surface_sha256"]),
            f"{role} harness surface",
        )

    freeze = load_json(freeze_path)
    for group in ("execution_code", "analysis_code"):
        validate_code_group(freeze, group)

    manifest_sha256 = str(provenance["manifest_sha256"])
    require_stable_file_hash(manifest_path, manifest_sha256, "sealed manifest")
    manifest = load_json(manifest_path)
    task_root = manifest_task_root(manifest_path=manifest_path, manifest=manifest)
    for item in manifest.get("tasks", []):
        case_id = str(item.get("case_id", ""))
        if task_digest(task_root / case_id) != item.get("task_sha256"):
            raise ValueError(f"sealed task changed during analysis: {case_id}")

    model = freeze.get("model")
    candidate_id = freeze.get("candidate_id")
    validate_preregistration(
        load_json(Path(str(provenance["preregistration"]))),
        model=str(model),
        candidate_id=str(candidate_id),
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        config_path=Path(str(provenance["sealed_config"])),
    )


def validate_result(
    *, result_path: Path, expected_tasks: set[str], expected_repeats: int
) -> tuple[dict[tuple[str, int, str], dict[str, Any]], list[tuple[str, int, str]]]:
    payload = load_json(result_path)
    splits = payload.get("splits")
    repeats = splits.get("sealed") if isinstance(splits, dict) else None
    if not isinstance(repeats, list) or len(repeats) != expected_repeats:
        raise ValueError("sealed result must contain the expected repeat records")
    outer_repeats = [record.get("repeat") if isinstance(record, dict) else None for record in repeats]
    expected_repeat_values = list(range(1, expected_repeats + 1))
    if outer_repeats != expected_repeat_values:
        raise ValueError(
            f"sealed result outer repeat sequence mismatch: {outer_repeats}"
        )
    for record in repeats:
        case_results = record.get("case_results") if isinstance(record, dict) else None
        if not isinstance(case_results, list):
            raise ValueError("sealed result repeat is missing case_results")
        outer_repeat = record["repeat"]
        for case in case_results:
            if not isinstance(case, dict) or case.get("repeat") != outer_repeat:
                raise ValueError("sealed result case repeat does not match outer repeat")
    rows = flatten_result(result_path)
    keys = set(rows)
    expected = {
        ("sealed", repeat, case_id)
        for repeat in range(1, expected_repeats + 1)
        for case_id in expected_tasks
    }
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise ValueError(f"sealed result key mismatch: missing={missing}, extra={extra}")
    malformed_passed = sorted(
        key for key, row in rows.items() if not isinstance(row.get("passed"), bool)
    )
    if malformed_passed:
        raise ValueError(f"sealed result passed field must be boolean: {malformed_passed}")
    for record in repeats:
        aggregate_error = repeat_aggregate_consistency_error(record)
        if aggregate_error is not None:
            raise ValueError(
                f"sealed result repeat aggregate is inconsistent: repeat={record['repeat']}; "
                f"{aggregate_error}"
            )
    missing_reward = sorted(
        key
        for key, row in rows.items()
        if "reward" not in row or row.get("reward") is None
    )
    if missing_reward:
        raise ValueError(f"sealed result verifier reward is missing: {missing_reward}")
    malformed_reward = sorted(
        key
        for key, row in rows.items()
        if isinstance(row.get("reward"), bool)
        or not isinstance(row.get("reward"), (int, float))
        or not math.isfinite(float(row["reward"]))
    )
    if malformed_reward:
        raise ValueError(f"sealed result verifier reward must be finite numeric: {malformed_reward}")
    invalid = sorted(key for key, row in rows.items() if is_effectively_invalid(row))
    return rows, invalid


def validate_container_resolution_evidence(
    *,
    path: Path,
    expected_tasks: set[str],
    expected_references: set[str] | None = None,
) -> dict[str, Any]:
    snapshot = load_json(path)
    validate_container_resolution(snapshot)
    if set(snapshot.get("case_ids", [])) != expected_tasks:
        raise ValueError("container resolution task set does not match sealed manifest")
    if expected_references is not None:
        actual_references = {str(image["reference"]) for image in snapshot["images"]}
        if actual_references != expected_references:
            raise ValueError("container resolution references do not match sealed Dockerfiles")
    return snapshot


def category_rows(
    *,
    manifest: dict[str, Any],
    baseline: dict[tuple[str, int, str], dict[str, Any]],
    candidate: dict[tuple[str, int, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    categories = {str(item["case_id"]): str(item.get("category") or "unknown") for item in manifest["tasks"]}
    grouped: dict[str, list[tuple[str, bool, bool, bool, bool]]] = {}
    for key in sorted(baseline):
        category = categories[key[2]]
        grouped.setdefault(category, []).append(
            (
                key[2],
                passed_for_descriptive_rate(baseline[key]),
                passed_for_descriptive_rate(candidate[key]),
                is_effectively_invalid(baseline[key]),
                is_effectively_invalid(candidate[key]),
            )
        )
    rows = []
    for category, pairs in sorted(grouped.items()):
        n = len(pairs)
        base = sum(before for _, before, _, _, _ in pairs)
        cand = sum(after for _, _, after, _, _ in pairs)
        base_invalid = sum(invalid for _, _, _, invalid, _ in pairs)
        cand_invalid = sum(invalid for _, _, _, _, invalid in pairs)
        task_clusters = len({case_id for case_id, _, _, _, _ in pairs})
        rows.append(
            {
                "category": category,
                "attempts": n,
                "task_clusters": task_clusters,
                "comparison_unit": "task",
                "baseline_passed": base,
                "candidate_passed": cand,
                "baseline_invalid": base_invalid,
                "candidate_invalid": cand_invalid,
                "baseline_rate": base / n,
                "candidate_rate": cand / n,
                "role_rates": {"baseline": base / n, "candidate": cand / n},
                "delta": (cand - base) / n,
            }
        )
    return rows


def fmt_pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def fmt_p(value: float) -> str:
    """格式化精确 p 值，避免最小可达值被四舍五入成 0.0000。"""
    if not math.isfinite(value) or value < 0 or value > 1:
        raise ValueError(f"p-value must be finite and within [0, 1]: {value!r}")
    return f"{value:.6g}" if value < 1e-4 else f"{value:.4f}"


def fmt_optional_pct(value: Any) -> str:
    """格式化允许在 incomplete 状态下为空的百分比。"""
    return "NA" if value is None else fmt_pct(float(value))


def fmt_optional_p(value: Any) -> str:
    """格式化允许在 incomplete 状态下为空的 p 值。"""
    return "NA" if value is None else fmt_p(float(value))


def require_confirmatory_stats(stats: dict[str, Any]) -> None:
    """Sealed21 只有完整零-invalid 结果才允许渲染确认性统计。"""
    required = (
        "cluster_bootstrap_ci_low",
        "cluster_bootstrap_ci_high",
        "mcnemar_exact_p",
        "task_permutation_p",
    )
    if not stats.get("inference_valid") or any(stats.get(key) is None for key in required):
        raise ValueError(
            "sealed analysis requires complete confirmatory statistics; "
            f"blocked_reason={stats.get('inference_blocked_reason')}"
        )


def sealed_analysis_status(stats: dict[str, Any]) -> str:
    """根据 invalid 门禁返回确认性分析状态。"""
    return "complete" if stats.get("inference_valid") is True else "incomplete"


def evaluate_primary_success(stats: dict[str, Any], *, alpha: float) -> bool:
    """按预注册 primary endpoint 的 alpha 重算确认性成功判定。"""
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(float(alpha)) or not 0 < float(alpha) < 1:
        raise ValueError(f"primary endpoint alpha must be finite and in (0, 1): {alpha!r}")
    return bool(
        stats.get("inference_valid") is True
        and isinstance(stats.get("delta"), (int, float))
        and stats["delta"] > 0
        and isinstance(stats.get("cluster_bootstrap_ci_low"), (int, float))
        and stats["cluster_bootstrap_ci_low"] > 0
        and isinstance(stats.get("task_permutation_p"), (int, float))
        and stats["task_permutation_p"] < float(alpha)
    )


def validate_sealed_statistics_payload(payload: dict[str, Any]) -> None:
    """校验 sealed_statistics.json 的状态、invalid 计数与确认性字段闭环。"""
    if not isinstance(payload, dict):
        raise ValueError("sealed statistics payload must be an object")
    stats = payload.get("primary")
    integrity = payload.get("result_integrity")
    status = payload.get("analysis_status")
    alpha = payload.get("primary_endpoint_alpha")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(float(alpha)) or not 0 < float(alpha) < 1:
        raise ValueError("sealed statistics primary_endpoint_alpha is malformed")
    if payload.get("primary_success_rule") != PRIMARY_SUCCESS_RULE:
        raise ValueError("sealed statistics primary_success_rule is malformed")
    expected_missingness = {
        "classification": "infrastructure-invalid is neither pass nor behavioral fail",
        "reruns": 2,
        "replacement_tasks": False,
        "imputation": False,
        "completion_rule": "both harnesses must have zero unresolved invalid cells",
        "failure_rule": "if invalid cells remain after allowed reruns, report Sealed21 as incomplete",
    }
    if payload.get("missingness_policy") != expected_missingness:
        raise ValueError("sealed statistics missingness_policy is malformed")
    expected_multiplicity = {
        "primary_hypotheses": 1,
        "adjustment": "none",
        "rationale": "one pre-registered model, split, contrast, and primary endpoint",
    }
    if payload.get("multiplicity") != expected_multiplicity:
        raise ValueError("sealed statistics multiplicity is malformed")
    expected_success = evaluate_primary_success(stats, alpha=float(alpha))
    if payload.get("pre_registered_success") is not expected_success:
        raise ValueError("sealed statistics pre_registered_success does not match primary endpoint")
    if not isinstance(stats, dict) or not isinstance(integrity, dict):
        raise ValueError("sealed statistics payload is missing primary or result_integrity")
    categories = payload.get("categories")
    if not isinstance(categories, list):
        raise ValueError("sealed statistics categories must be a list")
    design = payload.get("design")
    if design is None:
        attempts_per_task = 2
        design_roles = ["baseline", "candidate"]
        design_comparison_unit = "task"
        design_observation_unit = "attempt"
    elif isinstance(design, dict):
        attempts_per_task = design.get("attempts_per_task")
        if not isinstance(attempts_per_task, int) or isinstance(attempts_per_task, bool) or attempts_per_task < 1:
            raise ValueError("sealed statistics design attempts_per_task is malformed")
        design_comparison_unit = design.get("comparison_unit", "task")
        if design_comparison_unit != "task":
            raise ValueError("sealed statistics design comparison_unit is malformed")
        design_observation_unit = design.get("observation_unit", "attempt")
        if design_observation_unit != "attempt":
            raise ValueError("sealed statistics design observation_unit is malformed")
        design_roles = design.get("roles")
        if design_roles != ["baseline", "candidate"]:
            raise ValueError("sealed statistics design roles are malformed")
        bootstrap = design.get("bootstrap")
        if bootstrap is not None:
            if not isinstance(bootstrap, dict) or bootstrap.get("unit") != "task" or bootstrap.get("resamples") != 20_000 or bootstrap.get("seed") != BOOTSTRAP_SEED or bootstrap.get("percentile_method") != BOOTSTRAP_PERCENTILE_METHOD:
                raise ValueError("sealed statistics bootstrap design is malformed")
        permutation = design.get("permutation")
        if permutation is not None:
            expected_permutation = {
                "unit": "task",
                "sidedness": "two-sided",
                "exact": True,
                "aggregation": "mean signed pass difference across attempts within task",
                "algorithm": "dynamic programming over rational task effects",
            }
            if permutation != expected_permutation:
                raise ValueError("sealed statistics permutation design is malformed")
    else:
        raise ValueError("sealed statistics design is malformed")
    seen_categories: set[str] = set()
    for row in categories:
        if not isinstance(row, dict):
            raise ValueError("sealed statistics category row must be an object")
        category = row.get("category")
        if not isinstance(category, str) or not category.strip() or category in seen_categories:
            raise ValueError("sealed statistics category names must be unique non-empty strings")
        seen_categories.add(category)
        task_clusters = row.get("task_clusters")
        attempts = row.get("attempts")
        if not isinstance(task_clusters, int) or isinstance(task_clusters, bool) or task_clusters < 1:
            raise ValueError("sealed statistics category task_clusters is malformed")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < task_clusters:
            raise ValueError("sealed statistics category attempts are malformed")
        if attempts != task_clusters * attempts_per_task:
            raise ValueError("sealed statistics category attempts do not match attempts_per_task")
        if row.get("comparison_unit") != design_comparison_unit:
            raise ValueError("sealed statistics category comparison_unit is malformed")
        rates = row.get("role_rates")
        if not isinstance(rates, dict) or set(rates) != set(design_roles):
            raise ValueError("sealed statistics category role_rates are malformed")
        for role in design_roles:
            value = rates.get(role)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
                raise ValueError("sealed statistics category role rate is malformed")
        for key in ("baseline_passed", "candidate_passed"):
            count = row.get(key)
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= attempts:
                raise ValueError("sealed statistics category passed count is malformed")
        for key in ("baseline_invalid", "candidate_invalid"):
            count = row.get(key)
            if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= attempts:
                raise ValueError("sealed statistics category invalid count is malformed")
        if row["baseline_passed"] + row["baseline_invalid"] > attempts or row["candidate_passed"] + row["candidate_invalid"] > attempts:
            raise ValueError("sealed statistics category passed and invalid counts overlap")
        if abs(float(rates[design_roles[0]]) - row["baseline_passed"] / attempts) > 1e-12 or abs(float(rates[design_roles[1]]) - row["candidate_passed"] / attempts) > 1e-12:
            raise ValueError("sealed statistics category rates disagree with passed counts")
        delta = row.get("delta")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)) or abs(float(delta) - (float(rates[design_roles[1]]) - float(rates[design_roles[0]]))) > 1e-12:
            raise ValueError("sealed statistics category delta disagrees with role rates")
    total_tasks = stats.get("tasks")
    total_attempts = stats.get("attempts")
    has_positive_primary_total = (
        isinstance(total_tasks, int)
        and not isinstance(total_tasks, bool)
        and total_tasks > 0
    ) or (
        isinstance(total_attempts, int)
        and not isinstance(total_attempts, bool)
        and total_attempts > 0
    )
    if has_positive_primary_total and not categories:
        raise ValueError("sealed statistics categories must not be empty when primary totals are positive")
    if categories:
        if isinstance(total_tasks, int) and not isinstance(total_tasks, bool) and isinstance(total_attempts, int) and not isinstance(total_attempts, bool):
            if sum(int(row["task_clusters"]) for row in categories) != total_tasks:
                raise ValueError("sealed statistics category task_clusters do not sum to primary tasks")
            if sum(int(row["attempts"]) for row in categories) != total_attempts:
                raise ValueError("sealed statistics category attempts do not sum to primary attempts")
        for category_key, primary_key in (
            ("baseline_passed", "baseline_passed"),
            ("candidate_passed", "final_passed"),
            ("baseline_invalid", "baseline_invalid"),
            ("candidate_invalid", "final_invalid"),
        ):
            primary_count = stats.get(primary_key)
            if isinstance(primary_count, int) and not isinstance(primary_count, bool):
                category_count = sum(int(row[category_key]) for row in categories)
                if category_count != primary_count:
                    raise ValueError(
                        f"sealed statistics category {category_key} does not sum to primary {primary_key}"
                    )
    if status not in {"complete", "incomplete"}:
        raise ValueError("sealed statistics analysis_status must be complete or incomplete")
    for side, count_key, keys_key, stat_key in (
        ("baseline", "baseline_unresolved_invalid", "baseline_invalid_keys", "baseline_invalid"),
        ("candidate", "candidate_unresolved_invalid", "candidate_invalid_keys", "final_invalid"),
    ):
        count = integrity.get(count_key)
        keys = integrity.get(keys_key)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(f"sealed statistics {side} invalid count is malformed")
        if not isinstance(keys, list) or len(keys) != count:
            raise ValueError(f"sealed statistics {side} invalid keys do not match count")
        if stats.get(stat_key) != count:
            raise ValueError(f"sealed statistics {side} invalid count disagrees with primary")
    confirmatory = ("mcnemar_exact_p", "task_permutation_p", "cluster_bootstrap_ci_low", "cluster_bootstrap_ci_high")
    if status == "incomplete":
        if stats.get("inference_valid") is not False:
            raise ValueError("incomplete sealed statistics must have inference_valid=false")
        if not isinstance(payload.get("inference_blocked_reason"), str) or not payload["inference_blocked_reason"].strip():
            raise ValueError("incomplete sealed statistics must record inference_blocked_reason")
        if expected_success is not False:
            raise ValueError("incomplete sealed statistics cannot claim pre_registered_success")
        if any(stats.get(field) is not None for field in confirmatory):
            raise ValueError("incomplete sealed statistics must null confirmatory fields")
    else:
        if any(integrity.get(key) != 0 for key in ("baseline_unresolved_invalid", "candidate_unresolved_invalid")):
            raise ValueError("complete sealed statistics cannot contain unresolved invalid cells")
        if stats.get("inference_valid") is not True or payload.get("inference_blocked_reason") not in (None, ""):
            raise ValueError("complete sealed statistics has inconsistent inference status")
        if not all(stats.get(field) is not None for field in confirmatory):
            raise ValueError("complete sealed statistics is missing confirmatory fields")


def render_markdown(
    *,
    stats: dict[str, Any],
    categories: list[dict[str, Any]],
    success: bool,
    container_resolution: dict[str, Any],
    baseline_result_sha256: str,
    candidate_result_sha256: str,
    manifest_sha256: str,
    freeze_sha256: str,
    container_resolution_sha256: str,
    provenance: dict[str, Any],
    primary_endpoint_alpha: float = 0.05,
    primary_success_rule: str = PRIMARY_SUCCESS_RULE,
    missingness_policy: dict[str, Any] | None = None,
    multiplicity: dict[str, Any] | None = None,
    design: dict[str, Any] | None = None,
) -> str:
    report_design = design or sealed_statistical_design_metadata(21)
    task_count = int(report_design["split_task_counts"]["sealed"])
    repeat_count = len(report_design["repeat_ids"])
    role_count = len(report_design["roles"])
    role_labels = [str(role) for role in report_design["roles"]]
    if len(role_labels) != 2:
        raise ValueError("Sealed report requires exactly two role labels")
    evaluation_cells = task_count * repeat_count * role_count
    bootstrap = report_design["bootstrap"]
    permutation = report_design["permutation"]
    bootstrap_unit = str(bootstrap["unit"])
    permutation_unit = str(permutation["unit"])
    lines = [
        f"analysis_status=`{sealed_analysis_status(stats)}`。",
        (
            f"inference_blocked_reason={stats.get('inference_blocked_reason')}。"
            if not stats.get('inference_valid')
            else "confirmatory inference passed the invalid-cell gate。"
        ),
        "# Sealed21 预注册分析",
        "",
        f"primary_endpoint_alpha={primary_endpoint_alpha:g}",
        f"primary_success_rule=`{primary_success_rule}`",
        f"missingness_policy={json.dumps(missingness_policy or {}, ensure_ascii=False, sort_keys=True)}",
        f"multiplicity={json.dumps(multiplicity or {}, ensure_ascii=False, sort_keys=True)}",
        "",
        f"结论：**{'满足' if success else '未满足'}预注册独立泛化成功标准**。",
        "",
        f"| {role_labels[0]} | {role_labels[1]} | Δ | {bootstrap_unit} 聚类 95% CI | {permutation_unit} 置换 p | 改善/退化对 |",
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {fmt_optional_pct(stats['baseline_rate'])} | {fmt_optional_pct(stats['final_rate'])} "
            f"| {fmt_optional_pct(stats['delta'])} "
            f"| [{fmt_optional_pct(stats['cluster_bootstrap_ci_low'])}, {fmt_optional_pct(stats['cluster_bootstrap_ci_high'])}] "
            f"| {fmt_optional_p(stats['task_permutation_p'])} "
            f"| {stats['improved_pairs']}/{stats['regressed_pairs']} |"
        ),
        "",
        f"成功标准：`{primary_success_rule}`，且两侧均无 unresolved invalid。",
        "",
        f"设计与完整性：{task_count} 个任务 × {repeat_count} 次 repeat × {role_count} 个角色 = {evaluation_cells} 个评测单元；"
        f"{role_labels[0]} unresolved invalid={stats['baseline_invalid']}，"
        f"{role_labels[1]} unresolved invalid={stats['final_invalid']}。",
        f"Bootstrap 重采样={bootstrap['resamples']} 次，seed={bootstrap['seed']}；"
        f"置换检验：unit={permutation['unit']}，sidedness={permutation['sidedness']}，"
        f"exact={permutation['exact']}，aggregation={permutation['aggregation']}，"
        f"algorithm={permutation['algorithm']}。",
        f"原始结果 SHA256：baseline={baseline_result_sha256}；candidate={candidate_result_sha256}。",
        f"分析输入 SHA256：manifest={manifest_sha256}；freeze={freeze_sha256}；container_resolution={container_resolution_sha256}。",
        "冻结 provenance SHA256："
        f"preregistration={provenance['preregistration_sha256']}；"
        f"sealed_config={provenance['sealed_config_sha256']}；"
        f"strict_acceptance={provenance['strict_acceptance_sha256']}。",
        "源码与依赖 bundle SHA256："
        f"execution_code={provenance['execution_code_bundle_sha256']}；"
        f"analysis_code={provenance['analysis_code_bundle_sha256']}；"
        f"dependency={provenance['dependency_bundle_sha256']}。",
        "Harness surface SHA256："
        f"baseline={provenance['baseline_surface_sha256']}；"
        f"candidate={provenance['candidate_surface_sha256']}。",
        "",
        "## 类别描述性结果",
        "",
        f"| Category | Task clusters | Attempts | {role_labels[0]} | {role_labels[1]} | {role_labels[0]} invalid | {role_labels[1]} invalid | Δ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in categories:
        role_rates = row.get("role_rates")
        if not isinstance(role_rates, dict):
            role_rates = {"baseline": row.get("baseline_rate"), "candidate": row.get("candidate_rate")}
        first_rate = role_rates.get(role_labels[0])
        second_rate = role_rates.get(role_labels[1])
        lines.append(
            f"| {row['category']} | {row.get('task_clusters', 'NA')} | {row['attempts']} | {fmt_pct(first_rate)} "
            f"| {fmt_pct(second_rate)} | {row.get('baseline_invalid', 'NA')} | {row.get('candidate_invalid', 'NA')} | {fmt_pct(row['delta'])} |"
        )
    lines.append("")
    lines.append(
        "容器基础镜像动态解析证据："
        f"{container_resolution['resolved_count']}/{container_resolution['reference_count']} "
        f"个引用已记录本地 image ID/RepoDigest；"
        f"状态为 {'complete' if container_resolution['complete'] else 'incomplete'}，且未执行 pull。"
    )
    lines.append("")
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-result", required=True, type=Path)
    parser.add_argument("--candidate-result", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--freeze", required=True, type=Path)
    parser.add_argument("--container-resolution", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest_path = args.manifest.resolve()
    freeze_path = args.freeze.resolve()
    container_resolution_path = args.container_resolution.resolve()
    manifest_sha256 = sha256_file(manifest_path)
    freeze_sha256 = sha256_file(freeze_path)
    container_resolution_sha256 = sha256_file(container_resolution_path)
    manifest = load_json(manifest_path)
    expected_tasks = validate_sealed_manifest(manifest)
    task_root = manifest_task_root(manifest_path=manifest_path, manifest=manifest)
    expected_references = set(
        selected_references(task_root=task_root, case_ids=expected_tasks)
    )
    baseline_result_path = args.baseline_result.resolve()
    candidate_result_path = args.candidate_result.resolve()
    baseline_result_sha256 = sha256_file(baseline_result_path)
    candidate_result_sha256 = sha256_file(candidate_result_path)
    baseline_payload = load_json(baseline_result_path)
    candidate_payload = load_json(candidate_result_path)
    provenance = validate_frozen_provenance(
        freeze_path=freeze_path,
        manifest_path=manifest_path,
        baseline_payload=baseline_payload,
        candidate_payload=candidate_payload,
    )
    strict_acceptance = validate_strict_acceptance_artifact(
        path=Path(str(provenance["strict_acceptance"])),
        baseline_result_path=Path(str(provenance["strict_baseline_result"])),
        candidate_result_path=Path(str(provenance["strict_candidate_result"])),
    )
    container_resolution = validate_container_resolution_evidence(
        path=container_resolution_path,
        expected_tasks=expected_tasks,
        expected_references=expected_references,
    )
    baseline, baseline_invalid_keys = validate_result(
        result_path=baseline_result_path, expected_tasks=expected_tasks, expected_repeats=2
    )
    candidate, candidate_invalid_keys = validate_result(
        result_path=candidate_result_path, expected_tasks=expected_tasks, expected_repeats=2
    )
    stats = analyze_split(baseline, candidate, "sealed", seed=BOOTSTRAP_SEED)
    analysis_status = sealed_analysis_status(stats)
    if analysis_status == "complete":
        require_confirmatory_stats(stats)
    categories = category_rows(manifest=manifest, baseline=baseline, candidate=candidate)
    endpoint = load_json(Path(str(provenance["preregistration"]))).get("primary_endpoint", {})
    primary_alpha = endpoint.get("alpha")
    success = evaluate_primary_success(stats, alpha=primary_alpha)
    require_stable_file_hash(
        baseline_result_path, baseline_result_sha256, "baseline result"
    )
    require_stable_file_hash(
        candidate_result_path, candidate_result_sha256, "candidate result"
    )
    require_stable_file_hash(manifest_path, manifest_sha256, "sealed manifest")
    require_stable_file_hash(freeze_path, freeze_sha256, "sealed freeze")
    require_stable_file_hash(
        container_resolution_path,
        container_resolution_sha256,
        "container resolution snapshot",
    )
    require_stable_frozen_inputs(
        freeze_path=freeze_path,
        manifest_path=manifest_path,
        provenance=provenance,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "self_harness.sealed_analysis.v6",
        "model": baseline_payload.get("model"),
        "manifest": str(args.manifest.resolve()),
        "manifest_sha256": manifest_sha256,
        "freeze": str(freeze_path),
        "freeze_sha256": freeze_sha256,
        "baseline_result": str(baseline_result_path),
        "baseline_result_sha256": baseline_result_sha256,
        "candidate_result": str(candidate_result_path),
        "candidate_result_sha256": candidate_result_sha256,
        "provenance_verified": True,
        "provenance": provenance,
        "strict_acceptance_verified": True,
        "strict_acceptance_decision": strict_acceptance["decision"],
        "container_resolution": str(container_resolution_path),
        "container_resolution_sha256": container_resolution_sha256,
        "analysis_input_integrity": {
            "manifest_sha256": manifest_sha256,
            "freeze_sha256": freeze_sha256,
            "container_resolution_sha256": container_resolution_sha256,
            "stable_before_after_validation": True,
            "frozen_provenance_rechecked": True,
            "task_content_hashes_rechecked": provenance["task_content_hashes_verified"],
        },
        "container_resolution_bundle_sha256": container_resolution["bundle_sha256"],
        "container_resolution_complete": container_resolution["complete"],
        "container_references_resolved": container_resolution["resolved_count"],
        "container_reference_count": container_resolution["reference_count"],
        "design": sealed_statistical_design_metadata(len(expected_tasks)),
        "result_integrity": {
            "baseline_unresolved_invalid": len(baseline_invalid_keys),
            "candidate_unresolved_invalid": len(candidate_invalid_keys),
            "baseline_invalid_keys": [list(key) for key in baseline_invalid_keys],
            "candidate_invalid_keys": [list(key) for key in candidate_invalid_keys],
            "outer_repeats": [1, 2],
            "passed_type": "JSON boolean",
            "verifier_reward": "non-null numeric",
        },
        "analysis_status": analysis_status,
        "inference_blocked_reason": stats.get("inference_blocked_reason"),
        "pre_registered_success": success,
        "primary_endpoint_alpha": primary_alpha,
        "primary_success_rule": endpoint.get("success_rule"),
        "missingness_policy": load_json(Path(str(provenance["preregistration"]))).get("missingness_policy"),
        "multiplicity": load_json(Path(str(provenance["preregistration"]))).get("multiplicity"),
        "primary": stats,
        "categories": categories,
    }
    validate_sealed_statistics_payload(payload)
    (output_dir / "sealed_statistics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "SEALED_REPORT.md").write_text(
        render_markdown(
            stats=stats,
            categories=categories,
            success=success,
            container_resolution=container_resolution,
            baseline_result_sha256=payload["baseline_result_sha256"],
            candidate_result_sha256=payload["candidate_result_sha256"],
            manifest_sha256=payload["manifest_sha256"],
            freeze_sha256=payload["freeze_sha256"],
            container_resolution_sha256=payload["container_resolution_sha256"],
            provenance=payload["provenance"],
            primary_endpoint_alpha=payload["primary_endpoint_alpha"],
            primary_success_rule=payload["primary_success_rule"],
            missingness_policy=payload["missingness_policy"],
            multiplicity=payload["multiplicity"],
            design=payload["design"],
        ),
        encoding="utf-8",
    )
    write_csv(output_dir / "sealed_categories.csv", categories)
    print(output_dir / "SEALED_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
