"""校验 EI 实验章节中的数值表格是否与机器可读产物一致。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
import subprocess
import tempfile
from datetime import datetime, timezone
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "paper" / "generated" / "PAPER_CONSISTENCY_AUDIT.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.analyze_experiments import (
    MODELS,
    ANALYSIS_SEED,
    analysis_input_paths,
    analyze_split,
    build_markdown,
    flatten_result,
    is_effectively_invalid,
    load_json,
    recompute_historical_acceptance as recompute_historical_acceptance_from_sources,
    require_stable_analysis_inputs,
    sha256_file,
    stable_hash_snapshot,
    validate_paired_result_keys,
    write_effect_svg,
    write_invalid_svg,
)
from paper.audit_container_reproducibility import (
    build_audit as rebuild_container_audit,
    render_markdown as render_container_markdown,
)
from paper.audit_confirmation_design import (
    build_audit as rebuild_design_audit,
    render_markdown as render_design_markdown,
)
from eval.scripts.capture_container_resolution import (
    selected_references,
    validate_snapshot as validate_container_resolution_snapshot,
)
from eval.scripts.capture_environment_lock import validate_lock as validate_environment_lock
from acceptance.scripts.run_acceptance_gate import build_reason, verify_acceptance_artifact
from eval.scripts.result_validity import effective_invalid_reason
from paper.audit_mechanism_evidence import (
    build_audit as rebuild_mechanism_audit,
    render_markdown as render_mechanism_markdown,
    write_csv as write_mechanism_csv,
    write_svg as write_mechanism_svg,
)
from paper.audit_split_coverage import (
    build_audit as rebuild_split_coverage_audit,
    clean_splits,
    count_rows as split_coverage_rows,
    render_markdown as render_split_coverage_markdown,
    task_metadata,
    write_svg as write_split_coverage_svg,
)
from paper.build_paired_rerun_plan import build as rebuild_paired_plan, render_markdown

MECHANISM_PROPOSAL = (
    ROOT
    / "runs"
    / "clean64-qwen-self-harness"
    / "branches"
    / "baseline"
    / "candidates"
    / "anti_workaround_execution"
    / "proposal.json"
)
MECHANISM_BASELINE_RESULT = ROOT / "runs" / "clean64-qwen-baseline" / "result.json"
MECHANISM_CANDIDATE_RESULT = (
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
SPLIT_CLEAN_CONFIG = ROOT / "eval" / "configs" / "harbor_local_clean64.toml"
SPLIT_SEALED_MANIFEST = ROOT / "configs" / "splits" / "sealed21.json"
SPLIT_TASK_ROOT = ROOT / "runs" / "terminal-bench-2-reliable-v2"
CONTAINER_TASK_ROOT = SPLIT_TASK_ROOT
DESIGN_AUDIT_SCRIPT = ROOT / "paper" / "audit_confirmation_design.py"
DESIGN_STATISTICS_SCRIPT = ROOT / "paper" / "analyze_experiments.py"
DESIGN_VALIDITY_SCRIPT = ROOT / "eval" / "scripts" / "result_validity.py"
SEALED_PLAN_PATH = ROOT / "paper" / "generated" / "sealed21-execution-plan.json"
SEALED_MANIFEST_LOCK = ROOT / "configs" / "splits" / "sealed21.json.sha256"
SEALED_PREREGISTRATION = ROOT / "configs" / "experiments" / "ei_confirmation_v1.json"
SEALED_PROTOCOL = ROOT / "paper" / "SEALED_PROTOCOL.md"
SEALED_CONFIG = ROOT / "eval" / "configs" / "harbor_local_sealed21.toml"
SEALED_BASELINE_SURFACE = ROOT / "eval" / "harness_workspace" / "repo_baseline.py"
SEALED_CANDIDATE_SURFACE = (
    ROOT
    / "runs"
    / "clean64-qwen-self-harness"
    / "branches"
    / "baseline"
    / "candidates"
    / "anti_workaround_execution"
    / "current"
    / "repo_baseline.py"
)


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.2f}%"


def fmt_pp(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:+.2f} pp"


def table_rows(text: str, heading: str) -> list[list[str]]:
    start = text.index(heading)
    tail = text[start:]
    lines = tail.splitlines()[1:]
    rows: list[list[str]] = []
    for line in lines:
        if line.startswith("## "):
            break
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] not in {"Model", "模型"}:
            rows.append(cells)
    return rows


def assert_equal(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    checks.append({"name": name, "ok": actual == expected, "actual": actual, "expected": expected})


def assert_close(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    ok = (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and math.isfinite(float(actual))
        and math.isfinite(float(expected))
        and math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12)
    )
    checks.append({"name": name, "ok": ok, "actual": actual, "expected": expected})


def provenance_checks(
    stats: dict[str, Any],
    paired: dict[str, Any],
    phase_manifests: list[tuple[str, dict[str, Any]]],
    single_plans: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def resolve_recorded_path(value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    def check_file_hash(name: str, path_value: Any, expected: Any) -> None:
        path = resolve_recorded_path(path_value)
        actual = sha256_file(path) if path is not None and path.is_file() else None
        assert_equal(checks, name, actual, expected)

    integrity = stats.get("analysis_input_integrity", {})
    assert_equal(
        checks,
        "statistics.analysis_input_integrity",
        integrity.get("stable_before_after_analysis"),
        True,
    )
    assert_equal(
        checks,
        "statistics.tracked_input_count_positive",
        isinstance(integrity.get("tracked_input_count"), int) and integrity["tracked_input_count"] > 0,
        True,
    )
    source_files = stats.get("source_files", [])
    assert_equal(checks, "statistics.source_files_nonempty", isinstance(source_files, list) and bool(source_files), True)
    assert_equal(
        checks,
        "statistics.source_files_count_matches_tracked",
        len(source_files) == integrity.get("tracked_input_count") if isinstance(source_files, list) else False,
        True,
    )
    recorded_source_paths: set[str] = set()
    for index, record in enumerate(source_files if isinstance(source_files, list) else []):
        path_value = record.get("path") if isinstance(record, dict) else None
        digest = record.get("sha256") if isinstance(record, dict) else None
        resolved_recorded_path = resolve_recorded_path(path_value)
        if resolved_recorded_path is not None:
            recorded_source_paths.add(str(resolved_recorded_path.resolve()))
        assert_equal(
            checks,
            f"statistics.source_files[{index}].sha256_format",
            isinstance(digest, str) and len(digest) == 64,
            True,
        )
        check_file_hash(f"statistics.source_files[{index}].sha256_matches", path_value, digest)
    expected_source_paths = {
        str(path.resolve())
        for path in analysis_input_paths()
    }
    expected_source_paths.update(
        {
            str((ROOT / "paper" / "analyze_experiments.py").resolve()),
            str((ROOT / "eval" / "scripts" / "result_validity.py").resolve()),
            str((ROOT / "acceptance" / "scripts" / "run_acceptance_gate.py").resolve()),
        }
    )
    assert_equal(
        checks,
        "statistics.source_files_set_matches_discovered_inputs",
        sorted(recorded_source_paths),
        sorted(expected_source_paths),
    )
    check_file_hash(
        "paired.baseline_result_sha256_matches",
        paired.get("baseline_result"),
        paired.get("baseline_result_sha256"),
    )
    check_file_hash(
        "paired.candidate_result_sha256_matches",
        paired.get("candidate_result"),
        paired.get("candidate_result_sha256"),
    )
    assert_equal(checks, "paired.source_hashes_stable", paired.get("source_hashes_stable"), True)
    for name, payload in phase_manifests:
        assert_equal(checks, f"{name}.format", payload.get("format"), "self_harness.rerun_cell_manifest.v3")
        assert_equal(checks, f"{name}.source_hashes_stable", payload.get("source_hashes_stable"), True)
        for role in ("baseline", "candidate"):
            digest = payload.get(f"{role}_result_sha256")
            assert_equal(checks, f"{name}.{role}_sha256_present", isinstance(digest, str) and len(digest) == 64, True)
            check_file_hash(
                f"{name}.{role}_sha256_matches",
                payload.get(f"{role}_result"),
                digest,
            )
    for name, payload in single_plans:
        selection = payload.get("selection")
        if selection == "cell_manifest":
            expected_side = "candidate" if payload.get("candidate_workspace") else "baseline"
            assert_equal(checks, f"{name}.selection_side_present", payload.get("selection_side"), expected_side)
            assert_equal(
                checks,
                f"{name}.selection_phase_valid",
                payload.get("selection_phase") in {
                    "phase_0_infrastructure_canary",
                    "phase_1_one_sided_bridge",
                    "phase_2_both_sides_missing",
                },
                True,
            )
        else:
            assert_equal(checks, f"{name}.selection_side_null_for_full_plan", payload.get("selection_side"), None)
            assert_equal(checks, f"{name}.selection_phase_null_for_full_plan", payload.get("selection_phase"), None)
        assert_equal(checks, f"{name}.source_result_stable", payload.get("source_result_stable"), True)
        digest = payload.get("source_result_sha256")
        assert_equal(checks, f"{name}.source_result_sha256_present", isinstance(digest, str) and len(digest) == 64, True)
        execution = payload.get("execution_provenance")
        if execution is None:
            check_file_hash(f"{name}.source_result_sha256_matches", payload.get("source_result"), digest)
        else:
            assert_equal(checks, f"{name}.execution.pre_hash_matches_plan", execution.get("pre_source_result_sha256"), digest)
            status = execution.get("status")
            assert_equal(checks, f"{name}.execution.status_known", status in {"completed", "evaluator_failed"}, True)
            returncode = execution.get("evaluator_returncode")
            assert_equal(checks, f"{name}.execution.returncode_integer", isinstance(returncode, int) and not isinstance(returncode, bool), True)
            if status == "completed":
                post_digest = execution.get("post_source_result_sha256")
                assert_equal(checks, f"{name}.execution.post_hash_format", isinstance(post_digest, str) and len(post_digest) == 64, True)
                check_file_hash(f"{name}.execution.post_hash_matches_disk", payload.get("source_result"), post_digest)
                assert_equal(checks, f"{name}.execution.post_source_result_stable", execution.get("post_source_result_stable"), True)
                assert_equal(checks, f"{name}.execution.returncode_success", returncode, 0)
                source_path = resolve_recorded_path(payload.get("source_result"))
                try:
                    result = load_json(source_path) if source_path is not None else {}
                    invalid_keys: set[tuple[str, int, str]] = set()
                    for split, repeats in result.get("splits", {}).items():
                        if not isinstance(repeats, list):
                            continue
                        for repeat_record in repeats:
                            if not isinstance(repeat_record, dict):
                                continue
                            repeat_value = repeat_record.get("repeat")
                            if isinstance(repeat_value, bool):
                                continue
                            try:
                                repeat = int(repeat_value)
                            except (TypeError, ValueError):
                                continue
                            for case in repeat_record.get("case_results", []):
                                if not isinstance(case, dict):
                                    continue
                                case_id = case.get("case_id")
                                if not isinstance(case_id, str):
                                    continue
                                if effective_invalid_reason(case, require_reward=True) is not None:
                                    invalid_keys.add((str(split), repeat, case_id))
                    selected_keys = {
                        (str(item.get("split")), int(item.get("repeat")), str(item.get("case_id")))
                        for item in payload.get("cases", [])
                        if isinstance(item, dict)
                        and not isinstance(item.get("repeat"), bool)
                    }
                    assert_equal(checks, f"{name}.execution.global_invalid_after_matches_result", execution.get("global_invalid_after"), len(invalid_keys))
                    assert_equal(checks, f"{name}.execution.selected_invalid_after_matches_result", execution.get("selected_invalid_after"), len(invalid_keys & selected_keys))
                except (OSError, TypeError, ValueError, AttributeError):
                    assert_equal(checks, f"{name}.execution.post_result_recomputable", False, True)
            else:
                assert_equal(checks, f"{name}.execution.post_source_result_stable_failed", execution.get("post_source_result_stable"), False)
        if payload.get("selection_phase") == "phase_0_infrastructure_canary":
            outcome = payload.get("canary_outcome")
            if outcome is not None:
                required_counts = (
                    "selected_cell_count",
                    "numeric_verifier_outcome_count",
                    "infrastructure_invalid_count",
                    "missing_cell_count",
                    "behavioral_pass_count",
                    "behavioral_fail_count",
                )
                for field in required_counts:
                    value = outcome.get(field) if isinstance(outcome, dict) else None
                    assert_equal(
                        checks,
                        f"{name}.canary_outcome.{field}_nonnegative_int",
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                        True,
                    )
                selected_count = outcome.get("selected_cell_count") if isinstance(outcome, dict) else None
                numeric_count = outcome.get("numeric_verifier_outcome_count") if isinstance(outcome, dict) else None
                invalid_count = outcome.get("infrastructure_invalid_count") if isinstance(outcome, dict) else None
                missing_count = outcome.get("missing_cell_count") if isinstance(outcome, dict) else None
                details = outcome.get("details") if isinstance(outcome, dict) else None
                assert_equal(
                    checks,
                    f"{name}.canary_outcome.counts_sum_to_selected",
                    numeric_count + invalid_count + missing_count
                    if all(isinstance(value, int) and not isinstance(value, bool) for value in (numeric_count, invalid_count, missing_count))
                    else None,
                    selected_count,
                )
                assert_equal(
                    checks,
                    f"{name}.canary_outcome.details_count",
                    len(details) if isinstance(details, list) else None,
                    selected_count,
                )
                assert_equal(
                    checks,
                    f"{name}.canary_outcome.ready_matches_numeric_count",
                    outcome.get("canary_ready") if isinstance(outcome, dict) else None,
                    isinstance(selected_count, int)
                    and selected_count > 0
                    and numeric_count == selected_count,
                )
    return checks


def candidate_provenance_checks(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """把候选表行绑定到 acceptance 和两侧原始结果，而不是只比较已展开数字。"""
    checks: list[dict[str, Any]] = []
    source_files = stats.get("source_files", [])
    source_paths: set[str] = set()
    for record in source_files if isinstance(source_files, list) else []:
        if isinstance(record, dict) and isinstance(record.get("path"), str):
            path = Path(record["path"])
            source_paths.add(str((path if path.is_absolute() else ROOT / path).resolve()))

    def resolve(value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    candidates = stats.get("candidates", [])
    assert_equal(checks, "statistics.candidates_nonempty", isinstance(candidates, list) and bool(candidates), True)
    for index, row in enumerate(candidates if isinstance(candidates, list) else []):
        prefix = f"statistics.candidates[{index}]"
        acceptance_path = resolve(row.get("acceptance_path") if isinstance(row, dict) else None)
        acceptance_key = str(acceptance_path.resolve()) if acceptance_path is not None else None
        assert_equal(checks, f"{prefix}.acceptance_path_exists", acceptance_path is not None and acceptance_path.is_file(), True)
        assert_equal(checks, f"{prefix}.acceptance_path_in_source_files", acceptance_key in source_paths, True)
        if acceptance_path is None or not acceptance_path.is_file():
            continue
        acceptance = load_json(acceptance_path)
        baseline_path = resolve(acceptance.get("baseline_result"))
        candidate_path = resolve(acceptance.get("candidate_result"))
        expected_decision_source = (
            "strict" if acceptance_path.name == "acceptance.strict.json" else "historical"
        )
        assert_equal(
            checks,
            f"{prefix}.decision_source_matches_artifact_name",
            row.get("decision_source"),
            expected_decision_source,
        )
        expected_format = (
            "self_harness.acceptance_gate.v1"
            if expected_decision_source == "strict"
            else "self_harness.acceptance_gate.v0"
        )
        assert_equal(
            checks,
            f"{prefix}.acceptance_format_matches_source_kind",
            acceptance.get("format"),
            expected_format,
        )
        try:
            if baseline_path is None or candidate_path is None or not baseline_path.is_file() or not candidate_path.is_file():
                raise ValueError("acceptance source result path is missing")
            baseline_rows = flatten_result(baseline_path)
            candidate_rows = flatten_result(candidate_path)
            validate_paired_result_keys(
                baseline_rows,
                candidate_rows,
                label=f"historical acceptance {acceptance_path.name}",
            )
            if expected_decision_source == "strict":
                verify_acceptance_artifact(
                    artifact=acceptance,
                    baseline_result_path=baseline_path,
                    candidate_result_path=candidate_path,
                    splits=("train", "heldout"),
                    expected_repeats=2,
                )
                assert_equal(checks, f"{prefix}.strict_acceptance_reverified", True, True)
            else:
                recomputed_acceptance = recompute_historical_acceptance_from_sources(
                    baseline_path,
                    candidate_path,
                )
                for field in ("accepted", "decision", "reason", "rule", "splits"):
                    assert_equal(
                        checks,
                        f"{prefix}.{field}_matches_recomputed_acceptance",
                        acceptance.get(field),
                        recomputed_acceptance[field],
                    )
        except (OSError, TypeError, ValueError, KeyError) as exc:
            checks.append(
                {
                    "name": f"{prefix}.acceptance_recomputation",
                    "ok": False,
                    "actual": repr(exc),
                    "expected": "historical acceptance recomputes successfully",
                }
            )
        for role, result_path in (("baseline", baseline_path), ("candidate", candidate_path)):
            result_key = str(result_path.resolve()) if result_path is not None else None
            assert_equal(checks, f"{prefix}.{role}_result_exists", result_path is not None and result_path.is_file(), True)
            assert_equal(checks, f"{prefix}.{role}_result_in_source_files", result_key in source_paths, True)
        assert_equal(checks, f"{prefix}.decision_matches_acceptance", row.get("decision"), acceptance.get("decision"))
        splits = acceptance.get("splits", {})
        for split in ("train", "heldout"):
            expected_delta = splits.get(split, {}).get("delta") if isinstance(splits, dict) else None
            assert_close(checks, f"{prefix}.{split}_delta_matches_acceptance", row.get(f"{split}_delta"), expected_delta)
        if baseline_path is None or candidate_path is None or not baseline_path.is_file() or not candidate_path.is_file():
            continue
        baseline_rows = flatten_result(baseline_path)
        candidate_rows = flatten_result(candidate_path)
        try:
            validate_paired_result_keys(
                baseline_rows,
                candidate_rows,
                label=f"{prefix} result files",
            )
            paired_keys_match = True
        except ValueError:
            paired_keys_match = False
        assert_equal(checks, f"{prefix}.result_key_sets_match", paired_keys_match, True)
        baseline_invalid = sum(is_effectively_invalid(case) for case in baseline_rows.values())
        candidate_invalid = sum(is_effectively_invalid(case) for case in candidate_rows.values())
        assert_equal(checks, f"{prefix}.baseline_invalid_matches_result", row.get("baseline_invalid"), baseline_invalid)
        assert_equal(checks, f"{prefix}.candidate_invalid_matches_result", row.get("candidate_invalid"), candidate_invalid)
        assert_equal(
            checks,
            f"{prefix}.strict_gate_reproducible_matches_results",
            row.get("strict_gate_reproducible"),
            baseline_invalid == 0 and candidate_invalid == 0,
        )
    return checks


def paired_plan_semantic_checks(paired: dict[str, Any]) -> list[dict[str, Any]]:
    """从绑定的两侧结果重新构建配对计划，防止只改计划数字而保留旧哈希。"""
    checks: list[dict[str, Any]] = []
    limits = paired.get("limits", {})
    try:
        rebuilt = rebuild_paired_plan(
            baseline_path=Path(str(paired["baseline_result"])),
            candidate_path=Path(str(paired["candidate_result"])),
            timeout_s=float(limits["timeout_s"]),
            retries=int(limits["infrastructure_retries"]),
            concurrency=int(limits["case_concurrency"]),
        )
    except Exception as exc:  # pragma: no cover - exact failure is recorded below
        checks.append({"name": "paired.rebuild_from_sources", "ok": False, "actual": repr(exc), "expected": "successful rebuild"})
        return checks
    assert_equal(checks, "paired.rebuild_from_sources", True, True)
    for key in ("pair_counts", "incomplete_pairs", "rerun_cells", "reason_counts", "limits", "total_budget", "phases"):
        assert_equal(checks, f"paired.{key}_matches_rebuild", paired.get(key), rebuilt.get(key))
    return checks


def phase_manifest_semantic_checks(
    paired: dict[str, Any], phase_manifests: list[tuple[str, dict[str, Any]]]
) -> list[dict[str, Any]]:
    """逐 cell 验证 phase manifest 与 paired plan 的 side/phase 映射一致。"""
    checks: list[dict[str, Any]] = []
    phases = paired.get("phases", {})
    for name, manifest in phase_manifests:
        phase_name = manifest.get("phase")
        side = manifest.get("side")
        phase = phases.get(phase_name) if isinstance(phases, dict) else None
        assert_equal(checks, f"{name}.phase_known", isinstance(phase, dict), True)
        assert_equal(checks, f"{name}.side_known", side in {"baseline", "candidate"}, True)
        if not isinstance(phase, dict) or side not in {"baseline", "candidate"}:
            continue
        expected_cells = [
            {key: item[key] for key in ("split", "repeat", "case_id")}
            for item in phase.get("cells", [])
            if item.get("side") == side
        ]
        assert_equal(checks, f"{name}.cells_match_paired_plan", manifest.get("cells"), expected_cells)
        for role in ("baseline", "candidate"):
            assert_equal(
                checks,
                f"{name}.{role}_path_matches_plan",
                manifest.get(f"{role}_result"),
                paired.get(f"{role}_result"),
            )
            assert_equal(
                checks,
                f"{name}.{role}_sha256_matches_plan",
                manifest.get(f"{role}_result_sha256"),
                paired.get(f"{role}_result_sha256"),
            )
    return checks


def paired_derived_artifact_checks(
    paired: dict[str, Any],
    *,
    markdown: str | None = None,
    csv_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """验证 JSON paired plan 的 Markdown/CSV 派生产物未被手工改写。"""
    checks: list[dict[str, Any]] = []
    output_dir = ROOT / "paper" / "generated" / "paired-rerun"
    if markdown is None:
        markdown_path = output_dir / "PAIRED_RERUN_PLAN.md"
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else None
    assert_equal(checks, "paired.markdown_exists", markdown is not None, True)
    if markdown is not None:
        assert_equal(checks, "paired.markdown_matches_json", markdown, render_markdown(paired))
    if csv_rows is None:
        csv_path = output_dir / "paired_rerun_cells.csv"
        if csv_path.is_file():
            with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
        else:
            csv_rows = None
    assert_equal(checks, "paired.csv_exists", csv_rows is not None, True)
    expected_rows: list[dict[str, str]] = []
    for phase_name, phase in paired.get("phases", {}).items():
        for item in phase.get("cells", []):
            expected_rows.append(
                {
                    "phase": str(phase_name),
                    "side": str(item["side"]),
                    "split": str(item["split"]),
                    "repeat": str(item["repeat"]),
                    "case_id": str(item["case_id"]),
                    "reason": str(item["reason"]),
                    "reason_category": str(item["reason_category"]),
                }
            )
    if csv_rows is not None:
        assert_equal(checks, "paired.csv_rows_match_json", csv_rows, expected_rows)
    return checks


def statistics_derived_artifact_checks(
    stats: dict[str, Any],
    *,
    summary_rows: list[dict[str, str]] | None = None,
    candidate_rows: list[dict[str, str]] | None = None,
    markdown: str | None = None,
) -> list[dict[str, Any]]:
    """验证 statistics JSON 的 CSV/Markdown 派生产物未脱离单一来源。"""
    checks: list[dict[str, Any]] = []
    output_dir = ROOT / "paper" / "generated"

    def read_csv(path: Path) -> list[dict[str, str]] | None:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    if summary_rows is None:
        summary_rows = read_csv(output_dir / "summary.csv")
    if candidate_rows is None:
        candidate_rows = read_csv(output_dir / "candidates.csv")
    if markdown is None:
        markdown_path = output_dir / "STATISTICAL_AUDIT.md"
        markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else None
    assert_equal(checks, "statistics.summary_csv_exists", summary_rows is not None, True)
    assert_equal(checks, "statistics.candidates_csv_exists", candidate_rows is not None, True)
    assert_equal(checks, "statistics.audit_markdown_exists", markdown is not None, True)

    def expected_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [{key: "" if value is None else str(value) for key, value in row.items()} for row in rows]

    if summary_rows is not None:
        assert_equal(checks, "statistics.summary_csv_matches_json", summary_rows, expected_rows(stats.get("summary", [])))
    if candidate_rows is not None:
        assert_equal(checks, "statistics.candidates_csv_matches_json", candidate_rows, expected_rows(stats.get("candidates", [])))
    if markdown is not None:
        assert_equal(
            checks,
            "statistics.audit_markdown_matches_json",
            markdown,
            build_markdown(stats.get("summary", []), stats.get("candidates", [])),
        )
    return checks


def statistics_figure_checks(
    stats: dict[str, Any],
    *,
    effect_svg: str | bytes | None = None,
    invalid_svg: str | bytes | None = None,
) -> list[dict[str, Any]]:
    """从 statistics 汇总重新生成并核对论文 SVG 图形。"""
    checks: list[dict[str, Any]] = []
    output_dir = ROOT / "paper" / "generated"
    if effect_svg is None:
        path = output_dir / "effect_sizes.svg"
        effect_svg = path.read_bytes() if path.is_file() else None
    if invalid_svg is None:
        path = output_dir / "invalid_runs.svg"
        invalid_svg = path.read_bytes() if path.is_file() else None
    if isinstance(effect_svg, str):
        effect_svg = effect_svg.encode("utf-8")
    if isinstance(invalid_svg, str):
        invalid_svg = invalid_svg.encode("utf-8")
    assert_equal(checks, "statistics.effect_svg_exists", effect_svg is not None, True)
    assert_equal(checks, "statistics.invalid_svg_exists", invalid_svg is not None, True)
    with tempfile.TemporaryDirectory(prefix="self_harness_audit_") as temp_dir:
        temp = Path(temp_dir)
        expected_effect_path = temp / "effect_sizes.svg"
        expected_invalid_path = temp / "invalid_runs.svg"
        write_effect_svg(expected_effect_path, stats.get("summary", []))
        write_invalid_svg(expected_invalid_path, stats.get("summary", []))
        if effect_svg is not None:
            expected_effect = expected_effect_path.read_bytes()
            checks.append(
                {
                    "name": "statistics.effect_svg_matches_json",
                    "ok": effect_svg == expected_effect,
                    "actual_sha256": hashlib.sha256(effect_svg).hexdigest(),
                    "expected_sha256": hashlib.sha256(expected_effect).hexdigest(),
                    "actual_bytes": len(effect_svg),
                    "expected_bytes": len(expected_effect),
                }
            )
        if invalid_svg is not None:
            expected_invalid = expected_invalid_path.read_bytes()
            checks.append(
                {
                    "name": "statistics.invalid_svg_matches_json",
                    "ok": invalid_svg == expected_invalid,
                    "actual_sha256": hashlib.sha256(invalid_svg).hexdigest(),
                    "expected_sha256": hashlib.sha256(expected_invalid).hexdigest(),
                    "actual_bytes": len(invalid_svg),
                    "expected_bytes": len(expected_invalid),
                }
            )
    return checks


def _record_byte_match(
    checks: list[dict[str, Any]], name: str, actual: bytes | None, expected: bytes
) -> None:
    checks.append(
        {
            "name": name,
            "ok": actual is not None and actual == expected,
            "actual_sha256": hashlib.sha256(actual).hexdigest() if actual is not None else None,
            "expected_sha256": hashlib.sha256(expected).hexdigest(),
            "actual_bytes": len(actual) if actual is not None else None,
            "expected_bytes": len(expected),
        }
    )


def supplementary_artifact_checks(
    *,
    mechanism_svg: str | bytes | None = None,
    split_svg: str | bytes | None = None,
) -> list[dict[str, Any]]:
    """核对正文引用的机制证据和划分覆盖派生产物。"""
    checks: list[dict[str, Any]] = []
    mechanism_dir = ROOT / "paper" / "generated" / "mechanism"
    split_dir = ROOT / "paper" / "generated" / "split-coverage"
    mechanism_json_path = mechanism_dir / "mechanism_evidence.json"
    split_json_path = split_dir / "split_coverage.json"
    mechanism = load_json(mechanism_json_path) if mechanism_json_path.is_file() else None
    split = load_json(split_json_path) if split_json_path.is_file() else None
    assert_equal(checks, "mechanism.json_exists", mechanism is not None, True)
    assert_equal(checks, "split_coverage.json_exists", split is not None, True)
    if mechanism is None or split is None:
        return checks

    assert_equal(
        checks,
        "mechanism.format",
        mechanism.get("format"),
        "self_harness.mechanism_evidence_audit.v1",
    )
    assert_equal(
        checks,
        "split_coverage.format",
        split.get("format"),
        "self_harness.split_coverage_audit.v1",
    )
    if mechanism_svg is None:
        mechanism_svg = (
            mechanism_dir / "mechanism_evidence.svg"
        ).read_bytes() if (mechanism_dir / "mechanism_evidence.svg").is_file() else None
    if split_svg is None:
        split_svg = (
            split_dir / "split_coverage.svg"
        ).read_bytes() if (split_dir / "split_coverage.svg").is_file() else None
    if isinstance(mechanism_svg, str):
        mechanism_svg = mechanism_svg.encode("utf-8")
    if isinstance(split_svg, str):
        split_svg = split_svg.encode("utf-8")
    assert_equal(checks, "mechanism.svg_exists", mechanism_svg is not None, True)
    assert_equal(checks, "split_coverage.svg_exists", split_svg is not None, True)

    with tempfile.TemporaryDirectory(prefix="self_harness_supplementary_audit_") as temp_dir:
        temp = Path(temp_dir)
        expected_mechanism_md = temp / "MECHANISM_EVIDENCE.md"
        expected_mechanism_csv = temp / "mechanism_transitions.csv"
        expected_mechanism_svg = temp / "mechanism_evidence.svg"
        expected_split_md = temp / "SPLIT_COVERAGE_AUDIT.md"
        expected_split_csv = temp / "split_coverage_counts.csv"
        expected_split_svg = temp / "split_coverage.svg"
        expected_mechanism_md.write_text(render_mechanism_markdown(mechanism), encoding="utf-8")
        write_mechanism_csv(expected_mechanism_csv, mechanism.get("transitions", []))
        write_mechanism_svg(expected_mechanism_svg, mechanism)
        expected_split_md.write_text(render_split_coverage_markdown(split), encoding="utf-8")
        rows = split_coverage_rows(split)
        with expected_split_csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        write_split_coverage_svg(expected_split_svg, split)

        artifact_pairs = [
            ("mechanism.markdown_matches_json", mechanism_dir / "MECHANISM_EVIDENCE.md", expected_mechanism_md),
            ("mechanism.csv_matches_json", mechanism_dir / "mechanism_transitions.csv", expected_mechanism_csv),
            ("mechanism.svg_matches_json", None, expected_mechanism_svg),
            ("split_coverage.markdown_matches_json", split_dir / "SPLIT_COVERAGE_AUDIT.md", expected_split_md),
            ("split_coverage.csv_matches_json", split_dir / "split_coverage_counts.csv", expected_split_csv),
            ("split_coverage.svg_matches_json", None, expected_split_svg),
        ]
        for name, actual_path, expected_path in artifact_pairs:
            if name == "mechanism.svg_matches_json":
                actual = mechanism_svg
            elif name == "split_coverage.svg_matches_json":
                actual = split_svg
            else:
                actual = actual_path.read_bytes() if actual_path is not None and actual_path.is_file() else None
            _record_byte_match(checks, name, actual, expected_path.read_bytes())
    return checks


def _source_record(role: str, path: Path) -> dict[str, str]:
    resolved = path.resolve()
    return {"role": role, "path": str(resolved), "sha256": sha256_file(resolved)}


def _canonical_json_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _source_bundle_checks(
    checks: list[dict[str, Any]],
    name: str,
    actual_payload: dict[str, Any],
    expected_payload: dict[str, Any],
    expected_sources: list[dict[str, str]],
) -> None:
    actual_sources = actual_payload.get("source_files")
    assert_equal(checks, f"{name}.source_files_count", len(actual_sources) if isinstance(actual_sources, list) else None, len(expected_sources))
    for index, expected in enumerate(expected_sources):
        actual = actual_sources[index] if isinstance(actual_sources, list) and index < len(actual_sources) else None
        assert_equal(checks, f"{name}.source_files[{index}].role", actual.get("role") if isinstance(actual, dict) else None, expected["role"])
        assert_equal(checks, f"{name}.source_files[{index}].path", actual.get("path") if isinstance(actual, dict) else None, expected["path"])
        actual_hash = sha256_file(Path(expected["path"])) if Path(expected["path"]).is_file() else None
        assert_equal(checks, f"{name}.source_files[{index}].sha256_recorded", actual.get("sha256") if isinstance(actual, dict) else None, expected["sha256"])
        assert_equal(checks, f"{name}.source_files[{index}].sha256_matches_disk", actual_hash, expected["sha256"])
    assert_equal(
        checks,
        f"{name}.json_rebuild_matches_sources",
        _canonical_json_digest(actual_payload),
        _canonical_json_digest(expected_payload),
    )


def supplementary_provenance_checks(
    *,
    mechanism: dict[str, Any] | None = None,
    split: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从规范原始输入重建两组补充 JSON，并核对其 provenance。"""
    checks: list[dict[str, Any]] = []
    if mechanism is None:
        mechanism = load_json(ROOT / "paper" / "generated" / "mechanism" / "mechanism_evidence.json")
    if split is None:
        split = load_json(ROOT / "paper" / "generated" / "split-coverage" / "split_coverage.json")

    mechanism_sources = [
        _source_record("proposal", MECHANISM_PROPOSAL),
        _source_record("baseline_result", MECHANISM_BASELINE_RESULT),
        _source_record("candidate_result", MECHANISM_CANDIDATE_RESULT),
    ]
    expected_mechanism = rebuild_mechanism_audit(
        proposal=load_json(MECHANISM_PROPOSAL),
        baseline_path=MECHANISM_BASELINE_RESULT,
        candidate_path=MECHANISM_CANDIDATE_RESULT,
        source_files=mechanism_sources,
    )
    _source_bundle_checks(
        checks,
        "mechanism",
        mechanism,
        expected_mechanism,
        mechanism_sources,
    )

    manifest = load_json(SPLIT_SEALED_MANIFEST)
    clean = clean_splits(SPLIT_CLEAN_CONFIG)
    sealed = {str(item["case_id"]) for item in manifest["tasks"]}
    excluded = {str(item["case_id"]) for item in manifest["excluded_unused_tasks"]}
    universe = {
        path.name
        for path in SPLIT_TASK_ROOT.iterdir()
        if path.is_dir() and (path / "task.toml").is_file()
    }
    metadata = {case_id: task_metadata(SPLIT_TASK_ROOT / case_id) for case_id in universe}
    split_sources = [
        _source_record("clean_config", SPLIT_CLEAN_CONFIG),
        _source_record("sealed_manifest", SPLIT_SEALED_MANIFEST),
        *[
            _source_record("task_metadata", SPLIT_TASK_ROOT / case_id / "task.toml")
            for case_id in sorted(universe)
        ],
    ]
    expected_split = rebuild_split_coverage_audit(
        clean=clean,
        sealed=sealed,
        excluded=excluded,
        universe=universe,
        metadata=metadata,
        source_files=split_sources,
    )
    _source_bundle_checks(checks, "split_coverage", split, expected_split, split_sources)
    return checks


