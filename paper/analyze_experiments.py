"""从 Clean64 运行产物生成可复核的统计汇总。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
VALIDITY_DIR = ROOT / "eval" / "scripts"
ACCEPTANCE_GATE_PATH = ROOT / "acceptance" / "scripts" / "run_acceptance_gate.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(VALIDITY_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDITY_DIR))

from result_validity import is_effectively_invalid, load_json_object
from acceptance.scripts.run_acceptance_gate import build_reason, verify_acceptance_artifact

DEFAULT_OUTPUT = ROOT / "paper" / "generated"
MODELS = {
    "DeepSeek V4 Flash": {
        "baseline": ROOT / "runs" / "clean64-deepseek-baseline" / "result.json",
        "final": ROOT / "runs" / "clean64-deepseek-self-harness" / "baseline_eval" / "result.json",
        "queue": ROOT / "runs" / "clean64-deepseek-self-harness" / "candidate_queue.json",
        "active_branch": "baseline",
    },
    "Qwen3.7 Plus": {
        "baseline": ROOT / "runs" / "clean64-qwen-baseline" / "result.json",
        "final": ROOT
        / "runs"
        / "clean64-qwen-self-harness"
        / "branches"
        / "baseline"
        / "candidates"
        / "anti_workaround_execution"
        / "eval"
        / "result.json",
        "queue": ROOT / "runs" / "clean64-qwen-self-harness" / "candidate_queue.json",
        "active_branch": "baseline+anti_workaround_execution",
    },
}

ANALYSIS_VERSION = "1.1"
BOOTSTRAP_SAMPLES = 20_000
ANALYSIS_SEED = 20260812
PERMUTATION_METHOD = "two-sided exact task-level sign permutation via dynamic programming"
BOOTSTRAP_PERCENTILE_METHOD = "linear interpolation on sorted samples, q*(n-1) (Hyndman-Fan type 7)"
EXPECTED_SPLIT_TASKS = {"train": 43, "heldout": 21}
EXPECTED_REPEAT_IDS = (1, 2)
ATTEMPTS_PER_TASK = len(EXPECTED_REPEAT_IDS)


def statistical_design_metadata() -> dict[str, Any]:
    """返回论文确认统计设计的结构化、可审计元数据。"""
    return {
        "comparison_unit": "task",
        "observation_unit": "attempt",
        "split_task_counts": dict(EXPECTED_SPLIT_TASKS),
        "repeat_ids": list(EXPECTED_REPEAT_IDS),
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "bootstrap": {
            "unit": "task",
            "resamples": BOOTSTRAP_SAMPLES,
            "seed": ANALYSIS_SEED,
            "seed_derivation": "ANALYSIS_SEED + model_index * 10 + split_index",
            "percentile_method": BOOTSTRAP_PERCENTILE_METHOD,
        },
        "permutation": {
            "unit": "task",
            "sidedness": "two-sided",
            "exact": True,
            "aggregation": "mean signed pass difference across attempts within task",
            "algorithm": "dynamic programming over rational task effects",
        },
    }


def build_statistical_design_metadata(
    *,
    split_task_counts: dict[str, int],
    roles: list[str] | None = None,
    seed_derivation: str = "ANALYSIS_SEED + model_index * 10 + split_index",
) -> dict[str, Any]:
    """构造 Clean64 与 Sealed21 共用的统计设计元数据。"""
    design: dict[str, Any] = {
        "comparison_unit": "task",
        "observation_unit": "attempt",
        "split_task_counts": dict(split_task_counts),
        "repeat_ids": list(EXPECTED_REPEAT_IDS),
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "bootstrap": {
            "unit": "task",
            "resamples": BOOTSTRAP_SAMPLES,
            "seed": ANALYSIS_SEED,
            "seed_derivation": seed_derivation,
            "percentile_method": BOOTSTRAP_PERCENTILE_METHOD,
        },
        "permutation": {
            "unit": "task",
            "sidedness": "two-sided",
            "exact": True,
            "aggregation": "mean signed pass difference across attempts within task",
            "algorithm": "dynamic programming over rational task effects",
        },
    }
    if roles is not None:
        design["roles"] = list(roles)
    return design


def statistical_design_metadata() -> dict[str, Any]:
    """返回 Clean64 统计设计的结构化、可审计元数据。"""
    return build_statistical_design_metadata(split_task_counts=EXPECTED_SPLIT_TASKS)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return load_json_object(path)


def analysis_input_paths() -> tuple[Path, ...]:
    """返回 Clean64 统计会读取的结果、队列和 acceptance 输入文件。"""
    paths: set[Path] = set()
    for config in MODELS.values():
        paths.update(Path(config[key]).resolve() for key in ("baseline", "final", "queue"))
        queue_path = Path(config["queue"]).resolve()
        queue = load_json(queue_path)
        for candidate in queue.get("candidates", []):
            historical = Path(candidate["acceptance_result"]).resolve()
            acceptance_paths = [historical]
            strict = historical.with_name("acceptance.strict.json")
            if strict.is_file():
                acceptance_paths.append(strict)
            paths.update(acceptance_paths)
            for acceptance_path in acceptance_paths:
                acceptance = load_json(acceptance_path)
                for key in ("baseline_result", "candidate_result"):
                    referenced = acceptance.get(key)
                    if referenced:
                        paths.add(Path(str(referenced)).resolve())
    return tuple(sorted(paths, key=lambda item: source_path(item)))


def stable_hash_snapshot(paths: Iterable[Path]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"analysis input is missing: {resolved}")
        before = sha256_file(resolved)
        after = sha256_file(resolved)
        if before != after:
            raise ValueError(f"analysis input changed while being hashed: {resolved}")
        snapshot[str(resolved)] = before
    return snapshot


def require_stable_analysis_inputs(
    before: dict[str, str], after: dict[str, str]
) -> None:
    if before != after:
        changed = sorted(
            set(before) | set(after),
            key=lambda item: item,
        )
        details = [
            f"{path}: {before.get(path)} -> {after.get(path)}"
            for path in changed
            if before.get(path) != after.get(path)
        ]
        raise ValueError("Clean64 analysis inputs changed during analysis: " + "; ".join(details))


def flatten_result(path: Path) -> dict[tuple[str, int, str], dict[str, Any]]:
    result = load_json(path)
    splits = result.get("splits")
    if not isinstance(splits, dict):
        raise ValueError(f"{path} must contain a splits object")
    rows: dict[tuple[str, int, str], dict[str, Any]] = {}
    for split, repeats in splits.items():
        if not isinstance(split, str) or not split:
            raise ValueError(f"{path} contains an invalid split name: {split!r}")
        if not isinstance(repeats, list):
            raise ValueError(f"{path} split={split} must contain a repeat list")
        for repeat_record in repeats:
            if not isinstance(repeat_record, dict):
                raise ValueError(f"{path} split={split} contains a non-object repeat")
            outer_repeat = repeat_record.get("repeat")
            if isinstance(outer_repeat, bool):
                raise ValueError(f"{path} split={split} has a boolean repeat label")
            try:
                outer_repeat = int(outer_repeat)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path} split={split} has an invalid repeat label") from exc
            cases = repeat_record.get("case_results")
            if not isinstance(cases, list):
                raise ValueError(f"{path} split={split} repeat={outer_repeat} must contain case_results")
            for case in cases:
                if not isinstance(case, dict):
                    raise ValueError(f"{path} split={split} repeat={outer_repeat} contains a non-object case")
                case_repeat = case.get("repeat")
                if isinstance(case_repeat, bool):
                    raise ValueError(f"{path} split={split} repeat={outer_repeat} has a boolean case repeat")
                try:
                    case_repeat = int(case_repeat)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{path} split={split} has an invalid case repeat label") from exc
                if case_repeat != outer_repeat:
                    raise ValueError(
                        f"{path} split={split} case repeat mismatch: outer={outer_repeat}, case={case_repeat}"
                    )
                case_id = case.get("case_id")
                if not isinstance(case_id, str) or not case_id:
                    raise ValueError(f"{path} split={split} repeat={outer_repeat} has an invalid case_id")
                key = (split, case_repeat, case_id)
                if key in rows:
                    raise ValueError(f"duplicate experiment key: {key}")
                rows[key] = case
    return rows


def validate_clean64_layout(rows: dict[tuple[str, int, str], dict[str, Any]], *, label: str) -> None:
    """校验 Clean64 的固定 43/21 task × 2 repeat 设计。"""
    expected_tasks = EXPECTED_SPLIT_TASKS
    actual_splits = {key[0] for key in rows}
    if actual_splits != set(expected_tasks):
        raise ValueError(f"{label} split layout mismatch: expected={sorted(expected_tasks)}, actual={sorted(actual_splits)}")
    for split, task_count in expected_tasks.items():
        split_rows = [key for key in rows if key[0] == split]
        repeats = sorted({key[1] for key in split_rows})
        if repeats != list(EXPECTED_REPEAT_IDS):
            raise ValueError(f"{label} split={split} must contain repeat ids {list(EXPECTED_REPEAT_IDS)}, got {repeats}")
        case_sets = [{key[2] for key in split_rows if key[1] == repeat} for repeat in repeats]
        if any(len(case_ids) != task_count for case_ids in case_sets):
            raise ValueError(
                f"{label} split={split} must contain {task_count} tasks per repeat, "
                f"got {[len(case_ids) for case_ids in case_sets]}"
            )
        if case_sets[0] != case_sets[1]:
            raise ValueError(f"{label} split={split} repeat task sets do not match")


def validate_paired_result_keys(
    baseline: dict[tuple[str, int, str], dict[str, Any]],
    candidate: dict[tuple[str, int, str], dict[str, Any]],
    *,
    label: str,
) -> None:
    """要求两侧比较结果使用完全相同的 split/repeat/case_id 配对键。"""
    baseline_keys = set(baseline)
    candidate_keys = set(candidate)
    if baseline_keys != candidate_keys:
        missing_from_candidate = sorted(baseline_keys - candidate_keys)
        missing_from_baseline = sorted(candidate_keys - baseline_keys)
        raise ValueError(
            f"{label} paired result key mismatch: "
            f"missing_from_candidate={missing_from_candidate}, "
            f"missing_from_baseline={missing_from_baseline}"
        )


def passed_for_descriptive_rate(case: dict[str, Any]) -> bool:
    """只把 JSON boolean true 计为通过；结构无效单元按失败计入固定分母。"""
    value = case.get("passed")
    return value if isinstance(value, bool) else False


def exact_mcnemar_p(improved: int, regressed: int) -> float:
    """双侧精确 McNemar 检验，即不一致对上的二项检验。"""
    discordant = improved + regressed
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(improved, regressed) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"percentile q must be between 0 and 1: {q}")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def clustered_bootstrap_ci(
    pairs: list[tuple[str, bool, bool]], *, seed: int, samples: int = BOOTSTRAP_SAMPLES
) -> tuple[float, float]:
    """按任务重采样，任务内保留两次 repeat 的相关性。"""
    if samples <= 0:
        raise ValueError(f"bootstrap samples must be positive: {samples}")
    grouped: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for case_id, before, after in pairs:
        grouped[case_id].append((before, after))
    case_ids = sorted(grouped)
    rng = random.Random(seed)
    deltas: list[float] = []
    for _ in range(samples):
        sampled = [rng.choice(case_ids) for _ in case_ids]
        observations = [pair for case_id in sampled for pair in grouped[case_id]]
        deltas.append(sum(int(after) - int(before) for before, after in observations) / len(observations))
    return percentile(deltas, 0.025), percentile(deltas, 0.975)


def task_level_permutation_p(pairs: list[tuple[str, bool, bool]]) -> float:
    """按任务聚合 repeat 后，以动态规划执行双侧精确符号置换检验。"""
    grouped: dict[str, list[int]] = defaultdict(list)
    for case_id, before, after in pairs:
        grouped[case_id].append(int(after) - int(before))
    effects = [Fraction(sum(values), len(values)) for values in grouped.values()]
    nonzero = [effect for effect in effects if effect]
    if not nonzero:
        return 1.0
    observed_sum = abs(sum(effects, Fraction(0)))
    distribution: Counter[Fraction] = Counter({Fraction(0): 1})
    for effect in nonzero:
        magnitude = abs(effect)
        next_distribution: Counter[Fraction] = Counter()
        for total_effect, count in distribution.items():
            next_distribution[total_effect + magnitude] += count
            next_distribution[total_effect - magnitude] += count
        distribution = next_distribution
    extreme = sum(count for effect, count in distribution.items() if abs(effect) >= observed_sum)
    return extreme / (2 ** len(nonzero))


def analyze_split(
    baseline: dict[tuple[str, int, str], dict[str, Any]],
    final: dict[tuple[str, int, str], dict[str, Any]],
    split: str,
    seed: int,
) -> dict[str, Any]:
    keys = sorted(key for key in baseline.keys() & final.keys() if key[0] == split)
    if not keys:
        raise ValueError(f"没有找到 split={split} 的配对结果")
    pairs = [
        (
            key[2],
            passed_for_descriptive_rate(baseline[key]),
            passed_for_descriptive_rate(final[key]),
        )
        for key in keys
    ]
    before_passed = sum(before for _, before, _ in pairs)
    after_passed = sum(after for _, _, after in pairs)
    improved = sum((not before) and after for _, before, after in pairs)
    regressed = sum(before and (not after) for _, before, after in pairs)
    common_valid_keys = [
        key
        for key in keys
        if not is_effectively_invalid(baseline[key]) and not is_effectively_invalid(final[key])
    ]
    common_before = sum(passed_for_descriptive_rate(baseline[key]) for key in common_valid_keys)
    common_after = sum(passed_for_descriptive_rate(final[key]) for key in common_valid_keys)
    n = len(keys)
    common_n = len(common_valid_keys)
    baseline_invalid = sum(is_effectively_invalid(baseline[key]) for key in keys)
    final_invalid = sum(is_effectively_invalid(final[key]) for key in keys)
    inference_valid = baseline_invalid == 0 and final_invalid == 0
    if inference_valid:
        ci_low, ci_high = clustered_bootstrap_ci(pairs, seed=seed)
        mcnemar_p = exact_mcnemar_p(improved, regressed)
        permutation_p = task_level_permutation_p(pairs)
        inference_blocked_reason = None
    else:
        ci_low = ci_high = None
        mcnemar_p = permutation_p = None
        inference_blocked_reason = (
            "confirmatory inference withheld because baseline or final contains "
            "unresolved infrastructure-invalid cells"
        )
    return {
        "split": split,
        "attempts": n,
        "tasks": len({key[2] for key in keys}),
        "baseline_passed": before_passed,
        "final_passed": after_passed,
        "baseline_rate": before_passed / n,
        "final_rate": after_passed / n,
        "delta": (after_passed - before_passed) / n,
        "baseline_invalid": baseline_invalid,
        "final_invalid": final_invalid,
        "inference_valid": inference_valid,
        "inference_blocked_reason": inference_blocked_reason,
        "improved_pairs": improved,
        "regressed_pairs": regressed,
        "mcnemar_exact_p": mcnemar_p,
        "task_permutation_p": permutation_p,
        "cluster_bootstrap_ci_low": ci_low,
        "cluster_bootstrap_ci_high": ci_high,
        "common_valid_attempts": common_n,
        "common_valid_baseline_passed": common_before,
        "common_valid_final_passed": common_after,
        "common_valid_baseline_rate": common_before / common_n if common_n else None,
        "common_valid_final_rate": common_after / common_n if common_n else None,
        "common_valid_delta": (common_after - common_before) / common_n if common_n else None,
    }


def recompute_historical_acceptance(
    baseline_path: Path,
    candidate_path: Path,
    *,
    splits: tuple[str, ...] = ("train", "heldout"),
    expected_repeats: int = 2,
) -> dict[str, Any]:
    """按固定分母规则从两侧原始结果重算 historical v0 acceptance。"""
    if expected_repeats < 1:
        raise ValueError("historical acceptance expected_repeats must be positive")
    baseline_rows = flatten_result(baseline_path)
    candidate_rows = flatten_result(candidate_path)
    validate_paired_result_keys(
        baseline_rows,
        candidate_rows,
        label=f"historical acceptance {baseline_path.name}/{candidate_path.name}",
    )
    comparisons: dict[str, dict[str, Any]] = {}
    improved: list[str] = []
    dropped: list[str] = []
    for split in splits:
        baseline_repeats = []
        candidate_repeats = []
        for repeat in range(1, expected_repeats + 1):
            b_values = [
                row
                for (row_split, row_repeat, _), row in baseline_rows.items()
                if row_split == split and row_repeat == repeat
            ]
            c_values = [
                row
                for (row_split, row_repeat, _), row in candidate_rows.items()
                if row_split == split and row_repeat == repeat
            ]
            if not b_values or not c_values or len(b_values) != len(c_values):
                raise ValueError(f"historical acceptance has incomparable {split} repeat {repeat}")
            b_passed = sum(row.get("passed") is True for row in b_values)
            c_passed = sum(row.get("passed") is True for row in c_values)
            baseline_repeats.append(
                {"repeat": repeat, "passed": b_passed, "total": len(b_values), "pass_rate": b_passed / len(b_values)}
            )
            candidate_repeats.append(
                {"repeat": repeat, "passed": c_passed, "total": len(c_values), "pass_rate": c_passed / len(c_values)}
            )
        baseline_average = sum(item["pass_rate"] for item in baseline_repeats) / expected_repeats
        candidate_average = sum(item["pass_rate"] for item in candidate_repeats) / expected_repeats
        delta = candidate_average - baseline_average
        status = "improved" if delta > 0 else "dropped" if delta < 0 else "unchanged"
        comparisons[split] = {
            "baseline_average_pass_rate": baseline_average,
            "candidate_average_pass_rate": candidate_average,
            "delta": delta,
            "status": status,
            "baseline_repeats": baseline_repeats,
            "candidate_repeats": candidate_repeats,
        }
        (improved if status == "improved" else dropped if status == "dropped" else []).append(split)
    accepted = not dropped and bool(improved)
    return {
        "accepted": accepted,
        "decision": "accepted" if accepted else "rejected",
        "reason": build_reason(accepted=accepted, improved=improved, dropped=dropped),
        "rule": {
            "splits": list(splits),
            "expected_repeats": expected_repeats,
            "average_metric": "pass_rate",
            "accept_if": "no split drops and at least one split improves",
        },
        "splits": comparisons,
    }


def candidate_rows(
    model: str, queue_path: Path, *, require_clean64_layout: bool = False
) -> list[dict[str, Any]]:
    rows = []
    for candidate in load_json(queue_path)["candidates"]:
        historical_path = Path(candidate["acceptance_result"])
        strict_path = historical_path.with_name("acceptance.strict.json")
        acceptance_path = strict_path if strict_path.is_file() else historical_path
        acceptance = load_json(acceptance_path)
        baseline_rows = flatten_result(Path(acceptance["baseline_result"]))
        candidate_results = flatten_result(Path(acceptance["candidate_result"]))
        validate_paired_result_keys(
            baseline_rows,
            candidate_results,
            label=f"{model}/{candidate['candidate_id']}",
        )
        if require_clean64_layout:
            validate_clean64_layout(baseline_rows, label=f"{model}/{candidate['candidate_id']} baseline")
            validate_clean64_layout(candidate_results, label=f"{model}/{candidate['candidate_id']} candidate")
        if acceptance_path == strict_path:
            verify_acceptance_artifact(
                artifact=acceptance,
                baseline_result_path=Path(acceptance["baseline_result"]),
                candidate_result_path=Path(acceptance["candidate_result"]),
                artifact_path=acceptance_path,
                artifact_sha256=sha256_file(acceptance_path),
                splits=("train", "heldout"),
                expected_repeats=2,
            )
        else:
            recomputed_acceptance = recompute_historical_acceptance(
                Path(acceptance["baseline_result"]),
                Path(acceptance["candidate_result"]),
            )
            for field in ("accepted", "decision", "reason", "rule", "splits"):
                if acceptance.get(field) != recomputed_acceptance[field]:
                    raise ValueError(
                        f"historical acceptance {field} does not match recomputed acceptance"
                    )
        baseline_invalid = sum(is_effectively_invalid(row) for row in baseline_rows.values())
        candidate_invalid = sum(is_effectively_invalid(row) for row in candidate_results.values())
        rows.append(
            {
                "model": model,
                "candidate_id": candidate["candidate_id"],
                "mechanism_family": candidate["mechanism_family"],
                "decision": acceptance["decision"],
                "decision_source": "strict" if acceptance_path == strict_path else "historical",
                "acceptance_path": str(acceptance_path),
                "train_delta": acceptance["splits"]["train"]["delta"],
                "heldout_delta": acceptance["splits"]["heldout"]["delta"],
                "reason": acceptance["reason"],
                "baseline_invalid": baseline_invalid,
                "candidate_invalid": candidate_invalid,
                "strict_gate_reproducible": baseline_invalid == 0 and candidate_invalid == 0,
            }
        )
    return rows


def source_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def collect_source_files(snapshot: dict[str, str] | None = None) -> list[dict[str, str]]:
    if snapshot is None:
        paths = set(analysis_input_paths())
        paths.update(
            {
                Path(__file__).resolve(),
                (VALIDITY_DIR / "result_validity.py").resolve(),
                ACCEPTANCE_GATE_PATH.resolve(),
            }
        )
        snapshot = stable_hash_snapshot(paths)
    else:
        snapshot = dict(snapshot)
    source_records = [
        {"path": source_path(Path(path)), "sha256": digest}
        for path, digest in snapshot.items()
    ]
    source_records.extend(
        {
            "path": source_path(path),
            "sha256": sha256_file(path),
        }
        for path in (
            Path(__file__).resolve(),
            (VALIDITY_DIR / "result_validity.py").resolve(),
            ACCEPTANCE_GATE_PATH.resolve(),
        )
        if str(path.resolve()) not in snapshot
    )
    return sorted(source_records, key=lambda item: item["path"])


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{100 * value:.2f}%"


def build_markdown(summary: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    lines = [
        "# Clean64 实验统计审计",
        "",
        "历史固定分母数值把基础设施无效尝试按未通过计入，只用于说明旧流水线为何产生相应决策。评测单元有效性判定同时检查显式 status、verifier reward、runtime failure 和字段一致性。当前所有比较均含 unresolved invalid，因此置信区间和显著性不作确认性解释；共同有效样本也仅作为高度受选择影响的诊断。",
        "",
        "| 模型 | Split | Baseline | Final | Δ | 无效样本 B→F | 历史改善/退化对 | 95% CI | 任务级置换 p |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        interval = (
            f"[{fmt_pct(row['cluster_bootstrap_ci_low'])}, {fmt_pct(row['cluster_bootstrap_ci_high'])}]"
            if row["inference_valid"]
            else "incomplete"
        )
        permutation = f"{row['task_permutation_p']:.4f}" if row["inference_valid"] else "incomplete"
        lines.append(
            f"| {row['model']} | {row['split']} | {fmt_pct(row['baseline_rate'])} "
            f"| {fmt_pct(row['final_rate'])} | {fmt_pct(row['delta'])} "
            f"| {row['baseline_invalid']}→{row['final_invalid']} "
            f"| {row['improved_pairs']}/{row['regressed_pairs']} "
            f"| {interval} | {permutation} |"
        )
    lines.extend(
        [
            "",
            "## 共同有效样本敏感性分析",
            "",
            "| 模型 | Split | 共同有效 n | Baseline | Final | Δ |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in summary:
        lines.append(
            f"| {row['model']} | {row['split']} | {row['common_valid_attempts']} "
            f"| {fmt_pct(row['common_valid_baseline_rate'])} "
            f"| {fmt_pct(row['common_valid_final_rate'])} "
            f"| {fmt_pct(row['common_valid_delta'])} |"
        )
    lines.extend(
        [
            "",
            "## 候选门控结果",
            "",
            "| 模型 | 候选 | 机制 | Train Δ | Heldout Δ | 决策 | 来源 | Invalid B→C | 严格门控可复现 |",
            "|---|---|---|---:|---:|---|---|---:|---|",
        ]
    )
    for row in candidates:
        lines.append(
            f"| {row['model']} | `{row['candidate_id']}` | `{row['mechanism_family']}` "
            f"| {fmt_pct(row['train_delta'])} | {fmt_pct(row['heldout_delta'])} | {row['decision']} "
            f"| {row['decision_source']} "
            f"| {row['baseline_invalid']}→{row['candidate_invalid']} "
            f"| {'是' if row['strict_gate_reproducible'] else '否'} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_effect_svg(path: Path, summary: list[dict[str, Any]]) -> None:
    """生成无需绘图库的矢量效应量图。"""
    width, height = 980, 390
    left, right = 270, 930
    scale_min, scale_max = -10.0, 45.0

    def x(value: float) -> float:
        return left + (100 * value - scale_min) / (scale_max - scale_min) * (right - left)

    labels = [f"{row['model']} - {row['split'].title()}" for row in summary]
    ys = [105, 170, 235, 300]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#172033}.title{font-size:22px;font-weight:700}.label{font-size:15px}.tick{font-size:13px;fill:#4b5563}.note{font-size:12px;fill:#6b7280}</style>',
        '<text x="490" y="34" text-anchor="middle" class="title">Historical fixed-denominator effect (incomplete)</text>',
    ]
    for tick in range(-10, 46, 5):
        tick_x = x(tick / 100)
        parts.append(f'<line x1="{tick_x:.1f}" y1="65" x2="{tick_x:.1f}" y2="325" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{tick_x:.1f}" y="350" text-anchor="middle" class="tick">{tick}</text>')
    zero_x = x(0)
    parts.append(f'<line x1="{zero_x:.1f}" y1="60" x2="{zero_x:.1f}" y2="325" stroke="#111827" stroke-width="1.5"/>')
    for row, label, y in zip(summary, labels, ys):
        point = x(row["delta"])
        color = "#0f9d76" if row["model"].startswith("Qwen") else "#64748b"
        parts.append(f'<text x="250" y="{y + 5}" text-anchor="end" class="label">{label}</text>')
        if row["cluster_bootstrap_ci_low"] is not None and row["cluster_bootstrap_ci_high"] is not None:
            low = x(row["cluster_bootstrap_ci_low"])
            high = x(row["cluster_bootstrap_ci_high"])
            parts.append(f'<line x1="{low:.1f}" y1="{y}" x2="{high:.1f}" y2="{y}" stroke="{color}" stroke-width="4"/>')
            parts.append(f'<line x1="{low:.1f}" y1="{y - 8}" x2="{low:.1f}" y2="{y + 8}" stroke="{color}" stroke-width="2"/>')
            parts.append(f'<line x1="{high:.1f}" y1="{y - 8}" x2="{high:.1f}" y2="{y + 8}" stroke="{color}" stroke-width="2"/>')
        else:
            parts.append(f'<text x="{min(point + 12, 940):.1f}" y="{y + 17}" class="note">incomplete</text>')
        parts.append(f'<circle cx="{point:.1f}" cy="{y}" r="7" fill="{color}"/>')
        parts.append(f'<text x="{min(point + 12, 940):.1f}" y="{y - 10}" class="tick">{100 * row["delta"]:+.2f} pp</text>')
    parts.append('<text x="600" y="378" text-anchor="middle" class="note">Intervals are descriptive calculations only; unresolved infrastructure failures invalidate confirmation.</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_invalid_svg(path: Path, summary: list[dict[str, Any]]) -> None:
    """生成 baseline/final 基础设施无效单元对比图。"""
    width, height = 900, 390
    labels = [f"{row['model']}\n{row['split'].title()}" for row in summary]
    max_value = max(max(row["baseline_invalid"], row["final_invalid"]) for row in summary) or 1
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#172033}.title{font-size:22px;font-weight:700}.label{font-size:13px}.value{font-size:13px;font-weight:700}</style>',
        '<text x="450" y="34" text-anchor="middle" class="title">Infrastructure-invalid evaluation cells</text>',
    ]
    chart_bottom, chart_height = 290, 220
    tick_step = max(1, math.ceil(max_value / 5))
    ticks = list(range(0, max_value + 1, tick_step))
    if ticks[-1] != max_value:
        ticks.append(max_value)
    for tick in ticks:
        y = chart_bottom - tick / max_value * chart_height
        parts.append(f'<line x1="70" y1="{y:.1f}" x2="870" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="58" y="{y + 4:.1f}" text-anchor="end" class="label">{tick}</text>')
    for index, (row, label) in enumerate(zip(summary, labels)):
        center = 160 + index * 190
        for offset, key, color, name in ((-28, "baseline_invalid", "#94a3b8", "Baseline"), (28, "final_invalid", "#e76f51", "Final")):
            value = row[key]
            bar_height = value / max_value * chart_height
            parts.append(f'<rect x="{center + offset - 20}" y="{chart_bottom - bar_height:.1f}" width="40" height="{bar_height:.1f}" fill="{color}" rx="3"/>')
            parts.append(f'<text x="{center + offset}" y="{chart_bottom - bar_height - 7:.1f}" text-anchor="middle" class="value">{value}</text>')
        first, second = label.split("\n")
        parts.append(f'<text x="{center}" y="320" text-anchor="middle" class="label">{first}</text>')
        parts.append(f'<text x="{center}" y="338" text-anchor="middle" class="label">{second}</text>')
    parts.append('<rect x="335" y="360" width="14" height="14" fill="#94a3b8"/><text x="355" y="372" class="label">Baseline</text>')
    parts.append('<rect x="455" y="360" width="14" height="14" fill="#e76f51"/><text x="475" y="372" class="label">Final / candidate</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    tracked_before_paths = set(analysis_input_paths())
    tracked_before_paths.update(
        {
            Path(__file__).resolve(),
            (VALIDITY_DIR / "result_validity.py").resolve(),
            ACCEPTANCE_GATE_PATH.resolve(),
        }
    )
    tracked_before = stable_hash_snapshot(tracked_before_paths)

    summary: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for model_index, (model, config) in enumerate(MODELS.items()):
        baseline = flatten_result(config["baseline"])
        final = flatten_result(config["final"])
        validate_clean64_layout(baseline, label=f"{model} baseline")
        validate_clean64_layout(final, label=f"{model} final")
        if baseline.keys() != final.keys():
            raise ValueError(f"{model} 的 baseline/final 试验键不完全一致")
        for split_index, split in enumerate(("train", "heldout")):
            row = analyze_split(
                baseline,
                final,
                split,
                seed=ANALYSIS_SEED + model_index * 10 + split_index,
            )
            row = {
                "model": model,
                "active_branch": config["active_branch"],
                "bootstrap_seed": ANALYSIS_SEED + model_index * 10 + split_index,
                **row,
            }
            summary.append(row)
        candidates.extend(candidate_rows(model, config["queue"], require_clean64_layout=True))

    tracked_after_paths = set(analysis_input_paths())
    tracked_after_paths.update(
        {
            Path(__file__).resolve(),
            (VALIDITY_DIR / "result_validity.py").resolve(),
            ACCEPTANCE_GATE_PATH.resolve(),
        }
    )
    tracked_after = stable_hash_snapshot(tracked_after_paths)
    require_stable_analysis_inputs(tracked_before, tracked_after)

    write_csv(args.output_dir / "summary.csv", summary)
    write_csv(args.output_dir / "candidates.csv", candidates)
    payload = {
        "analysis_version": ANALYSIS_VERSION,
        "statistical_design": statistical_design_metadata(),
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "bootstrap_unit": "task",
        "bootstrap_percentile_method": BOOTSTRAP_PERCENTILE_METHOD,
        "permutation_method": PERMUTATION_METHOD,
        "seed": ANALYSIS_SEED,
        "validity_policy": {
            "fixed_denominator_invalid_counted_as_failure": True,
            "confirmatory_inference_requires_zero_invalid_on_both_sides": True,
            "common_valid_sensitivity_is_descriptive_only": True,
            "historical_missing_reward_field_compatibility": True,
            "strict_paths_require_reward_field": True,
            "confirmatory_fields_null_when_inference_blocked": True,
            "passed_requires_json_boolean": True,
            "finite_reward_required_when_reward_field_present": True,
        },
        "analysis_input_integrity": {
            "stable_before_after_analysis": True,
            "tracked_input_count": len(tracked_after),
        },
        "source_files": collect_source_files(tracked_after),
        "summary": summary,
        "candidates": candidates,
    }
    (args.output_dir / "statistics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "STATISTICAL_AUDIT.md").write_text(
        build_markdown(summary, candidates), encoding="utf-8"
    )
    write_effect_svg(args.output_dir / "effect_sizes.svg", summary)
    write_invalid_svg(args.output_dir / "invalid_runs.svg", summary)
    print(args.output_dir / "STATISTICAL_AUDIT.md")


if __name__ == "__main__":
    main()