def container_artifact_checks(
    *, container: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """从任务 Dockerfile/元数据重建容器复现审计及其发布文件。"""
    checks: list[dict[str, Any]] = []
    output_dir = ROOT / "paper" / "generated" / "container-audit"
    json_path = output_dir / "container_reproducibility.json"
    actual = container if container is not None else load_json(json_path)
    task_dirs = sorted(
        (
            path
            for path in CONTAINER_TASK_ROOT.iterdir()
            if path.is_dir() and (path / "task.toml").is_file()
        ),
        key=lambda path: path.name,
    )
    source_files: list[dict[str, str]] = []
    for task_dir in task_dirs:
        source_files.append(_source_record("task_metadata", task_dir / "task.toml"))
        source_files.extend(
            _source_record("dockerfile", dockerfile)
            for dockerfile in sorted(task_dir.rglob("Dockerfile*"), key=lambda path: path.as_posix())
        )
    expected = rebuild_container_audit(CONTAINER_TASK_ROOT, source_files=source_files)
    _source_bundle_checks(checks, "container", actual, expected, source_files)

    with tempfile.TemporaryDirectory(prefix="self_harness_container_audit_") as temp_dir:
        temp = Path(temp_dir)
        expected_md = temp / "CONTAINER_REPRODUCIBILITY_AUDIT.md"
        expected_csv = temp / "container_base_images.csv"
        expected_md.write_text(render_container_markdown(expected), encoding="utf-8")
        rows = [
            {"case_id": item["case_id"], **reference}
            for item in expected["tasks"]
            for reference in item["references"]
        ]
        with expected_csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        actual_md = (
            output_dir / "CONTAINER_REPRODUCIBILITY_AUDIT.md"
        ).read_bytes() if (output_dir / "CONTAINER_REPRODUCIBILITY_AUDIT.md").is_file() else None
        actual_csv = (
            output_dir / "container_base_images.csv"
        ).read_bytes() if (output_dir / "container_base_images.csv").is_file() else None
        _record_byte_match(checks, "container.markdown_matches_json", actual_md, expected_md.read_bytes())
        _record_byte_match(checks, "container.csv_matches_json", actual_csv, expected_csv.read_bytes())
    return checks


def design_artifact_checks(*, design: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """从固定预注册规则和统计代码重建 Sealed21 设计分辨率审计。"""
    checks: list[dict[str, Any]] = []
    output_dir = ROOT / "paper" / "generated" / "design-audit"
    json_path = output_dir / "confirmation_design_audit.json"
    actual = design if design is not None else load_json(json_path)
    source_files = [
        _source_record("design_audit_script", DESIGN_AUDIT_SCRIPT),
        _source_record("statistics_script", DESIGN_STATISTICS_SCRIPT),
        _source_record("validity_script", DESIGN_VALIDITY_SCRIPT),
        _source_record("preregistration", SEALED_PREREGISTRATION),
    ]
    expected = rebuild_design_audit(source_files=source_files)
    _source_bundle_checks(checks, "design", actual, expected, source_files)
    expected_md = render_design_markdown(expected).encode("utf-8")
    actual_md = (
        output_dir / "CONFIRMATION_DESIGN_AUDIT.md"
    ).read_bytes() if (output_dir / "CONFIRMATION_DESIGN_AUDIT.md").is_file() else None
    _record_byte_match(checks, "design.markdown_matches_json", actual_md, expected_md)
    return checks


def container_resolution_preview_checks(
    *, snapshot: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """校验 Sealed21 执行前的无 pull 容器解析预览。"""
    checks: list[dict[str, Any]] = []
    preview_path = (
        ROOT / "paper" / "generated" / "container-audit" / "sealed21_container_resolution.preview.json"
    )
    snapshot = snapshot if snapshot is not None else load_json(preview_path)
    try:
        validate_container_resolution_snapshot(snapshot)
        valid = True
    except ValueError:
        valid = False
    assert_equal(checks, "container_resolution_preview.valid_snapshot", valid, True)
    manifest = load_json(SPLIT_SEALED_MANIFEST)
    expected_case_ids = sorted(str(item["case_id"]) for item in manifest["tasks"])
    assert_equal(checks, "container_resolution_preview.format", snapshot.get("format"), "self_harness.container_resolution.v1")
    assert_equal(checks, "container_resolution_preview.outcome_blind", snapshot.get("outcome_blind"), True)
    assert_equal(checks, "container_resolution_preview.pull_performed", snapshot.get("pull_performed"), False)
    assert_equal(checks, "container_resolution_preview.case_ids", snapshot.get("case_ids"), expected_case_ids)

    expected_refs = selected_references(task_root=SPLIT_TASK_ROOT, case_ids=set(expected_case_ids))
    actual_images = snapshot.get("images", [])
    actual_refs = sorted(image.get("reference") for image in actual_images if isinstance(image, dict))
    assert_equal(checks, "container_resolution_preview.reference_count", snapshot.get("reference_count"), len(expected_refs))
    assert_equal(checks, "container_resolution_preview.references", actual_refs, sorted(expected_refs))
    expected_uses = {reference: uses for reference, uses in expected_refs.items()}
    actual_uses = {
        image.get("reference"): image.get("used_by")
        for image in actual_images
        if isinstance(image, dict)
    }
    assert_equal(checks, "container_resolution_preview.usage_bindings", actual_uses, expected_uses)
    return checks


def sealed_acceptance_plan_state(plan: dict[str, Any]) -> tuple[str, bool]:
    """从 execution plan 指向的 artifact 推导 strict decision 与可重算状态。"""
    strict_path = Path(str(plan.get("strict_acceptance", ""))).resolve()
    if not strict_path.is_file():
        return "missing", False
    try:
        artifact_hash = sha256_file(strict_path)
        strict_artifact = load_json(strict_path)
        decision_raw = strict_artifact.get("decision")
        decision = decision_raw if decision_raw in {"accepted", "rejected"} else "malformed"
        verify_acceptance_artifact(
            artifact=strict_artifact,
            baseline_result_path=Path(str(plan.get("strict_baseline_result", MECHANISM_BASELINE_RESULT))),
            candidate_result_path=Path(str(plan.get("strict_candidate_result", MECHANISM_CANDIDATE_RESULT))),
            artifact_path=strict_path,
            artifact_sha256=artifact_hash,
            splits=("train", "heldout"),
            expected_repeats=2,
        )
        return decision, True
    except (OSError, TypeError, ValueError):
        return decision if "decision" in locals() else "malformed", False


def derive_acceptance_readiness(plan: dict[str, Any]) -> dict[str, Any]:
    """从 strict acceptance 文件和绑定结果独立重算 readiness 状态。"""
    strict_path = Path(str(plan.get("strict_acceptance", ""))).resolve()
    state = {
        "strict_acceptance_artifact_present": strict_path.is_file(),
        "strict_acceptance_artifact_malformed": False,
        "strict_acceptance_boolean_consistent": False,
        "strict_acceptance_source_hashes_stable": False,
        "strict_acceptance_result_binding": False,
        "strict_acceptance_recomputed": False,
    }
    if not strict_path.is_file():
        return state
    try:
        artifact = load_json(strict_path)
    except (OSError, TypeError, ValueError):
        state["strict_acceptance_artifact_malformed"] = True
        return state
    if not isinstance(artifact, dict):
        state["strict_acceptance_artifact_malformed"] = True
        return state
    accepted = artifact.get("accepted")
    decision = artifact.get("decision")
    state["strict_acceptance_boolean_consistent"] = (
        isinstance(accepted, bool) and decision in {"accepted", "rejected"} and bool(accepted) == (decision == "accepted")
    )
    source_stable = artifact.get("source_hashes_stable")
    state["strict_acceptance_source_hashes_stable"] = isinstance(source_stable, bool) and source_stable
    baseline_path = Path(str(plan.get("strict_baseline_result", MECHANISM_BASELINE_RESULT))).resolve()
    candidate_path = Path(str(plan.get("strict_candidate_result", MECHANISM_CANDIDATE_RESULT))).resolve()
    state["strict_acceptance_result_binding"] = (
        artifact.get("baseline_result") == str(baseline_path)
        and artifact.get("candidate_result") == str(candidate_path)
        and artifact.get("baseline_result_sha256") == (sha256_file(baseline_path) if baseline_path.is_file() else None)
        and artifact.get("candidate_result_sha256") == (sha256_file(candidate_path) if candidate_path.is_file() else None)
    )
    try:
        verify_acceptance_artifact(
            artifact=artifact,
            baseline_result_path=baseline_path,
            candidate_result_path=candidate_path,
            artifact_path=strict_path,
            artifact_sha256=sha256_file(strict_path),
            splits=("train", "heldout"),
            expected_repeats=2,
        )
        state["strict_acceptance_recomputed"] = True
    except (OSError, TypeError, ValueError):
        state["strict_acceptance_recomputed"] = False
    return state


def derive_environment_readiness(plan: dict[str, Any]) -> dict[str, Any]:
    """从 execution_environment 与 dependency lock 重算可验证的环境 readiness。"""
    environment = plan.get("execution_environment")
    dependency_lock = plan.get("dependency_lock")
    if not isinstance(environment, dict):
        return {"source_snapshot_ready": False, "docker_available": False, "harbor_available": False, "dependency_lock_available": False}
    dirty = environment.get("git_dirty")
    override = environment.get("dirty_worktree_override")
    source_ready = isinstance(dirty, bool) and isinstance(override, bool) and ((not dirty) or override)
    harbor_path = Path(str(environment.get("harbor_executable", "")))
    harbor_ready = harbor_path.is_file() and bool(str(environment.get("harbor_version", "")).strip())
    docker_ready = environment.get("docker_available") is True and bool(str(environment.get("docker_client_version", "")).strip()) and bool(str(environment.get("docker_server_version", "")).strip())
    dependency_ready = False
    if isinstance(dependency_lock, dict):
        try:
            validate_environment_lock(dependency_lock)
            dependency_ready = True
        except ValueError:
            dependency_ready = False
    return {
        "source_snapshot_ready": source_ready,
        "harbor_available": harbor_ready,
        "docker_available": docker_ready,
        "dependency_lock_available": dependency_ready,
    }


def environment_identity_checks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """核对冻结环境快照的 Git、模型和时间身份是否仍对应当前工作区。"""
    checks: list[dict[str, Any]] = []
    environment = plan.get("execution_environment") if isinstance(plan, dict) else None
    if not isinstance(environment, dict):
        assert_equal(checks, "sealed_plan.execution_environment_shape", False, True)
        return checks
    try:
        current_head = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        current_head = None
    try:
        current_dirty = bool(
            subprocess.run(
                ["git", "-C", str(ROOT), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        current_dirty = None
    assert_equal(checks, "sealed_plan.execution_environment.git_head_current", environment.get("git_head"), current_head)
    assert_equal(checks, "sealed_plan.execution_environment.git_dirty_current", environment.get("git_dirty"), current_dirty)
    assert_equal(checks, "sealed_plan.execution_environment.model_matches_plan", environment.get("model"), plan.get("model"))
    captured = environment.get("captured_at")
    try:
        captured_dt = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
        captured_valid = captured_dt.tzinfo is not None and captured_dt <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        captured_valid = False
    assert_equal(checks, "sealed_plan.execution_environment.captured_at_valid_and_not_future", captured_valid, True)
    dependency = plan.get("dependency_lock") if isinstance(plan, dict) else None
    if isinstance(dependency, dict):
        dependency_time = dependency.get("captured_at")
        try:
            dependency_dt = datetime.fromisoformat(str(dependency_time).replace("Z", "+00:00"))
            captured_dt = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
            capture_window_ok = abs((captured_dt - dependency_dt).total_seconds()) <= 300
        except (TypeError, ValueError):
            capture_window_ok = False
        assert_equal(checks, "sealed_plan.environment_lock.capture_time_window_le_300s", capture_window_ok, True)
        harbor_cli = dependency.get("harbor_cli", {})
        harbor_python = dependency.get("harbor_python", {})
        project_python = dependency.get("project_python", {})
        assert_equal(checks, "sealed_plan.environment_lock.harbor_cli_path_matches", harbor_cli.get("executable"), environment.get("harbor_executable"))
        assert_equal(checks, "sealed_plan.environment_lock.harbor_version_matches", harbor_cli.get("version"), environment.get("harbor_version"))
        assert_equal(checks, "sealed_plan.environment_lock.harbor_python_path_matches", harbor_python.get("executable"), environment.get("harbor_executable", "").replace("\\Scripts\\harbor.exe", "\\Scripts\\python.exe"))
        assert_equal(checks, "sealed_plan.environment_lock.project_python_path_matches", project_python.get("executable"), environment.get("python_executable"))
        assert_equal(checks, "sealed_plan.environment_lock.project_python_version_matches", project_python.get("version"), environment.get("python_version"))
        binary_records = (
            ("project_python", project_python, environment.get("python_executable")),
            ("harbor_python", harbor_python, environment.get("harbor_executable", "").replace("\\Scripts\\harbor.exe", "\\Scripts\\python.exe")),
            ("harbor_cli", harbor_cli, environment.get("harbor_executable")),
        )
        for label, record, expected_path in binary_records:
            path = Path(str(expected_path)) if isinstance(expected_path, str) and expected_path else None
            actual_hash = sha256_file(path) if path is not None and path.is_file() else None
            assert_equal(checks, f"sealed_plan.environment_lock.{label}_sha256_matches_disk", record.get("executable_sha256"), actual_hash)
            if path is not None and path.is_file():
                if label == "project_python" or label == "harbor_python":
                    version_argv = [str(path), "--version"]
                else:
                    version_argv = [str(path), "--version"]
                try:
                    current_version = subprocess.run(version_argv, check=True, capture_output=True, text=True).stdout.strip()
                except (OSError, subprocess.CalledProcessError):
                    current_version = None
                assert_equal(checks, f"sealed_plan.environment_lock.{label}_version_matches_disk", record.get("version"), current_version)
    return checks


def expected_readiness_blockers(
    *, plan: dict[str, Any], strict_decision: str
) -> list[str]:
    """按 launcher 的固定顺序重算 execution plan 的阻断代码。"""
    readiness = plan.get("readiness", {})
    blockers: list[str] = []
    if strict_decision == "missing":
        blockers.append("strict_decision_missing")
    elif strict_decision != "accepted":
        blockers.append("strict_decision_not_accepted")
    if readiness.get("strict_acceptance_artifact_present") is not True:
        blockers.append("strict_acceptance_artifact_missing")
    elif readiness.get("strict_acceptance_artifact_malformed") is True:
        blockers.append("strict_acceptance_artifact_malformed")
    else:
        if readiness.get("strict_acceptance_boolean_consistent") is not True:
            blockers.append("strict_acceptance_boolean_inconsistent")
        if readiness.get("strict_acceptance_source_hashes_stable") is not True:
            blockers.append("strict_acceptance_source_hashes_unstable")
        if readiness.get("strict_acceptance_result_binding") is not True:
            blockers.append("strict_acceptance_result_binding_failed")
    if readiness.get("strict_acceptance_recomputed") is not True:
        blockers.append("strict_acceptance_not_recomputed")
    if readiness.get("source_snapshot_ready") is not True:
        blockers.append("source_snapshot_not_ready")
    if readiness.get("harbor_available") is not True:
        blockers.append("harbor_unavailable")
    if readiness.get("docker_available") is not True:
        blockers.append("docker_unavailable")
    if readiness.get("api_key_available") is not True:
        blockers.append("api_key_unavailable")
    if readiness.get("dependency_lock_available") is not True:
        blockers.append("dependency_lock_unavailable")
    return blockers


def sealed_execution_plan_checks(
    *, plan: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """校验 Sealed21 dry-run execution plan 与冻结输入和当前门禁状态一致。"""
    checks: list[dict[str, Any]] = []
    actual = plan if plan is not None else load_json(SEALED_PLAN_PATH)
    manifest = load_json(SPLIT_SEALED_MANIFEST)
    preregistration = load_json(SEALED_PREREGISTRATION)
    expected_case_count = len(manifest.get("tasks", []))
    sealed_config = tomllib.loads(SEALED_CONFIG.read_text(encoding="utf-8"))
    eval_config = sealed_config.get("eval", {})
    config_cases = sealed_config.get("cases", [])
    config_tasks = len(config_cases)
    config_repeats = int(eval_config.get("repeats", 0))
    config_retries = int(eval_config.get("infrastructure_retries", 0))
    config_concurrency = int(eval_config.get("case_concurrency", 0))
    config_timeout_s = float(eval_config.get("timeout_s", 0))
    config_budget_hours = (
        config_tasks
        * config_repeats
        * 2
        * (config_retries + 1)
        * config_timeout_s
        / config_concurrency
        / 3600
    )
    assert_equal(checks, "sealed_plan.format", actual.get("format"), "self_harness.sealed_execution_plan.v4")
    assert_equal(checks, "sealed_plan.model", actual.get("model"), "openai:qwen3.7-plus")
    assert_equal(checks, "sealed_plan.candidate_id", actual.get("candidate_id"), "anti_workaround_execution")
    # 执行计划必须携带冻结统计合同，避免启动器、分析器和论文审计各自读取不同定义。
    for section in ("primary_endpoint", "statistical_design", "missingness_policy", "multiplicity"):
        assert_equal(
            checks,
            f"sealed_plan.{section}_matches_preregistration",
            actual.get(section),
            preregistration.get(section),
        )
    candidate_queue_path = ROOT / "runs" / "clean64-qwen-self-harness" / "candidate_queue.json"
    candidate_dir = ROOT / "runs" / "clean64-qwen-self-harness" / "branches" / "baseline" / "candidates" / "anti_workaround_execution"
    candidate_manifest_path = candidate_dir / "manifest.json"
    candidate_queue = load_json(candidate_queue_path)
    candidate_matches = [
        item for item in candidate_queue.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id") == "anti_workaround_execution"
    ]
    assert_equal(checks, "sealed_plan.candidate_queue_unique_match", len(candidate_matches), 1)
    queue_item = candidate_matches[0] if len(candidate_matches) == 1 else {}
    assert_equal(checks, "sealed_plan.candidate_dir_matches_queue", actual.get("candidate_dir"), str(candidate_dir.resolve()))
    assert_equal(checks, "sealed_plan.candidate_dir_matches_canonical", actual.get("candidate_dir"), str(candidate_dir.resolve()))
    assert_equal(checks, "sealed_plan.eval_result_matches_queue", actual.get("strict_candidate_result"), queue_item.get("eval_result"))
    if candidate_manifest_path.is_file():
        candidate_manifest = load_json(candidate_manifest_path)
        assert_equal(checks, "sealed_plan.candidate_manifest_id_matches", candidate_manifest.get("candidate_id"), actual.get("candidate_id"))
        surface_relative = candidate_manifest.get("surface_files", {}).get("baseline") if isinstance(candidate_manifest.get("surface_files"), dict) else None
        expected_surface = (candidate_dir / str(surface_relative)).resolve() if isinstance(surface_relative, str) else None
        assert_equal(checks, "sealed_plan.candidate_surface_path_matches_manifest", str(SEALED_CANDIDATE_SURFACE.resolve()), str(expected_surface) if expected_surface else None)
    else:
        assert_equal(checks, "sealed_plan.candidate_manifest_exists", False, True)
    freeze_path = ROOT / "runs" / "sealed21-qwen-freeze.json"
    if freeze_path.is_file():
        freeze = load_json(freeze_path)
        assert_equal(checks, "sealed_plan.freeze_candidate_id_matches", freeze.get("candidate_id"), actual.get("candidate_id"))
        assert_equal(checks, "sealed_plan.freeze_candidate_dir_matches", freeze.get("candidate_dir"), actual.get("candidate_dir"))
        assert_equal(checks, "sealed_plan.freeze_candidate_surface_matches", freeze.get("candidate_surface"), str(SEALED_CANDIDATE_SURFACE.resolve()))
        for section in ("primary_endpoint", "statistical_design", "missingness_policy", "multiplicity"):
            assert_equal(
                checks,
                f"sealed_plan.freeze_{section}_matches_plan",
                freeze.get(section),
                actual.get(section),
            )
    else:
        assert_equal(checks, "sealed_plan.freeze_absent_before_execution", True, True)
    assert_equal(checks, "sealed_plan.tasks_matches_manifest", actual.get("tasks"), expected_case_count)
    assert_equal(checks, "sealed_plan.config_case_count_matches_manifest", config_tasks, expected_case_count)
    assert_equal(checks, "sealed_plan.config_repeats", config_repeats, 2)
    assert_equal(checks, "sealed_plan.config_infrastructure_retries", config_retries, 2)
    assert_equal(checks, "sealed_plan.config_case_concurrency", config_concurrency, 4)
    assert_equal(checks, "sealed_plan.config_timeout_seconds", config_timeout_s, 3000.0)
    assert_equal(checks, "sealed_plan.repeats", actual.get("repeats"), 2)
    assert_equal(checks, "sealed_plan.infrastructure_retries", actual.get("infrastructure_retries"), config_retries)
    assert_equal(checks, "sealed_plan.case_concurrency", actual.get("case_concurrency"), config_concurrency)
    assert_equal(checks, "sealed_plan.timeout_seconds", actual.get("timeout_seconds"), config_timeout_s)
    assert_equal(checks, "sealed_plan.evaluation_cells", actual.get("evaluation_cells"), expected_case_count * 2 * 2)
    assert_equal(checks, "sealed_plan.timeout_upper_hours_from_config", actual.get("configured_outer_timeout_upper_hours"), config_budget_hours)
    assert_equal(checks, "sealed_plan.timeout_upper_hours", actual.get("configured_outer_timeout_upper_hours"), 52.5)
    expected_strict_decision, expected_strict_recomputed = sealed_acceptance_plan_state(actual)
    assert_equal(checks, "sealed_plan.strict_decision_matches_artifact", actual.get("strict_decision"), expected_strict_decision)
    readiness = actual.get("readiness", {})
    derived_readiness = derive_acceptance_readiness(actual)
    for key, value in derived_readiness.items():
        assert_equal(checks, f"sealed_plan.{key}_matches_disk_evidence", readiness.get(key), value)
        if key in actual:
            assert_equal(checks, f"sealed_plan.top_level_{key}_matches_disk_evidence", actual.get(key), value)
    derived_environment = derive_environment_readiness(actual)
    for key, value in derived_environment.items():
        assert_equal(checks, f"sealed_plan.{key}_matches_environment_evidence", readiness.get(key), value)
    checks.extend(environment_identity_checks(actual))
    assert_equal(checks, "sealed_plan.ready_to_execute_type", isinstance(actual.get("ready_to_execute"), bool), True)
    assert_equal(checks, "sealed_plan.strict_acceptance_recomputed_type", isinstance(actual.get("strict_acceptance_recomputed"), bool), True)
    assert_equal(checks, "sealed_plan.strict_acceptance_artifact_present_type", isinstance(actual.get("strict_acceptance_artifact_present"), bool), True)
    assert_equal(checks, "sealed_plan.strict_acceptance_artifact_malformed_type", isinstance(actual.get("strict_acceptance_artifact_malformed"), bool), True)
    assert_equal(checks, "sealed_plan.readiness.strict_acceptance_recomputed_type", isinstance(readiness.get("strict_acceptance_recomputed"), bool), True)
    assert_equal(checks, "sealed_plan.readiness.strict_acceptance_artifact_present_type", isinstance(readiness.get("strict_acceptance_artifact_present"), bool), True)
    assert_equal(checks, "sealed_plan.readiness.strict_acceptance_artifact_malformed_type", isinstance(readiness.get("strict_acceptance_artifact_malformed"), bool), True)
    assert_equal(checks, "sealed_plan.strict_acceptance_recomputed_matches_readiness", actual.get("strict_acceptance_recomputed"), readiness.get("strict_acceptance_recomputed"))
    assert_equal(checks, "sealed_plan.strict_acceptance_artifact_present_matches_readiness", actual.get("strict_acceptance_artifact_present"), readiness.get("strict_acceptance_artifact_present"))
    assert_equal(checks, "sealed_plan.strict_acceptance_artifact_malformed_matches_readiness", actual.get("strict_acceptance_artifact_malformed"), readiness.get("strict_acceptance_artifact_malformed"))
    assert_equal(checks, "sealed_plan.strict_acceptance_recomputed_matches_source", actual.get("strict_acceptance_recomputed"), expected_strict_recomputed)
    expected_strict_ready = (
        expected_strict_decision == "accepted"
        and readiness.get("strict_acceptance_boolean_consistent") is True
        and readiness.get("strict_acceptance_source_hashes_stable") is True
        and readiness.get("strict_acceptance_result_binding") is True
        and expected_strict_recomputed is True
    )
    assert_equal(checks, "sealed_plan.readiness.strict_acceptance_matches_components", readiness.get("strict_acceptance"), expected_strict_ready)
    expected_blockers = expected_readiness_blockers(
        plan=actual,
        strict_decision=expected_strict_decision,
    )
    assert_equal(checks, "sealed_plan.readiness_blockers", actual.get("readiness_blockers"), expected_blockers)
    assert_equal(checks, "sealed_plan.readiness.readiness_blockers", readiness.get("readiness_blockers"), expected_blockers)
    readiness_keys = (
        "strict_acceptance",
        "strict_acceptance_recomputed",
        "source_snapshot_ready",
        "harbor_available",
        "docker_available",
        "api_key_available",
        "dependency_lock_available",
    )
    expected_ready = all(readiness.get(key) is True for key in readiness_keys)
    assert_equal(checks, "sealed_plan.ready_to_execute_matches_readiness", actual.get("ready_to_execute"), expected_ready)
    assert_equal(checks, "sealed_plan.ready_requires_strict_acceptance", actual.get("ready_to_execute") is True, actual.get("strict_acceptance") is True)

    canonical_paths = {
        "sealed_manifest": SPLIT_SEALED_MANIFEST,
        "preregistration": SEALED_PREREGISTRATION,
        "sealed_config": SEALED_CONFIG,
        "strict_acceptance": ROOT
        / "runs"
        / "clean64-qwen-self-harness"
        / "branches"
        / "baseline"
        / "candidates"
        / "anti_workaround_execution"
        / "acceptance.strict.json",
        "strict_baseline_result": MECHANISM_BASELINE_RESULT,
        "strict_candidate_result": MECHANISM_CANDIDATE_RESULT,
        "candidate_dir": candidate_dir,
        "container_resolution_output": ROOT / "runs" / "sealed21-qwen-container-resolution.json",
    }
    for field, path in canonical_paths.items():
        assert_equal(checks, f"sealed_plan.{field}_path", actual.get(field), str(path.resolve()))
    hash_fields = {
        "sealed_manifest_sha256": SPLIT_SEALED_MANIFEST,
        "preregistration_sha256": SEALED_PREREGISTRATION,
        "sealed_config_sha256": SEALED_CONFIG,
        "strict_acceptance_sha256": Path(str(actual.get("strict_acceptance", ""))).resolve(),
        "baseline_surface_sha256": SEALED_BASELINE_SURFACE,
        "candidate_surface_sha256": SEALED_CANDIDATE_SURFACE,
        "strict_baseline_result_sha256": MECHANISM_BASELINE_RESULT,
        "strict_candidate_result_sha256": MECHANISM_CANDIDATE_RESULT,
    }
    for field, path in hash_fields.items():
        digest = sha256_file(path) if path.is_file() else None
        assert_equal(checks, f"sealed_plan.{field}_matches_disk", actual.get(field), digest)
    lock_value = SEALED_MANIFEST_LOCK.read_text(encoding="ascii").strip()
    assert_equal(checks, "sealed_plan.manifest_hash_matches_lock", actual.get("sealed_manifest_sha256"), lock_value)
    dependency_lock = actual.get("dependency_lock")
    dependency_lock_valid = False
    if isinstance(dependency_lock, dict):
        try:
            validate_environment_lock(dependency_lock)
            dependency_lock_valid = True
        except ValueError:
            dependency_lock_valid = False
    assert_equal(checks, "sealed_plan.dependency_lock_valid", dependency_lock_valid, True)
    execution_bundle_hash = (
        actual.get("execution_code", {}).get("bundle_sha256")
        if isinstance(actual.get("execution_code"), dict)
        else None
    )
    dependency_bundle_hash = (
        actual.get("dependency_lock", {}).get("bundle_sha256")
        if isinstance(actual.get("dependency_lock"), dict)
        else None
    )
    identity_prefix = (
        f"sealed21:{actual.get('sealed_manifest_sha256')}:{actual.get('preregistration_sha256')}:"
        f"{actual.get('sealed_config_sha256')}:{execution_bundle_hash}:{dependency_bundle_hash}:"
    )
    expected_baseline_identity = f"{identity_prefix}baseline:{actual.get('baseline_surface_sha256')}"
    expected_candidate_identity = f"{identity_prefix}candidate:{actual.get('candidate_surface_sha256')}"
    assert_equal(checks, "sealed_plan.baseline_run_identity_recomputed", actual.get("baseline_run_identity"), expected_baseline_identity)
    assert_equal(checks, "sealed_plan.candidate_run_identity_recomputed", actual.get("candidate_run_identity"), expected_candidate_identity)

    execution_order = (
        "backend_bridge",
        "capture_container_resolution",
        "capture_environment_lock",
        "harbor_wrapper",
        "run_harbor_eval",
    )
    analysis_order = (
        "acceptance_gate",
        "analyze_experiments",
        "analyze_sealed",
        "build_sealed_split",
        "result_validity",
    )
    for section, order in (("execution_code", execution_order), ("analysis_code", analysis_order)):
        bundle = actual.get(section, {})
        files = bundle.get("files", {}) if isinstance(bundle, dict) else {}
        hashes: dict[str, str] = {}
        for key in order:
            item = files.get(key, {})
            path = Path(item.get("path")) if isinstance(item, dict) and item.get("path") else None
            digest = sha256_file(path) if path is not None and path.is_file() else None
            assert_equal(checks, f"sealed_plan.{section}.{key}.sha256_matches_disk", item.get("sha256") if isinstance(item, dict) else None, digest)
            if digest is not None:
                hashes[key] = digest
        canonical = "".join(f"{key}={hashes.get(key, '')}\n" for key in order).encode("utf-8")
        assert_equal(checks, f"sealed_plan.{section}.bundle_sha256", bundle.get("bundle_sha256") if isinstance(bundle, dict) else None, hashlib.sha256(canonical).hexdigest())
    return checks


def sealed_protocol_checks(
    *,
    plan: dict[str, Any] | None = None,
    preregistration: dict[str, Any] | None = None,
    protocol: str | None = None,
) -> list[dict[str, Any]]:
    """交叉核对 Sealed21 协议、预注册和机器执行计划的关键设计参数。"""
    checks: list[dict[str, Any]] = []
    actual_plan = plan if plan is not None else load_json(SEALED_PLAN_PATH)
    prereg = preregistration if preregistration is not None else load_json(SEALED_PREREGISTRATION)
    protocol_text = protocol if protocol is not None else SEALED_PROTOCOL.read_text(encoding="utf-8")
    split = prereg.get("sealed_split", {}) if isinstance(prereg, dict) else {}
    missingness = prereg.get("missingness_policy", {}) if isinstance(prereg, dict) else {}

    assert_equal(checks, "sealed_protocol.prereg_format", prereg.get("format"), "self_harness.ei_confirmation_preregistration.v1")
    assert_equal(checks, "sealed_protocol.model_matches_plan", prereg.get("primary_model"), actual_plan.get("model"))
    assert_equal(checks, "sealed_protocol.candidate_matches_plan", prereg.get("primary_candidate"), actual_plan.get("candidate_id"))
    assert_equal(checks, "sealed_protocol.tasks_matches_plan", split.get("tasks"), actual_plan.get("tasks"))
    assert_equal(checks, "sealed_protocol.attempts_per_harness", split.get("attempts_per_harness"), actual_plan.get("tasks", 0) * actual_plan.get("repeats", 0))
    # preregistration 的 attempts_per_harness 已经是单侧总 attempts（21×2=42），再乘两侧角色才得到 84 cells。
    assert_equal(checks, "sealed_protocol.evaluation_cells", split.get("attempts_per_harness", 0) * 2, actual_plan.get("evaluation_cells"))
    assert_equal(checks, "sealed_protocol.outcome_blind", split.get("outcome_blind"), True)
    endpoint = prereg.get("primary_endpoint", {}) if isinstance(prereg, dict) else {}
    assert_equal(checks, "sealed_protocol.primary_endpoint.name", endpoint.get("name"), "paired Sealed21 pass-rate difference")
    assert_equal(checks, "sealed_protocol.primary_endpoint.contrast", endpoint.get("contrast"), "frozen candidate minus baseline")
    assert_equal(checks, "sealed_protocol.primary_endpoint.unit", endpoint.get("unit"), "Terminal-Bench task")
    assert_equal(checks, "sealed_protocol.primary_endpoint.metric", endpoint.get("metric"), "verifier pass fraction over two attempts per task")
    assert_equal(checks, "sealed_protocol.primary_endpoint.confidence_interval", endpoint.get("confidence_interval"), "task-clustered nonparametric bootstrap, 20000 resamples")
    assert_equal(checks, "sealed_protocol.primary_endpoint.hypothesis_test", endpoint.get("hypothesis_test"), "two-sided task-level paired sign permutation test")
    assert_equal(checks, "sealed_protocol.primary_endpoint.alpha", endpoint.get("alpha"), 0.05)
    assert_equal(checks, "sealed_protocol.primary_endpoint.success_rule", endpoint.get("success_rule"), "delta > 0, 95% CI lower bound > 0, and p < 0.05")
    assert_equal(checks, "sealed_protocol.manifest_path_matches_plan", split.get("manifest"), str(SPLIT_SEALED_MANIFEST.relative_to(ROOT)).replace("\\", "/"))
    assert_equal(checks, "sealed_protocol.config_path_matches_plan", split.get("config"), str(SEALED_CONFIG.relative_to(ROOT)).replace("\\", "/"))
    assert_equal(checks, "sealed_protocol.manifest_hash_matches_plan", split.get("manifest_sha256"), actual_plan.get("sealed_manifest_sha256"))
    assert_equal(checks, "sealed_protocol.reruns", missingness.get("reruns"), 2)
    assert_equal(checks, "sealed_protocol.missingness.classification", missingness.get("classification"), "infrastructure-invalid is neither pass nor behavioral fail")
    assert_equal(checks, "sealed_protocol.no_task_replacement", missingness.get("replacement_tasks"), False)
    assert_equal(checks, "sealed_protocol.no_imputation", missingness.get("imputation"), False)
    assert_equal(checks, "sealed_protocol.missingness.completion_rule", missingness.get("completion_rule"), "both harnesses must have zero unresolved invalid cells")
    assert_equal(checks, "sealed_protocol.missingness.failure_rule", missingness.get("failure_rule"), "if invalid cells remain after allowed reruns, report Sealed21 as incomplete")
    multiplicity = prereg.get("multiplicity", {}) if isinstance(prereg, dict) else {}
    assert_equal(checks, "sealed_protocol.multiplicity.primary_hypotheses", multiplicity.get("primary_hypotheses"), 1)
    assert_equal(checks, "sealed_protocol.multiplicity.adjustment", multiplicity.get("adjustment"), "none")
    assert_equal(checks, "sealed_protocol.multiplicity.rationale", multiplicity.get("rationale"), "one pre-registered model, split, contrast, and primary endpoint")
    assert_equal(checks, "sealed_protocol.protocol_declares_84_cells", "21 个任务 × 2 attempts，共 84 个评测单元" in protocol_text, True)
    assert_equal(checks, "sealed_protocol.protocol_declares_timeout", "52.5 wall-clock hours" in protocol_text, True)
    assert_equal(checks, "sealed_protocol.protocol_declares_precondition_gate", "任一条件不满足时，sealed 启动器必须退出且不得调用模型" in protocol_text, True)
    precondition_section = protocol_text.split("## 执行前置条件", 1)[1].split("## 执行约束", 1)[0] if "## 执行前置条件" in protocol_text and "## 执行约束" in protocol_text else ""
    assert_equal(checks, "sealed_protocol.invalid_preconditions_explicitly_unresolved", "当前尚未解决" in precondition_section, True)
    assert_equal(checks, "sealed_protocol.invalid_preconditions_not_claimed_fixed", "已解决" not in precondition_section, True)
    return checks


def sealed_statistical_design_checks(
    *, preregistration: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """核对 Sealed21 统计设计与共享分析构造器一致。"""
    checks: list[dict[str, Any]] = []
    prereg = preregistration if preregistration is not None else load_json(SEALED_PREREGISTRATION)
    from paper import analyze_experiments as analysis
    from paper import analyze_sealed

    split = prereg.get("sealed_split", {}) if isinstance(prereg, dict) else {}
    expected = analyze_sealed.sealed_statistical_design_metadata(int(split.get("tasks", 0)))
    assert_equal(checks, "sealed_statistics.preregistration.statistical_design", prereg.get("statistical_design"), expected)
    assert_equal(checks, "sealed_statistics.shared_design.tasks", expected["split_task_counts"].get("sealed"), 21)
    assert_equal(checks, "sealed_statistics.shared_design.attempts_per_task", expected["attempts_per_task"], 2)
    assert_equal(checks, "sealed_statistics.shared_design.repeat_ids", expected["repeat_ids"], [1, 2])
    assert_equal(checks, "sealed_statistics.shared_design.roles", expected.get("roles"), ["baseline", "candidate"])
    clean = analysis.statistical_design_metadata()
    assert_equal(checks, "sealed_statistics.shared_design.bootstrap.resamples", expected["bootstrap"]["resamples"], clean["bootstrap"]["resamples"])
    assert_equal(checks, "sealed_statistics.shared_design.bootstrap.seed", expected["bootstrap"]["seed"], clean["bootstrap"]["seed"])
    assert_equal(checks, "sealed_statistics.shared_design.bootstrap.percentile_method", expected["bootstrap"]["percentile_method"], clean["bootstrap"]["percentile_method"])
    assert_equal(checks, "sealed_statistics.shared_design.permutation", expected["permutation"], clean["permutation"])
    endpoint = prereg.get("primary_endpoint", {}) if isinstance(prereg, dict) else {}
    assert_equal(checks, "sealed_statistics.endpoint.alpha", endpoint.get("alpha"), 0.05)
    assert_equal(checks, "sealed_statistics.endpoint.success_rule", endpoint.get("success_rule"), "delta > 0, 95% CI lower bound > 0, and p < 0.05")
    return checks


def sealed_statistics_status_checks(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """审计 Sealed 统计产物的状态与 invalid/推断字段闭环。"""
    checks: list[dict[str, Any]] = []
    from paper import analyze_sealed

    actual = payload if payload is not None else load_json(ROOT / "paper" / "generated" / "sealed21" / "sealed_statistics.json")
    try:
        analyze_sealed.validate_sealed_statistics_payload(actual)
        valid = True
        error = None
    except (TypeError, ValueError) as exc:
        valid = False
        error = str(exc)
    assert_equal(checks, "sealed_statistics.status_payload_valid", valid, True)
    if error is not None:
        checks.append({"name": "sealed_statistics.status_payload_error", "ok": False, "actual": error, "expected": None})
    report_path = ROOT / "paper" / "generated" / "sealed21" / "SEALED_REPORT.md"
    if report_path.is_file() and isinstance(actual, dict):
        report = report_path.read_text(encoding="utf-8")
        status = actual.get("analysis_status")
        assert_equal(checks, "sealed_report.analysis_status_marker", f"analysis_status=`{status}`" in report, True)
        alpha = actual.get("primary_endpoint_alpha")
        rule = actual.get("primary_success_rule")
        assert_equal(checks, "sealed_report.alpha_marker", f"primary_endpoint_alpha={alpha:g}" in report if isinstance(alpha, (int, float)) else False, True)
        assert_equal(checks, "sealed_report.success_rule_marker", f"primary_success_rule=`{rule}`" in report, True)
        if status == "incomplete":
            assert_equal(checks, "sealed_report.incomplete_uses_NA", "NA" in report, True)
            assert_equal(checks, "sealed_report.blocked_reason_marker", "inference_blocked_reason=" in report, True)
            assert_equal(checks, "sealed_report.no_literal_None", "None" not in report, True)
    preregistration = load_json(SEALED_PREREGISTRATION)
    endpoint = preregistration.get("primary_endpoint", {}) if isinstance(preregistration, dict) else {}
    assert_equal(
        checks,
        "sealed_statistics.primary_endpoint_alpha_matches_preregistration",
        actual.get("primary_endpoint_alpha") if isinstance(actual, dict) else None,
        endpoint.get("alpha"),
    )
    assert_equal(
        checks,
        "sealed_statistics.primary_success_rule_matches_preregistration",
        actual.get("primary_success_rule") if isinstance(actual, dict) else None,
        endpoint.get("success_rule"),
    )
    for section in ("missingness_policy", "multiplicity"):
        assert_equal(
            checks,
            f"sealed_statistics.{section}_matches_preregistration",
            actual.get(section) if isinstance(actual, dict) else None,
            preregistration.get(section),
        )
    return checks


def sealed_readiness_narrative_checks(
    *, plan: dict[str, Any], english: str, chinese: str
) -> list[dict[str, Any]]:
    """确保中英文实验章节报告当前 execution plan 的 readiness blockers。"""
    checks: list[dict[str, Any]] = []
    blockers = plan.get("readiness_blockers", [])
    if not isinstance(blockers, list) or any(not isinstance(item, str) for item in blockers):
        assert_equal(checks, "sealed_readiness.blockers_shape", False, True)
        return checks
    for blocker in blockers:
        assert_equal(checks, f"sealed_readiness.en.{blocker}", blocker in english, True)
        assert_equal(checks, f"sealed_readiness.zh.{blocker}", blocker in chinese, True)
    return checks


def statistical_unit_checks(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """验证 Clean64 汇总仍遵守 task × two-attempt 的统计单位边界。"""
    checks: list[dict[str, Any]] = []
    expected = {"train": (43, 86), "heldout": (21, 42)}
    rows = stats.get("summary", [])
    assert_equal(checks, "statistics.summary_row_count", len(rows), 4)
    for index, row in enumerate(rows):
        prefix = f"statistics.summary[{index}]"
        split = row.get("split")
        task_count, attempt_count = expected.get(split, (None, None))
        assert_equal(checks, f"{prefix}.split_known", split in expected, True)
        if task_count is None:
            continue
        assert_equal(checks, f"{prefix}.tasks", row.get("tasks"), task_count)
        assert_equal(checks, f"{prefix}.attempts", row.get("attempts"), attempt_count)
        assert_equal(checks, f"{prefix}.attempts_equal_tasks_x_two", row.get("attempts"), row.get("tasks", 0) * 2)
        for side in ("baseline", "final"):
            passed = row.get(f"{side}_passed")
            invalid = row.get(f"{side}_invalid")
            assert_equal(
                checks,
                f"{prefix}.{side}_counts_bounded",
                isinstance(passed, int)
                and not isinstance(passed, bool)
                and isinstance(invalid, int)
                and not isinstance(invalid, bool)
                and 0 <= invalid <= attempt_count
                and 0 <= passed <= attempt_count - invalid,
                True,
            )
            assert_close(checks, f"{prefix}.{side}_rate_formula", row.get(f"{side}_rate"), passed / attempt_count if isinstance(passed, int) else float("nan"))
        assert_close(
            checks,
            f"{prefix}.delta_formula",
            row.get("delta"),
            (row.get("final_passed") - row.get("baseline_passed")) / attempt_count
            if isinstance(row.get("final_passed"), int) and isinstance(row.get("baseline_passed"), int)
            else float("nan"),
        )
        common_n = row.get("common_valid_attempts")
        common_before = row.get("common_valid_baseline_passed")
        common_after = row.get("common_valid_final_passed")
        assert_equal(
            checks,
            f"{prefix}.common_valid_counts_bounded",
            isinstance(common_n, int)
            and not isinstance(common_n, bool)
            and isinstance(common_before, int)
            and not isinstance(common_before, bool)
            and isinstance(common_after, int)
            and not isinstance(common_after, bool)
            and 0 <= common_n <= attempt_count
            and 0 <= common_before <= common_n
            and 0 <= common_after <= common_n,
            True,
        )
        assert_close(
            checks,
            f"{prefix}.common_valid_baseline_rate_formula",
            row.get("common_valid_baseline_rate"),
            common_before / common_n if common_n else None,
        ) if common_n else assert_equal(checks, f"{prefix}.common_valid_baseline_rate_null_when_empty", row.get("common_valid_baseline_rate"), None)
        assert_close(
            checks,
            f"{prefix}.common_valid_final_rate_formula",
            row.get("common_valid_final_rate"),
            common_after / common_n if common_n else None,
        ) if common_n else assert_equal(checks, f"{prefix}.common_valid_final_rate_null_when_empty", row.get("common_valid_final_rate"), None)
        if common_n:
            assert_close(
                checks,
                f"{prefix}.common_valid_delta_formula",
                row.get("common_valid_delta"),
                (common_after - common_before) / common_n,
            )
        else:
            assert_equal(checks, f"{prefix}.common_valid_delta_null_when_empty", row.get("common_valid_delta"), None)
    return checks


def statistics_summary_rebuild_checks(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """从原始 baseline/final result.json 重算每个 summary 行，拒绝只在统计 JSON 内自洽的篡改。"""
    checks: list[dict[str, Any]] = []
    rows = stats.get("summary", [])
    expected_rows: list[dict[str, Any]] = []
    for model_index, (model, config) in enumerate(MODELS.items()):
        try:
            baseline = flatten_result(Path(config["baseline"]))
            final = flatten_result(Path(config["final"]))
        except (OSError, TypeError, ValueError) as exc:
            assert_equal(checks, f"statistics.rebuild.{model}.inputs_load", str(exc), "")
            continue
        if baseline.keys() != final.keys():
            assert_equal(checks, f"statistics.rebuild.{model}.key_set_matches", baseline.keys(), final.keys())
            continue
        for split_index, split in enumerate(("train", "heldout")):
            expected = analyze_split(
                baseline,
                final,
                split,
                seed=ANALYSIS_SEED + model_index * 10 + split_index,
            )
            expected_rows.append(
                {
                    "model": model,
                    "active_branch": config["active_branch"],
                    "bootstrap_seed": ANALYSIS_SEED + model_index * 10 + split_index,
                    **expected,
                }
            )
    assert_equal(checks, "statistics.summary_rebuild.row_count", len(rows), len(expected_rows))
    for index, expected in enumerate(expected_rows):
        actual = rows[index] if index < len(rows) and isinstance(rows[index], dict) else {}
        for key, expected_value in expected.items():
            assert_equal(
                checks,
                f"statistics.summary[{index}].{key}_matches_raw_results",
                actual.get(key),
                expected_value,
            )
    return checks


def statistical_configuration_checks(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """核对统计产物中的方法参数与分析器公开常量一致。"""
    checks: list[dict[str, Any]] = []
    from paper import analyze_experiments as analysis

    assert_equal(checks, "statistics.analysis_version", stats.get("analysis_version"), analysis.ANALYSIS_VERSION)
    assert_equal(checks, "statistics.bootstrap_samples", stats.get("bootstrap_samples"), analysis.BOOTSTRAP_SAMPLES)
    assert_equal(checks, "statistics.bootstrap_unit", stats.get("bootstrap_unit"), "task")
    assert_equal(
        checks,
        "statistics.bootstrap_percentile_method",
        stats.get("bootstrap_percentile_method"),
        analysis.BOOTSTRAP_PERCENTILE_METHOD,
    )
    assert_equal(checks, "statistics.permutation_method", stats.get("permutation_method"), analysis.PERMUTATION_METHOD)
    assert_equal(checks, "statistics.seed", stats.get("seed"), analysis.ANALYSIS_SEED)
    for index, row in enumerate(stats.get("summary", [])):
        model_index = 0 if row.get("model") == "DeepSeek V4 Flash" else 1 if row.get("model") == "Qwen3.7 Plus" else None
        split_index = 0 if row.get("split") == "train" else 1 if row.get("split") == "heldout" else None
        expected_seed = (
            analysis.ANALYSIS_SEED + model_index * 10 + split_index
            if model_index is not None and split_index is not None
            else None
        )
        assert_equal(checks, f"statistics.summary[{index}].bootstrap_seed", row.get("bootstrap_seed"), expected_seed)
    return checks


def statistical_design_checks(stats: dict[str, Any]) -> list[dict[str, Any]]:
    """核对统计设计元数据，而不是只核对分散的旧版常量字段。"""
    checks: list[dict[str, Any]] = []
    from paper import analyze_experiments as analysis

    expected = analysis.statistical_design_metadata()
    actual = stats.get("statistical_design")
    assert_equal(checks, "statistics.statistical_design", actual, expected)
    if isinstance(actual, dict):
        assert_equal(
            checks,
            "statistics.statistical_design.split_task_counts_matches_summary",
            actual.get("split_task_counts"),
            {
                row.get("split"): row.get("tasks")
                for row in stats.get("summary", [])
                if isinstance(row, dict) and row.get("split") in {"train", "heldout"}
            },
        )
    return checks


INFERENCE_FIELDS = (
    "mcnemar_exact_p",
    "task_permutation_p",
    "cluster_bootstrap_ci_low",
    "cluster_bootstrap_ci_high",
)


def confirmatory_inference_checks(
    stats: dict[str, Any],
    english: str,
    chinese: str,
) -> list[dict[str, Any]]:
    """确保论文没有把被 invalid 门禁阻断的描述性结果写成确认性推断。"""
    checks: list[dict[str, Any]] = []
    policy = stats.get("validity_policy", {})
    assert_equal(
        checks,
        "statistics.validity_policy.confirmatory_fields_null_when_inference_blocked",
        policy.get("confirmatory_fields_null_when_inference_blocked"),
        True,
    )
    rows = stats.get("summary", [])
    assert_equal(checks, "statistics.summary_nonempty", isinstance(rows, list) and bool(rows), True)
    all_blocked = bool(rows) and all(row.get("inference_valid") is False for row in rows)
    assert_equal(checks, "statistics.all_current_rows_inference_blocked", all_blocked, True)
    for index, row in enumerate(rows):
        prefix = f"statistics.summary[{index}]"
        baseline_invalid = row.get("baseline_invalid")
        final_invalid = row.get("final_invalid")
        counts_are_valid = all(
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for value in (baseline_invalid, final_invalid)
        )
        expected_inference_valid = (
            counts_are_valid and baseline_invalid == 0 and final_invalid == 0
        )
        assert_equal(
            checks,
            f"{prefix}.inference_valid_matches_invalid_gate",
            row.get("inference_valid"),
            expected_inference_valid,
        )
        blocked = not expected_inference_valid
        expected_blocked_reason = (
            None
            if expected_inference_valid
            else "confirmatory inference withheld because baseline or final contains unresolved infrastructure-invalid cells"
        )
        assert_equal(
            checks,
            f"{prefix}.blocked_reason_matches_invalid_gate",
            row.get("inference_blocked_reason"),
            expected_blocked_reason,
        )
        assert_equal(checks, f"{prefix}.blocked_reason_present", isinstance(row.get("inference_blocked_reason"), str) and bool(row.get("inference_blocked_reason", "").strip()), blocked)
        null_fields = all(row.get(field) is None for field in INFERENCE_FIELDS)
        assert_equal(checks, f"{prefix}.confirmatory_fields_null", null_fields, blocked)
        if row.get("inference_valid") is True:
            finite_fields = all(
                isinstance(row.get(field), (int, float))
                and not isinstance(row.get(field), bool)
                and math.isfinite(float(row[field]))
                for field in INFERENCE_FIELDS
            )
            assert_equal(checks, f"{prefix}.confirmatory_fields_finite", finite_fields, True)
            assert_equal(checks, f"{prefix}.blocked_reason_absent", row.get("inference_blocked_reason") in (None, ""), True)
    if all_blocked:
        assert_equal(
            checks,
            "en.withheld_inference_claim",
            "without confirmatory intervals or significance claims" in english,
            True,
        )
        assert_equal(
            checks,
            "zh.withheld_inference_claim",
            "不报告确认性置信区间或显著性" in chinese,
            True,
        )
    return checks


def narrative_claim_checks(
    stats: dict[str, Any],
    paired: dict[str, Any],
    english: str,
    chinese: str,
) -> list[dict[str, Any]]:
    """校验正文中的关键 invalid/共同有效样本声明来自当前统计产物。"""
    checks: list[dict[str, Any]] = []

    def rows_for(model: str) -> list[dict[str, Any]]:
        return [row for row in stats.get("summary", []) if row.get("model") == model]

    qwen_rows = rows_for("Qwen3.7 Plus")
    deepseek_rows = rows_for("DeepSeek V4 Flash")
    qwen_train = next((row for row in qwen_rows if row.get("split") == "train"), {})
    qwen_heldout = next((row for row in qwen_rows if row.get("split") == "heldout"), {})
    qwen_baseline_invalid = sum(int(row.get("baseline_invalid", 0)) for row in qwen_rows)
    qwen_candidate_invalid = sum(int(row.get("final_invalid", 0)) for row in qwen_rows)
    deepseek_baseline_invalid = sum(int(row.get("baseline_invalid", 0)) for row in deepseek_rows)
    deepseek_candidates = [
        int(row["candidate_invalid"])
        for row in stats.get("candidates", [])
        if row.get("model") == "DeepSeek V4 Flash"
    ]
    pair_counts = paired.get("pair_counts", {})
    both_valid = pair_counts.get("both_valid")
    baseline_only = pair_counts.get("baseline_invalid_only")
    candidate_only = pair_counts.get("candidate_invalid_only")
    both_invalid = pair_counts.get("both_invalid")
    checks_to_apply = [
        (
            "en.qwen_invalid_totals",
            f"baseline has {qwen_baseline_invalid}/128 effective invalid cells and the candidate has {qwen_candidate_invalid}/128",
            english,
        ),
        (
            "en.qwen_common_valid",
            f"Qwen Train has zero jointly valid baseline/candidate pairs and Heldout has only {qwen_heldout.get('common_valid_attempts')}",
            english,
        ),
        (
            "en.deepseek_baseline_invalid",
            f"DeepSeek V4 Flash baseline likewise contains {deepseek_baseline_invalid} effective invalid cells",
            english,
        ),
        (
            "en.deepseek_candidate_invalids",
            "The three DeepSeek candidates contain "
            + ", ".join(map(str, deepseek_candidates[:-1]))
            + (f", and {deepseek_candidates[-1]} effective invalid cells" if deepseek_candidates else ""),
            english,
        ),
        (
            "en.paired_missingness",
            f"Eighteen of 128 pairs are currently valid on both sides; {baseline_only} cells are missing only on the baseline side, {candidate_only} only on the candidate side, and {both_invalid} pairs are missing on both sides",
            english,
        ),
        (
            "en.qwen_heldout_sensitivity",
            f"Heldout retains only {qwen_heldout.get('common_valid_attempts')}/42 pairs, on which the observed rates are {fmt_pct(qwen_heldout.get('common_valid_baseline_rate'))} and {fmt_pct(qwen_heldout.get('common_valid_final_rate'))} ({fmt_pp(qwen_heldout.get('common_valid_delta'))})",
            english,
        ),
        (
            "en.deepseek_common_valid",
            f"DeepSeek retains {next((row.get('common_valid_attempts') for row in deepseek_rows if row.get('split') == 'train'), None)} Train and {next((row.get('common_valid_attempts') for row in deepseek_rows if row.get('split') == 'heldout'), None)} Heldout jointly valid cells",
            english,
        ),
        (
            "zh.qwen_invalid_totals",
            f"baseline 有效 invalid 为 {qwen_baseline_invalid}/128，candidate 为 {qwen_candidate_invalid}/128",
            chinese,
        ),
        (
            "zh.qwen_common_valid",
            f"Qwen Train 没有任何一对 baseline/candidate 同时有效的评测单元，Heldout 仅有 {qwen_heldout.get('common_valid_attempts')} 对共同有效单元",
            chinese,
        ),
        (
            "zh.deepseek_baseline_invalid",
            f"DeepSeek V4 Flash baseline 同样包含 {deepseek_baseline_invalid} 个有效 invalid",
            chinese,
        ),
        (
            "zh.paired_missingness",
            f"当前已有双方有效 {both_valid}/128 对；单侧缺失为 baseline {baseline_only}、candidate {candidate_only}；双方均缺失 {both_invalid} 对",
            chinese,
        ),
        (
            "zh.qwen_heldout_sensitivity",
            f"Heldout 仅保留 {qwen_heldout.get('common_valid_attempts')}/42 个配对单元，观察到 {fmt_pct(qwen_heldout.get('common_valid_baseline_rate'))}→{fmt_pct(qwen_heldout.get('common_valid_final_rate'))}（{fmt_pp(qwen_heldout.get('common_valid_delta'))}）",
            chinese,
        ),
        (
            "zh.deepseek_common_valid",
            f"DeepSeek 共同有效样本为 Train {next((row.get('common_valid_attempts') for row in deepseek_rows if row.get('split') == 'train'), None)}、Heldout {next((row.get('common_valid_attempts') for row in deepseek_rows if row.get('split') == 'heldout'), None)}",
            chinese,
        ),
    ]
    for name, claim, document in checks_to_apply:
        assert_equal(checks, name, claim in document, True)
    # 使局部变量对应当前汇总行，若统计结构被破坏则显式失败而不是静默生成错误文本。
    assert_equal(checks, "statistics.qwen_train_row_present", bool(qwen_train), True)
    assert_equal(checks, "statistics.qwen_heldout_row_present", bool(qwen_heldout), True)
    return checks


def audit_input_paths() -> tuple[Path, ...]:
    """返回论文一致性审计需要在整个运行期间保持稳定的输入集合。"""
    paired_dir = ROOT / "paper" / "generated" / "paired-rerun"
    rerun_dir = ROOT / "paper" / "generated" / "rerun-plans"
    paths = [
        ROOT / "paper" / "generated" / "statistics.json",
        ROOT / "paper" / "generated" / "summary.csv",
        ROOT / "paper" / "generated" / "candidates.csv",
        ROOT / "paper" / "generated" / "STATISTICAL_AUDIT.md",
        ROOT / "paper" / "generated" / "effect_sizes.svg",
        ROOT / "paper" / "generated" / "invalid_runs.svg",
        ROOT / "paper" / "generated" / "mechanism" / "mechanism_evidence.json",
        ROOT / "paper" / "generated" / "mechanism" / "MECHANISM_EVIDENCE.md",
        ROOT / "paper" / "generated" / "mechanism" / "mechanism_transitions.csv",
        ROOT / "paper" / "generated" / "mechanism" / "mechanism_evidence.svg",
        ROOT / "paper" / "generated" / "split-coverage" / "split_coverage.json",
        ROOT / "paper" / "generated" / "split-coverage" / "SPLIT_COVERAGE_AUDIT.md",
        ROOT / "paper" / "generated" / "split-coverage" / "split_coverage_counts.csv",
        ROOT / "paper" / "generated" / "split-coverage" / "split_coverage.svg",
        ROOT / "paper" / "generated" / "container-audit" / "container_reproducibility.json",
        ROOT / "paper" / "generated" / "container-audit" / "CONTAINER_REPRODUCIBILITY_AUDIT.md",
        ROOT / "paper" / "generated" / "container-audit" / "container_base_images.csv",
        ROOT / "paper" / "generated" / "container-audit" / "sealed21_container_resolution.preview.json",
        ROOT / "paper" / "generated" / "design-audit" / "confirmation_design_audit.json",
        ROOT / "paper" / "generated" / "design-audit" / "CONFIRMATION_DESIGN_AUDIT.md",
        SEALED_PLAN_PATH,
        SEALED_MANIFEST_LOCK,
        SEALED_PREREGISTRATION,
        SEALED_PROTOCOL,
        SEALED_CONFIG,
        SEALED_BASELINE_SURFACE,
        SEALED_CANDIDATE_SURFACE,
        ROOT / "eval" / "harness_workspace" / "self_harness_harbor" / "backend_bridge.py",
        ROOT / "eval" / "scripts" / "capture_container_resolution.py",
        ROOT / "eval" / "scripts" / "capture_environment_lock.py",
        ROOT / "eval" / "harness_workspace" / "self_harness_harbor" / "harbor_wrapper.py",
        ROOT / "eval" / "scripts" / "run_harbor_eval.py",
        ROOT / "paper" / "analyze_sealed.py",
        ROOT / "acceptance" / "scripts" / "run_acceptance_gate.py",
        ROOT / "eval" / "scripts" / "build_sealed_split.py",
        ROOT / "paper" / "EXPERIMENTS_EN.md",
        ROOT / "paper" / "EXPERIMENTS_ZH.md",
        paired_dir / "paired_rerun_plan.json",
        paired_dir / "PAIRED_RERUN_PLAN.md",
        paired_dir / "paired_rerun_cells.csv",
        *sorted(paired_dir.glob("phase_*.json")),
        *sorted(rerun_dir.glob("*.json")),
        MECHANISM_PROPOSAL,
        MECHANISM_BASELINE_RESULT,
        MECHANISM_CANDIDATE_RESULT,
        SPLIT_CLEAN_CONFIG,
        SPLIT_SEALED_MANIFEST,
        *sorted(SPLIT_TASK_ROOT.glob("*/task.toml")),
        *sorted(
            dockerfile
            for task_dir in CONTAINER_TASK_ROOT.iterdir()
            if task_dir.is_dir() and (task_dir / "task.toml").is_file()
            for dockerfile in task_dir.rglob("Dockerfile*")
            if dockerfile.is_file()
        ),
        DESIGN_AUDIT_SCRIPT,
        DESIGN_STATISTICS_SCRIPT,
        DESIGN_VALIDITY_SCRIPT,
    ]
    sealed_strict_artifact = (
        ROOT
        / "runs"
        / "clean64-qwen-self-harness"
        / "branches"
        / "baseline"
        / "candidates"
        / "anti_workaround_execution"
        / "acceptance.strict.json"
    )
    if sealed_strict_artifact.is_file():
        paths.append(sealed_strict_artifact)
    return tuple(dict.fromkeys(path.resolve() for path in paths))


def artifact_inventory_checks(phase_count: int, single_count: int) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    assert_equal(checks, "paired.phase_manifest_count", phase_count, 6)
    assert_equal(checks, "single_plan_count", single_count, 10)
    return checks


def run_audit() -> dict[str, Any]:
    input_paths = audit_input_paths()
    input_before = stable_hash_snapshot(input_paths)
    stats = load_json(ROOT / "paper" / "generated" / "statistics.json")
    english = (ROOT / "paper" / "EXPERIMENTS_EN.md").read_text(encoding="utf-8")
    chinese = (ROOT / "paper" / "EXPERIMENTS_ZH.md").read_text(encoding="utf-8")
    paired = load_json(ROOT / "paper" / "generated" / "paired-rerun" / "paired_rerun_plan.json")
    checks: list[dict[str, Any]] = []

    phase_manifests = [
        (path.name, load_json(path))
        for path in sorted((ROOT / "paper" / "generated" / "paired-rerun").glob("phase_*.json"))
    ]
    single_plans = [
        (path.name, load_json(path))
        for path in sorted((ROOT / "paper" / "generated" / "rerun-plans").glob("*.json"))
    ]
    checks.extend(artifact_inventory_checks(len(phase_manifests), len(single_plans)))
    checks.extend(provenance_checks(stats, paired, phase_manifests, single_plans))
    checks.extend(paired_plan_semantic_checks(paired))
    checks.extend(phase_manifest_semantic_checks(paired, phase_manifests))
    checks.extend(paired_derived_artifact_checks(paired))
    checks.extend(statistics_derived_artifact_checks(stats))
    checks.extend(statistics_figure_checks(stats))
    checks.extend(supplementary_artifact_checks())
    checks.extend(supplementary_provenance_checks())
    checks.extend(container_artifact_checks())
    checks.extend(design_artifact_checks())
    checks.extend(container_resolution_preview_checks())
    checks.extend(sealed_execution_plan_checks())
    checks.extend(sealed_protocol_checks())
    checks.extend(sealed_statistical_design_checks())
    sealed_output = ROOT / "paper" / "generated" / "sealed21" / "sealed_statistics.json"
    if sealed_output.is_file():
        checks.extend(sealed_statistics_status_checks())
    checks.extend(
        sealed_readiness_narrative_checks(
            plan=load_json(SEALED_PLAN_PATH),
            english=english,
            chinese=chinese,
        )
    )
    checks.extend(candidate_provenance_checks(stats))
    checks.extend(statistical_unit_checks(stats))
    checks.extend(statistics_summary_rebuild_checks(stats))
    checks.extend(statistical_configuration_checks(stats))
    checks.extend(statistical_design_checks(stats))
    checks.extend(confirmatory_inference_checks(stats, english, chinese))
    checks.extend(narrative_claim_checks(stats, paired, english, chinese))

    for language, document in (("en", english), ("zh", chinese)):
        rows = table_rows(document, "## 4.5" if language == "en" else "## 4.5")
        expected_rows = []
        for row in stats["summary"]:
            expected_rows.append(
                [
                    row["model"],
                    row["split"].title(),
                    fmt_pct(row["baseline_rate"]),
                    fmt_pct(row["final_rate"]),
                    fmt_pp(row["delta"]),
                    f"{row['baseline_invalid']}→{row['final_invalid']}",
                    "incomplete",
                ]
            )
        assert_equal(checks, f"{language}.historical_table", rows, expected_rows)

        rows = table_rows(document, "## 4.6" if language == "en" else "## 4.6")
        expected_rows = []
        for row in stats["candidates"]:
            decision = row["decision"]
            if row["decision_source"] == "historical" and decision == "accepted":
                decision = "accepted (historical)"
            if language == "zh":
                decision = {"rejected": "拒绝", "accepted (historical)": "接受（历史）"}[decision]
            expected_rows.append(
                [
                    row["model"],
                    f"`{row['candidate_id']}`",
                    row["mechanism_family"].replace("_", " "),
                    fmt_pp(row["train_delta"]),
                    fmt_pp(row["heldout_delta"]),
                    decision,
                    f"{row['baseline_invalid']}→{row['candidate_invalid']}",
                    ("yes" if row["strict_gate_reproducible"] else "no")
                    if language == "en"
                    else ("是" if row["strict_gate_reproducible"] else "否"),
                ]
            )
        assert_equal(checks, f"{language}.candidate_table", rows, expected_rows)

        budget = paired["total_budget"]
        for label, value in (
            ("paired_cells", budget["cells"]),
            ("paired_attempts", budget["max_model_attempts"]),
            ("paired_wall_hours", budget["configured_wall_hours"]),
        ):
            assert_equal(checks, f"{language}.{label}_mentioned", str(value) in document, True)

    input_after = stable_hash_snapshot(input_paths)
    require_stable_analysis_inputs(input_before, input_after)
    return {
        "format": "self_harness.paper_consistency_audit.v3",
        "audit_input_integrity": {
            "stable_before_after_audit": True,
            "tracked_input_count": len(input_before),
        },
        "checks": checks,
        "passed": all(check["ok"] for check in checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    if not result["passed"]:
        failed = [check for check in result["checks"] if not check["ok"]]
        raise SystemExit(f"paper consistency audit failed: {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
